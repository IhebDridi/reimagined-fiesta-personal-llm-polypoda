"""Linux screen, mouse, keyboard, and shell control via xdotool/wmctrl."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path


def _run(args: list[str], timeout: int = 30, input_text: str | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "", f"Missing command: {args[0]}"
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _have(name: str) -> bool:
    return shutil.which(name) is not None


class LinuxComputer:
    def windows(self) -> list[dict]:
        found: list[dict] = []
        if _have("wmctrl"):
            code, out, _ = _run(["wmctrl", "-lG"], timeout=10)
            if code == 0:
                for line in out.splitlines():
                    parts = line.split(None, 7)
                    if len(parts) < 8:
                        continue
                    try:
                        x, y, width, height = (int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
                    except ValueError:
                        continue
                    title = parts[7].strip()
                    if not title or width < 40 or height < 40:
                        continue
                    found.append({"title": title[:140], "x": x, "y": y, "width": width, "height": height})
                    if len(found) >= 25:
                        break
                return found
        if _have("xdotool"):
            code, out, _ = _run(["xdotool", "search", "--name", ".*"], timeout=10)
            for wid in out.split():
                if len(found) >= 25:
                    break
                _c, title, _ = _run(["xdotool", "getwindowname", wid], timeout=5)
                title = title.strip()
                if not title:
                    continue
                _c, geo, _ = _run(["xdotool", "getwindowgeometry", "--shell", wid], timeout=5)
                vals = dict(re.findall(r"(\w+)=(\d+)", geo))
                try:
                    found.append(
                        {
                            "title": title[:140],
                            "x": int(vals.get("X", 0)),
                            "y": int(vals.get("Y", 0)),
                            "width": int(vals.get("WIDTH", 0)),
                            "height": int(vals.get("HEIGHT", 0)),
                        }
                    )
                except ValueError:
                    continue
        return found

    def capture(self) -> tuple[str, str]:
        raw = b""
        if _have("gnome-screenshot"):
            path = Path("/tmp") / f"polypoda-shot-{os.getpid()}.png"
            code, _, err = _run(["gnome-screenshot", "-f", str(path)], timeout=15)
            if code == 0 and path.is_file():
                raw = path.read_bytes()
                path.unlink(missing_ok=True)
            elif err:
                raise OSError(err.strip() or "gnome-screenshot failed")
        elif _have("scrot"):
            path = Path("/tmp") / f"polypoda-shot-{os.getpid()}.png"
            code, _, err = _run(["scrot", str(path)], timeout=15)
            if code == 0 and path.is_file():
                raw = path.read_bytes()
                path.unlink(missing_ok=True)
        elif _have("import"):
            proc = subprocess.run(
                ["import", "-window", "root", "png:-"],
                capture_output=True,
                timeout=15,
            )
            if proc.returncode == 0:
                raw = proc.stdout or b""
        else:
            try:
                from PIL import ImageGrab

                image = ImageGrab.grab()
                buffer = BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=60)
                image.thumbnail((1280, 800))
                buffer = BytesIO()
                image.convert("RGB").save(buffer, format="JPEG", quality=60)
                return base64.b64encode(buffer.getvalue()).decode("ascii"), f"{image.width}x{image.height}"
            except Exception as exc:
                raise OSError(
                    "Install gnome-screenshot, scrot, or ImageMagick (import) for screenshots."
                ) from exc
        if not raw:
            raise OSError("Screenshot returned no image.")
        from PIL import Image

        picture = Image.open(BytesIO(raw)).convert("RGB")
        picture.thumbnail((1280, 800))
        buffer = BytesIO()
        picture.save(buffer, format="JPEG", quality=60)
        return base64.b64encode(buffer.getvalue()).decode("ascii"), f"{picture.width}x{picture.height}"

    def observe(self, vision: bool) -> tuple[str, str]:
        code, out, _ = _run(["xdpyinfo"], timeout=5) if _have("xdpyinfo") else (1, "", "")
        size = "unknown"
        match = re.search(r"dimensions:\s+(\d+)x(\d+)", out)
        if match:
            size = f"{match.group(1)}x{match.group(2)}"
        lines = [f"Linux screen size {size}. Coordinates are pixels from the top-left corner."]
        windows = self.windows()
        if windows:
            lines.append("Windows:")
            for item in windows:
                lines.append(f"- {item['title']} | x={item['x']} y={item['y']} {item['width']}x{item['height']}")
        else:
            lines.append("No visible windows. Install wmctrl or xdotool for window titles.")
        image = ""
        if vision:
            try:
                image, shot = self.capture()
                lines.append(f"Screenshot attached ({shot}).")
            except OSError as exc:
                lines.append(f"Screenshot failed: {exc}")
        else:
            lines.append("The vision projector is off, so there is no picture. Click using the window coordinates.")
        missing = [name for name in ("xdotool",) if not _have(name)]
        if missing:
            lines.append("Missing tools: " + ", ".join(missing) + ". Install them for mouse and keyboard control.")
        return "\n".join(lines), image

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        if not _have("xdotool"):
            return "xdotool is not installed. Install it with your package manager (for example apt install xdotool)."
        action = (action or "click").lower()
        if action not in {"move", "click", "double_click", "right_click", "scroll"}:
            return "Unknown mouse action. Use move, click, double_click, right_click, or scroll."
        if action != "scroll" or x is not None:
            if x is None or y is None:
                return "This mouse action needs x and y."
            x, y = int(x), int(y)
            code, _, err = _run(["xdotool", "mousemove", "--sync", str(x), str(y)])
            if code != 0:
                return f"Move failed: {err or 'unknown error'}"
        if action == "move":
            return f"Moved the pointer to {x}, {y}."
        if action == "scroll":
            notches = max(-10, min(10, int(amount or -3)))
            button = "4" if notches > 0 else "5"
            for _ in range(abs(notches)):
                _run(["xdotool", "click", button])
            where = f" at {x}, {y}" if x is not None else ""
            return f"Scrolled {notches}{where}."
        if action == "right_click":
            _run(["xdotool", "click", "3"])
            return f"Right-clicked at {x}, {y}."
        clicks = "2" if action == "double_click" else "1"
        _run(["xdotool", "click", "--repeat", clicks, "--delay", "50", "1"])
        name = {"click": "Clicked", "double_click": "Double-clicked"}[action]
        return f"{name} at {x}, {y}."

    def keyboard(self, text: str, keys: str) -> str:
        if not _have("xdotool"):
            return "xdotool is not installed. Install it with your package manager (for example apt install xdotool)."
        notes = []
        if text:
            code, _, err = _run(["xdotool", "type", "--delay", "12", "--", text[:2000]])
            if code != 0:
                return f"Typing failed: {err or 'unknown error'}"
            notes.append(f"Typed {min(len(text), 2000)} characters")
        if keys:
            chord = keys.strip().lower().replace("control", "ctrl").replace(" ", "")
            mapping = {
                "enter": "Return",
                "return": "Return",
                "backspace": "BackSpace",
                "tab": "Tab",
                "escape": "Escape",
                "esc": "Escape",
                "space": "space",
                "delete": "Delete",
                "del": "Delete",
                "home": "Home",
                "end": "End",
                "left": "Left",
                "right": "Right",
                "up": "Up",
                "down": "Down",
                "win": "Super_L",
                "meta": "Super_L",
            }
            parts = [part for part in re.split(r"[+]+", chord) if part]
            xd = []
            for part in parts:
                if part in mapping:
                    xd.append(mapping[part])
                elif len(part) == 1:
                    xd.append(part)
                elif part.startswith("f") and part[1:].isdigit():
                    xd.append("F" + part[1:])
                else:
                    xd.append(part)
            code, _, err = _run(["xdotool", "key", "--", "+".join(xd)])
            if code != 0:
                return f"Key failed: {err or 'unknown error'}"
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
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.time() + 45
        while proc.poll() is None:
            if stop():
                proc.kill()
                return "Command cancelled."
            if time.time() > deadline:
                proc.kill()
                return "Command timed out after 45 seconds."
            time.sleep(0.1)
        out, err = proc.communicate()
        text = f"exit {proc.returncode}\n{(out or '').strip()}\n{(err or '').strip()}".strip()
        return text[:6000]
