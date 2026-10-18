"""Run on another PC so the desktop agent can operate it."""

from __future__ import annotations

import argparse
import json
import secrets
import socket
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .computer import Computer, platform_kind
from .devices import COMPANION_PORT, new_token

APP = "llama-companion"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def local_ips() -> list[str]:
    found: list[str] = []
    if sys.platform == "win32":
        try:
            kwargs = {"creationflags": CREATE_NO_WINDOW} if CREATE_NO_WINDOW else {}
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress",
                ],
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        for line in output.splitlines():
            ip = line.strip()
            if ip and ip not in found:
                found.append(ip)
        return found
    try:
        output = subprocess.check_output(
            ["hostname", "-I"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        output = ""
    for ip in output.split():
        if ip.count(".") == 3 and not ip.startswith("127.") and not ip.startswith("169.254.") and ip not in found:
            found.append(ip)
    if found:
        return found
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return found
    for line in output.splitlines():
        parts = line.split()
        for part in parts:
            if "/" in part and part[0].isdigit():
                ip = part.split("/", 1)[0]
                if ip.count(".") == 3 and not ip.startswith("127.") and ip not in found:
                    found.append(ip)
    return found


class Companion:
    def __init__(self, name: str, token: str) -> None:
        self.name = name
        self.token = token
        self.kind = platform_kind()
        self.computer = Computer()

    def authorized(self, header: str) -> bool:
        value = (header or "").strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        return bool(value) and secrets.compare_digest(value, self.token)

    def act(self, data: dict) -> dict:
        tool = str(data.get("tool") or "").lower()
        if tool == "screen":
            text, image = self.computer.observe(bool(data.get("vision")))
            return {"ok": True, "text": text, "image": image}
        if tool == "mouse":
            text = self.computer.mouse(
                str(data.get("action") or "click"),
                data.get("x"),
                data.get("y"),
                int(data.get("amount") or 0),
            )
            return {"ok": True, "text": text}
        if tool == "keyboard":
            try:
                text = self.computer.keyboard(str(data.get("text") or ""), str(data.get("keys") or ""))
            except ValueError as exc:
                return {"ok": True, "text": str(exc)}
            return {"ok": True, "text": text}
        if tool == "command":
            text = self.computer.command(str(data.get("command") or ""), lambda: False)
            return {"ok": True, "text": text}
        return {"ok": False, "text": "Unknown tool."}


STATE: Companion | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 2_000_000:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/health":
            self._send(404, {"ok": False, "error": "Not found"})
            return
        assert STATE is not None
        self._send(200, {"app": APP, "name": STATE.name, "kind": STATE.kind})

    def do_POST(self) -> None:
        assert STATE is not None
        path = self.path.split("?", 1)[0]
        if path != "/action":
            self._send(404, {"ok": False, "error": "Not found"})
            return
        if not STATE.authorized(self.headers.get("Authorization", "")):
            self._send(401, {"ok": False, "error": "Bad token."})
            return
        try:
            self._send(200, STATE.act(self._body()))
        except Exception as exc:
            self._send(500, {"ok": False, "text": str(exc)[:300]})


def register_with_hub(hub: str, name: str, url: str, token: str, kind: str) -> str:
    hub = hub.rstrip("/")
    body = json.dumps({"name": name, "url": url, "token": token, "kind": kind}).encode("utf-8")
    request = urllib.request.Request(
        hub + "/api/devices/register",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": APP},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "Hub rejected the companion.")
    return "Registered with the hub."


def pick_url(port: int, prefer: str = "") -> str:
    if prefer:
        return prefer.rstrip("/")
    ips = local_ips()
    tailscale = []
    for ip in ips:
        parts = ip.split(".")
        if len(parts) == 4 and parts[0] == "100":
            try:
                if 64 <= int(parts[1]) <= 127:
                    tailscale.append(ip)
            except ValueError:
                pass
    host = (tailscale or ips or ["127.0.0.1"])[0]
    return f"http://{host}:{port}"


def main() -> None:
    global STATE
    parser = argparse.ArgumentParser(description="Let the Polypoda agent control this computer.")
    parser.add_argument("--name", default=socket.gethostname() or "Companion")
    parser.add_argument("--port", type=int, default=COMPANION_PORT)
    parser.add_argument("--token", default="")
    parser.add_argument("--hub", default="", help="Desktop launcher URL, for example http://100.x.x.x:47321")
    parser.add_argument("--url", default="", help="Public URL other machines should use for this companion")
    args = parser.parse_args()
    token = args.token.strip() or new_token()
    STATE = Companion(args.name.strip() or "Companion", token)
    url = pick_url(args.port, args.url)

    def say(text: str) -> None:
        print(text, flush=True)

    say(f"Companion: {STATE.name}")
    say(f"Kind: {STATE.kind}")
    say(f"Listening on 0.0.0.0:{args.port}")
    say(f"URL: {url}")
    say(f"Token: {token}")
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)

    def announce() -> None:
        if not args.hub:
            say("Add this URL and token in the Devices card on the desktop app.")
            return
        try:
            say(register_with_hub(args.hub, STATE.name, url, token, STATE.kind))
        except Exception as exc:
            say(f"Could not register with the hub: {exc}")
            say("Add this companion manually in the Devices card.")

    threading.Timer(0.3, announce).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("Stopped.")
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
