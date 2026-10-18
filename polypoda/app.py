"""Local web app that configures and launches llama.cpp (Polypoda)."""

from __future__ import annotations

import json
import os
import queue
import shlex
import socket
import subprocess
import threading
import traceback
import urllib.parse
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .computer import ComputerAgent
from .devices import DeviceManager
from .hf import HuggingFace

APP_NAME = "llama.cpp"
PKG = Path(__file__).resolve().parent
WEB = PKG / "web"
ICON = PKG / "llama.png"


def _project_root() -> Path:
    here = PKG.parent
    if (here / "pyproject.toml").is_file() or (here / "runtime").is_dir():
        return here
    return Path.cwd()


ROOT = _project_root()
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "LlamaCppLauncher"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "launcher.log"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_CONSOLE = 0x00000010
HOST = "127.0.0.1"
BIND_HOST = "0.0.0.0"
LAUNCH_PORT = 0
_ADDRESS_LOCK = threading.Lock()
_ADDRESSES: list[str] = []


def _ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def read_addresses() -> list[str]:
    try:
        output = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } | Select-Object -ExpandProperty IPAddress",
            ],
            creationflags=CREATE_NO_WINDOW,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    seen: set[str] = set()
    for line in output.splitlines():
        ip = line.strip()
        if _ipv4(ip) and ip not in seen:
            seen.add(ip)
            found.append(ip)
    return found


def watch_addresses() -> None:
    global _ADDRESSES
    while True:
        found = read_addresses()
        with _ADDRESS_LOCK:
            _ADDRESSES = found
        threading.Event().wait(20)


def network_links() -> list[dict]:
    if not LAUNCH_PORT:
        return []
    with _ADDRESS_LOCK:
        addresses = list(_ADDRESSES)
    links = []
    for ip in addresses:
        parts = [int(part) for part in ip.split(".")]
        tailscale = parts[0] == 100 and 64 <= parts[1] <= 127
        links.append({"label": f"Tailscale {ip}" if tailscale else ip, "url": f"http://{ip}:{LAUNCH_PORT}"})
    links.sort(key=lambda item: (not item["label"].startswith("Tailscale"), item["label"]))
    return links


def allow_network() -> str:
    show = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=llama.cpp launcher"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if show.returncode == 0 and "llama.cpp launcher" in (show.stdout or ""):
        return ""
    added = subprocess.run(
        [
            "netsh", "advfirewall", "firewall", "add", "rule",
            "name=llama.cpp launcher", "dir=in", "action=allow", "protocol=TCP",
            "localport=47321-47339", "profile=any",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    if added.returncode != 0:
        return "Windows Firewall may block other devices. Allow TCP ports 47321-47339 if a phone or laptop cannot connect."
    return "Allowed this app through Windows Firewall on TCP ports 47321-47339."

DEFAULTS = {
    "model": r"C:\Users\waben\.lmstudio\models\lmstudio-community\gemma-4-E4B-it-GGUF\gemma-4-E4B-it-Q4_K_M.gguf",
    "use_mmproj": False,
    "mmproj": "",
    "runtime": "",
    "device": "",
    "gpu_layers": "all",
    "fit": "on",
    "ctx": "0",
    "temp": "0.8",
    "top_p": "0.95",
    "top_k": "40",
    "threads": "",
    "flash_attn": "auto",
    "host": "127.0.0.1",
    "port": "8080",
    "system": "",
    "extra": "",
}


def load_config() -> dict:
    data = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({key: saved[key] for key in DEFAULTS if key in saved})
    except (OSError, json.JSONDecodeError):
        pass
    return data


def save_config(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def exe_in(folder: Path, name: str) -> Path | None:
    direct = folder / name
    if direct.is_file():
        return direct
    matches = [path for path in folder.rglob(name) if path.is_file()]
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def winget_runtime() -> Path | None:
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if not packages.is_dir():
        return None
    hits = [path.parent for path in packages.glob("ggml.llamacpp*/llama-server.exe")]
    if not hits:
        return None
    hits.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return hits[0]


def discover_models() -> list[dict]:
    roots = [
        Path.home() / ".lmstudio" / "models",
        Path.home() / "models",
        ROOT / "models",
        ROOT.parent / "models",
    ]
    found = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.gguf"):
            key = str(path).lower()
            if key in seen or "mmproj" in path.name.lower():
                continue
            seen.add(key)
            size = path.stat().st_size
            found.append(
                {
                    "path": str(path),
                    "label": f"{path.parent.name} / {path.name}  ({size / (1024 ** 3):.1f} GB)",
                    "bytes": size,
                }
            )
    found.sort(key=lambda item: Path(item["path"]).name.lower())
    return found


def sibling_mmproj(model: Path) -> Path | None:
    if not model.is_file():
        return None
    names = list(model.parent.glob("mmproj*.gguf")) + list(model.parent.glob("*mmproj*.gguf"))
    unique = []
    seen: set[str] = set()
    for path in names:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique[0] if unique else None


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), 0.4):
            return True
    except OSError:
        return False


def browser_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host


def normalize(raw: dict) -> dict:
    data = dict(DEFAULTS)
    for key in DEFAULTS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "use_mmproj":
            data[key] = bool(value)
        else:
            data[key] = str(value).strip()
    if data["gpu_layers"] == "":
        data["gpu_layers"] = "all"
    if data["fit"] not in {"on", "off"}:
        data["fit"] = "on"
    if data["flash_attn"] not in {"auto", "on", "off"}:
        data["flash_attn"] = "auto"
    if data["ctx"] == "":
        data["ctx"] = "0"
    if data["host"] == "":
        data["host"] = "127.0.0.1"
    if data["port"] == "":
        data["port"] = "8080"
    return data


def validate(config: dict, server_exe: Path | None, cli_exe: Path | None, need_server: bool) -> str | None:
    exe = server_exe if need_server else cli_exe
    if exe is None or not exe.is_file():
        return "llama.cpp binaries were not found."
    if not Path(config["model"]).is_file():
        return "Choose a GGUF model file that exists."
    if config["use_mmproj"] and not Path(config["mmproj"]).is_file():
        return "The vision projector file does not exist."
    try:
        ctx = int(config["ctx"])
        port = int(config["port"])
        int(config["top_k"])
        float(config["temp"])
        float(config["top_p"])
        if config["threads"]:
            int(config["threads"])
    except ValueError:
        return "Context, port, temperature, top-p, top-k, and threads must be numbers."
    if ctx < 0 or not 1 <= port <= 65535:
        return "Context must be zero or more, and the port must be from 1 to 65535."
    return None


def common_args(config: dict) -> list[str]:
    args = [
        "-m", config["model"],
        "-ngl", config["gpu_layers"],
        "--fit", config["fit"],
        "-c", config["ctx"],
        "--temp", config["temp"],
        "--top-p", config["top_p"],
        "--top-k", config["top_k"],
        "-fa", config["flash_attn"],
        "--jinja",
    ]
    if config["device"]:
        args.extend(["-dev", config["device"]])
    if config["threads"]:
        args.extend(["-t", config["threads"]])
    if config["system"]:
        args.extend(["-sys", config["system"]])
    if config["use_mmproj"] and config["mmproj"]:
        args.extend(["--mmproj", config["mmproj"]])
    if config["extra"]:
        args.extend(shlex.split(config["extra"], posix=False))
    return args


def run_capture(exe: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            [str(exe), *args],
            cwd=str(exe.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    return (completed.stdout or "") + (completed.stderr or "")


def read_version(cli: Path) -> str:
    text = run_capture(cli, ["--version"])
    for line in text.splitlines():
        lowered = line.lower()
        if "version:" in lowered or lowered.startswith("build"):
            return line.strip()
    return "llama.cpp"


def read_devices(cli: Path) -> list[dict]:
    text = run_capture(cli, ["--list-devices"])
    devices = []
    for line in text.splitlines():
        stripped = line.strip()
        if ":" not in stripped or stripped.lower().startswith("available"):
            continue
        if "load_backend" in stripped or stripped.startswith("-"):
            continue
        device_id = stripped.split(":", 1)[0].strip()
        if not device_id or " " in device_id:
            continue
        if not device_id.lower().startswith(("cuda", "vulkan", "cpu", "gpu", "rpc", "metal", "sycl", "rocm")):
            continue
        devices.append({"id": device_id, "label": stripped})
    return devices


def resolve_runtime(saved: str) -> Path | None:
    candidates = []
    if saved.strip():
        candidates.append(Path(saved.strip()))
    candidates.append(ROOT / "runtime")
    winget = winget_runtime()
    if winget:
        candidates.append(winget)
    for folder in candidates:
        exe = exe_in(folder, "llama-server.exe")
        if exe:
            return exe.parent
    return None


class Dialogs:
    def __init__(self) -> None:
        self.requests: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="dialogs", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()

        def pump() -> None:
            try:
                while True:
                    kind, options, reply = self.requests.get_nowait()
                    try:
                        if kind == "dir":
                            path = filedialog.askdirectory(parent=root, **options) or ""
                        else:
                            path = filedialog.askopenfilename(parent=root, **options) or ""
                        reply.put({"ok": True, "path": path})
                    except Exception as exc:
                        reply.put({"ok": False, "error": str(exc), "path": ""})
            except queue.Empty:
                pass
            root.after(80, pump)

        root.after(80, pump)
        root.mainloop()

    def ask(self, kind: str, options: dict) -> dict:
        reply: queue.Queue = queue.Queue()
        self.requests.put((kind, options, reply))
        try:
            return reply.get(timeout=300)
        except queue.Empty:
            return {"ok": False, "error": "The file dialog timed out.", "path": ""}


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config = load_config()
        self.logs: deque[str] = deque(maxlen=400)
        self.server_proc: subprocess.Popen | None = None
        self.chat_proc: subprocess.Popen | None = None
        self.version = "Looking up llama.cpp"
        self.runtime: Path | None = None
        self.server_exe: Path | None = None
        self.cli_exe: Path | None = None
        self.devices: list[dict] = []
        self.models: list[dict] = []
        self.status = "Starting"
        self.server_url = ""
        self.opened_browser = False
        self.dialogs = Dialogs()
        self.hf = HuggingFace(self, ROOT / "models")
        self.agent = ComputerAgent(self)
        self.fleet = DeviceManager(self.agent.computer, CONFIG_DIR / "devices.json")
        threading.Thread(target=self.probe, daemon=True).start()
        threading.Thread(target=self.scan_models, daemon=True).start()

    def log(self, text: str) -> None:
        with self.lock:
            for line in text.splitlines() or [""]:
                self.logs.append(line)

    def alive(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def suggestion(self) -> str:
        found = sibling_mmproj(Path(self.config.get("model", "")))
        if found and not self.config.get("mmproj"):
            return str(found)
        return self.config.get("mmproj", "")

    def state(self) -> dict:
        fleet = self.fleet.snapshot()
        with self.lock:
            running = self.alive(self.server_proc)
            if running and self.server_url:
                status = f"Serving at {self.server_url}"
            elif running:
                status = "Loading model"
            else:
                status = self.status
            return {
                "config": dict(self.config),
                "version": self.version,
                "runtime": str(self.runtime or ""),
                "devices": list(self.devices),
                "models": list(self.models),
                "suggestion": self.suggestion(),
                "status": status,
                "server_url": self.server_url if running else "",
                "server": {"running": running},
                "log": list(self.logs),
                "download": self.hf.snapshot(),
                "agent": self.agent.snapshot(),
                "fleet": fleet,
                "network": network_links(),
            }

    def update_config(self, raw: dict) -> dict:
        config = normalize(raw)
        suggestion = sibling_mmproj(Path(config["model"]))
        if config["use_mmproj"] and not config["mmproj"] and suggestion:
            config["mmproj"] = str(suggestion)
        with self.lock:
            self.config = config
        save_config(config)
        return config

    def probe(self) -> None:
        saved = self.config.get("runtime", "")
        folder = resolve_runtime(str(saved))
        server = exe_in(folder, "llama-server.exe") if folder else None
        cli = exe_in(folder, "llama-cli.exe") if folder else None
        version = "llama.cpp was not found."
        devices: list[dict] = []
        if cli:
            version = read_version(cli)
            devices = read_devices(cli)
        with self.lock:
            self.runtime = folder
            self.server_exe = server
            self.cli_exe = cli
            self.version = version
            self.devices = devices
            if devices and not self.config.get("device"):
                gpu = next((item["id"] for item in devices if "cpu" not in item["label"].lower()), devices[0]["id"])
                self.config["device"] = gpu
                save_config(self.config)
            if not self.alive(self.server_proc):
                self.status = "Ready" if server else "llama.cpp was not found"
        if devices:
            self.log("Devices: " + "; ".join(item["label"] for item in devices))
        elif server:
            self.log(f"Runtime ready at {folder}")
        else:
            self.log("Could not find llama-server.exe.")

    def scan_models(self) -> None:
        models = discover_models()
        with self.lock:
            self.models = models
        self.log(f"Found {len(models)} GGUF model(s).")

    def adopt_download(self, path: Path, kind: str) -> None:
        with self.lock:
            if kind == "mmproj":
                self.config["mmproj"] = str(path)
                self.config["use_mmproj"] = True
            else:
                self.config["model"] = str(path)
            save_config(self.config)
        self.scan_models()

    def browse(self, kind: str, current: str) -> dict:
        path = Path(current) if current else Path(self.config.get("model", ""))
        initial = str(path if path.is_dir() else path.parent if path.parent.is_dir() else Path.home())
        if kind == "runtime":
            result = self.dialogs.ask("dir", {"title": "Folder that contains llama-server.exe", "initialdir": str(self.runtime or ROOT)})
            if result.get("path"):
                folder = Path(result["path"])
                if exe_in(folder, "llama-server.exe") is None:
                    return {"ok": False, "error": "That folder does not contain llama-server.exe.", "path": ""}
                with self.lock:
                    self.config["runtime"] = str(folder)
                    save_config(self.config)
                self.probe()
            return result
        title = "Choose a GGUF model" if kind == "model" else "Choose a vision projector"
        result = self.dialogs.ask(
            "file",
            {
                "title": title,
                "filetypes": [("GGUF", "*.gguf"), ("All files", "*.*")],
                "initialdir": initial,
            },
        )
        if kind == "model" and result.get("path"):
            found = sibling_mmproj(Path(result["path"]))
            with self.lock:
                if found and not self.config.get("mmproj"):
                    self.config["mmproj"] = str(found)
            self.log(f"Selected {Path(result['path']).name}")
        return result

    def start_server(self, raw: dict) -> dict:
        config = self.update_config(raw)
        with self.lock:
            if self.alive(self.server_proc):
                return {"ok": False, "error": "The server is already running."}
            server_exe = self.server_exe
            cli_exe = self.cli_exe
        error = validate(config, server_exe, cli_exe, True)
        if error:
            return {"ok": False, "error": error}
        host = config["host"]
        port = int(config["port"])
        probe = browser_host(host)
        if port_is_open(probe, port):
            return {"ok": False, "error": f"Port {port} is already in use."}
        cmd = [str(server_exe), *common_args(config), "--host", host, "--port", str(port)]
        self.log("$ " + subprocess.list2cmdline(cmd))
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(server_exe.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            return {"ok": False, "error": f"Could not start llama-server: {exc}"}
        with self.lock:
            self.server_proc = proc
            self.server_url = ""
            self.opened_browser = False
            self.status = f"Loading model on port {port}"
        threading.Thread(target=self._read_server, args=(proc,), daemon=True).start()
        threading.Thread(target=self._wait_for_port, args=(proc, probe, port), daemon=True).start()
        return {"ok": True}

    def _read_server(self, proc: subprocess.Popen) -> None:
        if proc.stdout is not None:
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
        code = proc.poll()
        self.log(f"Server exited ({code}).")
        with self.lock:
            if self.server_proc is proc:
                self.status = "Server stopped"
                self.server_url = ""

    def _wait_for_port(self, proc: subprocess.Popen, host: str, port: int) -> None:
        for _ in range(240):
            if proc.poll() is not None:
                return
            if port_is_open(host, port):
                url = f"http://{browser_host(host)}:{port}"
                with self.lock:
                    if self.server_proc is not proc or self.opened_browser:
                        return
                    self.opened_browser = True
                    self.server_url = url
                    self.status = f"Serving at {url}"
                webbrowser.open(url)
                return
            threading.Event().wait(0.5)

    def start_chat(self, raw: dict) -> dict:
        config = self.update_config(raw)
        with self.lock:
            if self.alive(self.chat_proc):
                return {"ok": False, "error": "A terminal chat is already open."}
            cli_exe = self.cli_exe
            server_exe = self.server_exe
        error = validate(config, server_exe, cli_exe, False)
        if error:
            return {"ok": False, "error": error}
        cmd = [str(cli_exe), *common_args(config), "--conversation"]
        self.log("$ " + subprocess.list2cmdline(cmd))
        try:
            proc = subprocess.Popen(cmd, cwd=str(cli_exe.parent), creationflags=CREATE_NEW_CONSOLE)
        except OSError as exc:
            return {"ok": False, "error": f"Could not start llama-cli: {exc}"}
        with self.lock:
            self.chat_proc = proc
            self.status = "Chat opened in a new terminal"
        return {"ok": True}

    def stop(self) -> dict:
        stopped = False
        with self.lock:
            procs = [self.server_proc, self.chat_proc]
        for proc in procs:
            if self.alive(proc):
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )
                stopped = True
        with self.lock:
            self.server_proc = None
            self.chat_proc = None
            self.server_url = ""
            self.status = "Stopped" if stopped else "Nothing is running"
        if stopped:
            self.log("Stopped llama.cpp.")
        return {"ok": True, "stopped": stopped}


ENGINE: Engine | None = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > 1_000_000:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self._json({"app": APP_NAME})
            return
        if path == "/api/state":
            self._json(ENGINE.state() if ENGINE else {})
            return
        if path == "/icon.png" and ICON.is_file():
            self._send(200, ICON.read_bytes(), "image/png")
            return
        file_map = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
        name = file_map.get(path)
        if not name:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        target = WEB / name
        kind = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
        self._send(200, target.read_bytes(), kind)

    def do_POST(self) -> None:
        if ENGINE is None:
            self._json({"ok": False, "error": "Launcher is not ready."}, 500)
            return
        path = urllib.parse.urlparse(self.path).path
        data = self._body()
        if path == "/api/config":
            ENGINE.update_config(data)
            self._json({"ok": True})
        elif path == "/api/start":
            self._json(ENGINE.start_server(data))
        elif path == "/api/chat":
            self._json(ENGINE.start_chat(data))
        elif path == "/api/stop":
            self._json(ENGINE.stop())
        elif path == "/api/rescan":
            ENGINE.scan_models()
            ENGINE.probe()
            self._json({"ok": True})
        elif path == "/api/browse":
            self._json(ENGINE.browse(str(data.get("kind") or "model"), str(data.get("current") or "")))
        elif path == "/api/hf/search":
            self._json(ENGINE.hf.search(str(data.get("query") or ""), str(data.get("mode") or "model")))
        elif path == "/api/hf/files":
            self._json(ENGINE.hf.files(str(data.get("repo") or ""), str(data.get("mode") or "model")))
        elif path == "/api/hf/download":
            self._json(ENGINE.hf.download(str(data.get("repo") or ""), data.get("files") or [], str(data.get("label") or "")))
        elif path == "/api/hf/cancel":
            self._json(ENGINE.hf.cancel())
        elif path == "/api/agent/start":
            self._json(
                ENGINE.agent.start(
                    str(data.get("goal") or ""),
                    data.get("max_steps", 20),
                    str(data.get("device_id") or ""),
                )
            )
        elif path == "/api/agent/stop":
            self._json(ENGINE.agent.stop())
        elif path == "/api/devices/select":
            self._json(ENGINE.fleet.select(str(data.get("id") or "local")))
        elif path == "/api/devices/add":
            self._json(
                ENGINE.fleet.add_remote(
                    str(data.get("name") or ""),
                    str(data.get("url") or ""),
                    str(data.get("token") or ""),
                    str(data.get("kind") or ""),
                )
            )
        elif path == "/api/devices/register":
            self._json(
                ENGINE.fleet.register(
                    str(data.get("name") or ""),
                    str(data.get("url") or ""),
                    str(data.get("token") or ""),
                    str(data.get("kind") or ""),
                )
            )
        elif path == "/api/devices/remove":
            self._json(ENGINE.fleet.remove(str(data.get("id") or "")))
        elif path == "/api/devices/adb-pair":
            self._json(ENGINE.fleet.adb_pair(str(data.get("address") or ""), str(data.get("code") or "")))
        elif path == "/api/devices/adb-connect":
            self._json(ENGINE.fleet.adb_connect(str(data.get("address") or "")))
        else:
            self._json({"ok": False, "error": "Unknown request."}, 404)


def our_server(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), 0.4) as sock:
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            data = sock.recv(1024)
        return b"llama.cpp" in data
    except OSError:
        return False


def claim_port() -> tuple[int, bool]:
    for port in range(47321, 47340):
        if our_server(port):
            return port, True
        if not port_is_open(HOST, port):
            return port, False
    raise SystemExit("No free port for the llama.cpp launcher.")


def note_error(text: str) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass


def main() -> None:
    global ENGINE
    try:
        port, already = claim_port()
        url = f"http://{HOST}:{port}"
        if already:
            webbrowser.open(url)
            return
        global LAUNCH_PORT
        LAUNCH_PORT = port
        with _ADDRESS_LOCK:
            _ADDRESSES[:] = read_addresses()
        threading.Thread(target=watch_addresses, daemon=True).start()
        ENGINE = Engine()
        notice = allow_network()
        if notice:
            ENGINE.log(notice)
        for link in network_links():
            ENGINE.log("Reachable at " + link["url"])
        server = ThreadingHTTPServer((BIND_HOST, port), Handler)
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except Exception:
        note_error(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
