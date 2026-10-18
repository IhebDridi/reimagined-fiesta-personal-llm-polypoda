"""Track local, companion, and Android targets the agent can operate."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
COMPANION_PORT = 47340
TOKEN_BYTES = 18


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def _ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or ""
    candidates = [
        Path(sdk) / "platform-tools" / "adb.exe",
        Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        Path(r"C:\Android\platform-tools\adb.exe"),
        Path(r"C:\platform-tools\adb.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


class DeviceBackend:
    kind = "unknown"
    label = "Device"

    def observe(self, vision: bool) -> tuple[str, str]:
        raise NotImplementedError

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        raise NotImplementedError

    def keyboard(self, text: str, keys: str) -> str:
        raise NotImplementedError

    def command(self, command: str, stop) -> str:
        raise NotImplementedError


class LocalBackend(DeviceBackend):
    label = "This PC"

    def __init__(self, computer) -> None:
        from .computer import platform_kind

        self.computer = computer
        self.kind = platform_kind()

    def observe(self, vision: bool) -> tuple[str, str]:
        return self.computer.observe(vision)

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        return self.computer.mouse(action, x, y, amount)

    def keyboard(self, text: str, keys: str) -> str:
        return self.computer.keyboard(text, keys)

    def command(self, command: str, stop) -> str:
        return self.computer.command(command, stop)


class RemoteBackend(DeviceBackend):
    def __init__(self, name: str, url: str, token: str, kind: str = "windows") -> None:
        self.label = name
        self.url = url.rstrip("/")
        self.token = token
        self.kind = kind if kind in {"windows", "linux"} else "windows"

    def _post(self, path: str, payload: dict, timeout: int = 60) -> dict:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url + path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                "User-Agent": "llama.cpp-launcher",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Companion returned a bad response.")
        return data

    def ping(self) -> bool:
        try:
            request = urllib.request.Request(
                self.url + "/health",
                headers={"Authorization": "Bearer " + self.token, "User-Agent": "llama.cpp-launcher"},
            )
            with urllib.request.urlopen(request, timeout=4) as response:
                data = json.loads(response.read().decode("utf-8"))
            ok = data.get("app") == "llama-companion"
            if ok and data.get("kind") in {"windows", "linux"}:
                self.kind = data["kind"]
            return ok
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError):
            return False

    def observe(self, vision: bool) -> tuple[str, str]:
        data = self._post("/action", {"tool": "screen", "vision": vision}, timeout=45)
        return str(data.get("text") or ""), str(data.get("image") or "")

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        data = self._post("/action", {"tool": "mouse", "action": action, "x": x, "y": y, "amount": amount})
        return str(data.get("text") or "Done.")

    def keyboard(self, text: str, keys: str) -> str:
        data = self._post("/action", {"tool": "keyboard", "text": text, "keys": keys})
        return str(data.get("text") or "Done.")

    def command(self, command: str, stop) -> str:
        if stop():
            return "Command cancelled."
        data = self._post("/action", {"tool": "command", "command": command}, timeout=70)
        return str(data.get("text") or "Done.")


class AndroidBackend(DeviceBackend):
    kind = "android"

    def __init__(self, serial: str, adb: str, label: str = "") -> None:
        self.serial = serial
        self.adb = adb
        self.label = label or f"Android {serial}"

    def _run(self, args: list[str], timeout: int = 30, stop=None) -> tuple[int, str, str]:
        proc = subprocess.Popen(
            [self.adb, "-s", self.serial, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
        deadline = time.time() + timeout
        while proc.poll() is None:
            if stop and stop():
                proc.kill()
                return -1, "", "cancelled"
            if time.time() > deadline:
                proc.kill()
                return -1, "", "timeout"
            time.sleep(0.05)
        out, err = proc.communicate()
        return (
            proc.returncode or 0,
            (out or b"").decode("utf-8", errors="replace"),
            (err or b"").decode("utf-8", errors="replace"),
        )

    def _shell(self, command: str, timeout: int = 30, stop=None) -> tuple[int, str, str]:
        return self._run(["shell", command], timeout=timeout, stop=stop)

    def observe(self, vision: bool) -> tuple[str, str]:
        code, size_out, _ = self._shell("wm size")
        size = "unknown"
        match = re.search(r"(\d+)\s*x\s*(\d+)", size_out)
        if match:
            size = f"{match.group(1)}x{match.group(2)}"
        _code, dump, _ = self._shell("dumpsys window windows", timeout=20)
        lines = [f"Android screen size {size}. Coordinates are pixels from the top-left corner."]
        focus = []
        for line in dump.splitlines():
            text = line.strip()
            if text and ("mCurrentFocus" in text or "mFocusedApp" in text or "mSurface" in text):
                focus.append(text[:180])
                if len(focus) >= 8:
                    break
        if focus:
            lines.append("Focus:")
            lines.extend(f"- {item}" for item in focus)
        else:
            lines.append("Could not read the focused window.")
        image = ""
        if vision:
            code, out, err = self._run(["exec-out", "screencap", "-p"], timeout=25)
            if code == 0 and out.startswith(b"\x89PNG"):
                try:
                    from io import BytesIO
                    from PIL import Image

                    picture = Image.open(BytesIO(out)).convert("RGB")
                    picture.thumbnail((1280, 800))
                    buffer = BytesIO()
                    picture.save(buffer, format="JPEG", quality=60)
                    image = base64.b64encode(buffer.getvalue()).decode("ascii")
                    lines.append(f"Screenshot attached ({picture.width}x{picture.height}).")
                except OSError as exc:
                    lines.append(f"Screenshot failed: {exc}")
            else:
                lines.append("Screenshot failed: " + (err or out[:120] or "adb screencap returned nothing."))
        else:
            lines.append("The vision projector is off, so there is no picture. Tap using the reported coordinates.")
        return "\n".join(lines), image

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        action = (action or "click").lower()
        if action in {"move"}:
            if x is None or y is None:
                return "Move needs x and y."
            return f"Android has no pointer. Tap or swipe at {int(x)}, {int(y)} instead."
        if action in {"click", "double_click", "right_click"}:
            if x is None or y is None:
                return "Tap needs x and y."
            x, y = int(x), int(y)
            code, _, err = self._shell(f"input tap {x} {y}")
            if code != 0:
                return f"Tap failed: {err or 'unknown error'}"
            if action == "double_click":
                time.sleep(0.08)
                self._shell(f"input tap {x} {y}")
                return f"Double-tapped at {x}, {y}."
            return f"Tapped at {x}, {y}."
        if action == "scroll":
            if x is None or y is None:
                return "Scroll needs x and y as the start of a swipe."
            x, y = int(x), int(y)
            notches = max(-10, min(10, int(amount or -3)))
            distance = abs(notches) * 90
            end_y = y - distance if notches < 0 else y + distance
            code, _, err = self._shell(f"input swipe {x} {y} {x} {end_y} 280")
            if code != 0:
                return f"Swipe failed: {err or 'unknown error'}"
            return f"Swiped from {x},{y} to {x},{end_y}."
        return "Unknown mouse action. Use click, double_click, or scroll."

    def keyboard(self, text: str, keys: str) -> str:
        notes = []
        if text:
            escaped = (
                text[:500]
                .replace("\\", "\\\\")
                .replace(" ", "%s")
                .replace("'", "\\'")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("(", "\\(")
                .replace(")", "\\)")
                .replace("&", "\\&")
                .replace("<", "\\<")
                .replace(">", "\\>")
                .replace("|", "\\|")
                .replace(";", "\\;")
            )
            code, _, err = self._shell(f'input text "{escaped}"')
            if code != 0:
                return f"Typing failed: {err or 'unknown error'}"
            notes.append(f"Typed {min(len(text), 500)} characters")
        if keys:
            chord = keys.strip().lower()
            mapping = {
                "enter": "66",
                "return": "66",
                "backspace": "67",
                "tab": "61",
                "escape": "111",
                "esc": "111",
                "home": "3",
                "back": "4",
                "delete": "112",
                "del": "112",
                "space": "62",
                "up": "19",
                "down": "20",
                "left": "21",
                "right": "22",
            }
            code = mapping.get(chord)
            if code is None and "+" in chord:
                return "Android key chords are limited. Use enter, back, home, or a single key."
            if code is None and len(chord) == 1 and chord.isalnum():
                return "Send letters with the text field instead of keys."
            if code is None:
                return f"Unknown key: {keys}"
            result = self._shell(f"input keyevent {code}")
            if result[0] != 0:
                return f"Key failed: {result[2] or 'unknown error'}"
            notes.append(f"Pressed {keys}")
        if not notes:
            return "Nothing to type. Provide text, keys, or both."
        return ". ".join(notes) + "."

    def command(self, command: str, stop) -> str:
        command = command.strip()
        if not command:
            return "The command was empty."
        if len(command) > 2000:
            return "The command is too long."
        code, out, err = self._shell(command, timeout=45, stop=stop)
        if code == -1 and err == "cancelled":
            return "Command cancelled."
        if code == -1 and err == "timeout":
            return "Command timed out after 45 seconds."
        text = f"exit {code}\n{(out or '').strip()}\n{(err or '').strip()}".strip()
        return text[:6000]


WINDOWS_SYSTEM = """You operate a Windows computer. Reply with one JSON object and nothing else.
{"tool":"screen"}
{"tool":"mouse","action":"move","x":0,"y":0}
{"tool":"mouse","action":"click","x":0,"y":0}
{"tool":"mouse","action":"double_click","x":0,"y":0}
{"tool":"mouse","action":"right_click","x":0,"y":0}
{"tool":"mouse","action":"scroll","x":0,"y":0,"amount":-3}
{"tool":"keyboard","text":"hello","keys":"enter"}
{"tool":"command","command":"Get-Location"}
{"tool":"done","summary":"what you finished"}
Call screen first. x and y are pixels from the screen origin in the latest screen result. keys is a chord such as ctrl+s, alt+tab, or enter. command runs in PowerShell. When the task is finished, call done.
"""


LINUX_SYSTEM = """You operate a Linux computer. Reply with one JSON object and nothing else.
{"tool":"screen"}
{"tool":"mouse","action":"move","x":0,"y":0}
{"tool":"mouse","action":"click","x":0,"y":0}
{"tool":"mouse","action":"double_click","x":0,"y":0}
{"tool":"mouse","action":"right_click","x":0,"y":0}
{"tool":"mouse","action":"scroll","x":0,"y":0,"amount":-3}
{"tool":"keyboard","text":"hello","keys":"enter"}
{"tool":"command","command":"pwd"}
{"tool":"done","summary":"what you finished"}
Call screen first. x and y are pixels from the screen origin in the latest screen result. keys is a chord such as ctrl+s, alt+Tab, or enter. command runs in bash. When the task is finished, call done.
"""

ANDROID_SYSTEM = """You operate an Android phone. Reply with one JSON object and nothing else.
{"tool":"screen"}
{"tool":"mouse","action":"click","x":0,"y":0}
{"tool":"mouse","action":"double_click","x":0,"y":0}
{"tool":"mouse","action":"scroll","x":0,"y":0,"amount":-3}
{"tool":"keyboard","text":"hello","keys":"enter"}
{"tool":"command","command":"pm list packages | head"}
{"tool":"done","summary":"what you finished"}
Call screen first. click is a tap. scroll is a vertical swipe starting at x,y. keys may be enter, back, home, backspace, or arrows. command runs in adb shell. When the task is finished, call done.
"""


class DeviceManager:
    def __init__(self, computer, config_path: Path) -> None:
        self.computer = computer
        self.config_path = config_path
        self.lock = threading.Lock()
        self.local = LocalBackend(computer)
        self.remotes: dict[str, dict] = {}
        self.android_cache: list[dict] = []
        self.android_checked = 0.0
        self.remote_online: dict[str, bool] = {}
        self.remote_checked = 0.0
        self.selected = "local"
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        remotes = raw.get("remotes")
        if isinstance(remotes, list):
            for item in remotes:
                if not isinstance(item, dict):
                    continue
                device_id = str(item.get("id") or "").strip()
                url = str(item.get("url") or "").strip().rstrip("/")
                token = str(item.get("token") or "").strip()
                name = str(item.get("name") or "Companion").strip() or "Companion"
                if device_id and url and token:
                    self.remotes[device_id] = {"id": device_id, "name": name, "url": url, "token": token, "kind": str(item.get("kind") or "windows")}
        selected = str(raw.get("selected") or "local").strip()
        if selected:
            self.selected = selected

    def _save(self) -> None:
        payload = {
            "selected": self.selected,
            "remotes": [
                {"id": item["id"], "name": item["name"], "url": item["url"], "token": item["token"], "kind": item.get("kind") or "windows"}
                for item in self.remotes.values()
            ],
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def snapshot(self) -> dict:
        devices = self.list_devices()
        with self.lock:
            selected = self.selected
        if selected not in {item["id"] for item in devices}:
            selected = "local"
        return {
            "selected": selected,
            "devices": devices,
            "adb": find_adb() or "",
            "companion_port": COMPANION_PORT,
        }

    def list_devices(self) -> list[dict]:
        from .computer import platform_kind
        kind = platform_kind()
        detail = "Mouse, keyboard, screen, PowerShell" if kind == "windows" else "Mouse, keyboard, screen, bash"
        items = [{"id": "local", "name": "This PC", "kind": kind, "online": True, "detail": detail}]
        now = time.time()
        with self.lock:
            remotes = list(self.remotes.values())
            refresh_online = now - self.remote_checked > 8 or len(self.remote_online) != len(remotes)
        online_map: dict[str, bool] = {}
        if refresh_online:
            for item in remotes:
                online_map[item["id"]] = RemoteBackend(item["name"], item["url"], item["token"]).ping()
            with self.lock:
                self.remote_online = online_map
                self.remote_checked = now
        else:
            with self.lock:
                online_map = dict(self.remote_online)
        for item in remotes:
            online = online_map.get(item["id"], False)
            items.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "kind": item.get("kind") or "windows",
                    "online": online,
                    "detail": item["url"] + (" · online" if online else " · offline"),
                    "url": item["url"],
                }
            )
        for phone in self._android_devices():
            items.append(phone)
        return items

    def _android_devices(self) -> list[dict]:
        now = time.time()
        with self.lock:
            if now - self.android_checked < 4 and self.android_cache:
                return list(self.android_cache)
        adb = find_adb()
        found: list[dict] = []
        if adb:
            try:
                output = subprocess.check_output(
                    [adb, "devices", "-l"],
                    creationflags=CREATE_NO_WINDOW,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                )
            except (OSError, subprocess.SubprocessError):
                output = ""
            for line in output.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 2 or parts[1] != "device":
                    continue
                serial = parts[0]
                model = ""
                for part in parts[2:]:
                    if part.startswith("model:"):
                        model = part.split(":", 1)[1].replace("_", " ")
                label = model or serial
                found.append(
                    {
                        "id": "android:" + serial,
                        "name": f"Phone · {label}",
                        "kind": "android",
                        "online": True,
                        "detail": f"ADB {serial}",
                        "serial": serial,
                    }
                )
        with self.lock:
            self.android_cache = found
            self.android_checked = now
        return list(found)

    def select(self, device_id: str) -> dict:
        device_id = str(device_id or "local").strip() or "local"
        ids = {item["id"] for item in self.list_devices()}
        if device_id not in ids:
            return {"ok": False, "error": "That device is not available."}
        with self.lock:
            self.selected = device_id
            self._save()
        return {"ok": True, "selected": device_id}

    def add_remote(self, name: str, url: str, token: str, kind: str = "") -> dict:
        name = " ".join(str(name or "").split())[:60] or "Companion"
        url = str(url or "").strip().rstrip("/")
        token = str(token or "").strip()
        kind = str(kind or "windows").strip().lower() or "windows"
        if kind not in {"windows", "linux"}:
            kind = "windows"
        if not url.startswith("http://") and not url.startswith("https://"):
            return {"ok": False, "error": "Use an http:// or https:// companion URL."}
        if not token:
            return {"ok": False, "error": "The companion token is required."}
        backend = RemoteBackend(name, url, token, kind)
        if not backend.ping():
            return {"ok": False, "error": "Could not reach that companion. Start it on the other PC and check the URL."}
        device_id = "remote:" + secrets.token_hex(4)
        with self.lock:
            for existing in self.remotes.values():
                if existing["url"] == url:
                    existing["name"] = name
                    existing["token"] = token
                    existing["kind"] = backend.kind
                    self._save()
                    return {"ok": True, "id": existing["id"], "updated": True}
            self.remotes[device_id] = {"id": device_id, "name": name, "url": url, "token": token, "kind": backend.kind}
            self.selected = device_id
            self._save()
        return {"ok": True, "id": device_id}

    def register(self, name: str, url: str, token: str, kind: str = "") -> dict:
        return self.add_remote(name, url, token, kind)

    def remove(self, device_id: str) -> dict:
        device_id = str(device_id or "").strip()
        with self.lock:
            if device_id not in self.remotes:
                return {"ok": False, "error": "Only companion PCs can be removed here."}
            del self.remotes[device_id]
            if self.selected == device_id:
                self.selected = "local"
            self._save()
        return {"ok": True}

    def adb_connect(self, address: str) -> dict:
        address = str(address or "").strip()
        if not address:
            return {"ok": False, "error": "Enter the phone address, for example 100.x.x.x:5555."}
        adb = find_adb()
        if not adb:
            return {"ok": False, "error": "adb was not found. Install Android platform-tools and put adb on PATH."}
        try:
            output = subprocess.check_output(
                [adb, "connect", address],
                creationflags=CREATE_NO_WINDOW,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc)}
        with self.lock:
            self.android_checked = 0
        text = (output or "").strip()
        if "connected" in text.lower() or "already connected" in text.lower():
            return {"ok": True, "message": text}
        return {"ok": False, "error": text or "adb connect failed."}

    def adb_pair(self, address: str, code: str) -> dict:
        address = str(address or "").strip()
        code = str(code or "").strip()
        if not address or not code:
            return {"ok": False, "error": "Enter the pairing address and the 6-digit code from the phone."}
        adb = find_adb()
        if not adb:
            return {"ok": False, "error": "adb was not found. Install Android platform-tools and put adb on PATH."}
        try:
            proc = subprocess.run(
                [adb, "pair", address, code],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": str(exc)}
        text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if proc.returncode == 0 and "successfully" in text.lower():
            return {"ok": True, "message": text}
        return {"ok": False, "error": text or "adb pair failed."}

    def backend(self, device_id: str | None = None) -> DeviceBackend:
        with self.lock:
            chosen = device_id or self.selected or "local"
            remotes = dict(self.remotes)
        if chosen == "local" or not chosen:
            return self.local
        if chosen.startswith("remote:"):
            item = remotes.get(chosen)
            if not item:
                raise RuntimeError("That companion is no longer saved.")
            return RemoteBackend(item["name"], item["url"], item["token"], item.get("kind") or "windows")
        if chosen.startswith("android:"):
            serial = chosen.split(":", 1)[1]
            adb = find_adb()
            if not adb:
                raise RuntimeError("adb was not found.")
            label = next((item["name"] for item in self._android_devices() if item["id"] == chosen), f"Android {serial}")
            return AndroidBackend(serial, adb, label)
        raise RuntimeError("Unknown device.")

    def system_prompt(self, device_id: str | None = None) -> str:
        backend = self.backend(device_id)
        if backend.kind == "android":
            return ANDROID_SYSTEM
        if backend.kind == "linux":
            return LINUX_SYSTEM
        return WINDOWS_SYSTEM
