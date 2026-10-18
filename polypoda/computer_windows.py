"""Windows screen, mouse, keyboard, and PowerShell control."""

from __future__ import annotations

import base64
import ctypes
import json
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from ctypes import wintypes
from io import BytesIO

from PIL import ImageGrab

CREATE_NO_WINDOW = 0x08000000
MAX_STEPS = 20
USER32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

VK = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "meta": 0x5B,
    **{f"f{index}": 0x6F + index for index in range(1, 13)},
}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]


USER32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
USER32.SendInput.restype = wintypes.UINT


def _send(*items: INPUT) -> None:
    count = len(items)
    array = (INPUT * count)(*items)
    sent = USER32.SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        raise OSError("Windows did not accept the input.")


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(x, y, data, flags, 0, 0))


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def screen_bounds() -> tuple[int, int, int, int]:
    return (
        USER32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        USER32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        USER32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        USER32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def parse_action(text: str) -> dict:
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("The model did not return an action.")
    data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict) or not data.get("tool"):
        raise ValueError("The model returned an unusable action.")
    return data


class WindowsComputer:
    def windows(self) -> list[dict]:
        found: list[dict] = []
        foreground = USER32.GetForegroundWindow()
        _left, _top, screen_w, screen_h = screen_bounds()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _lparam):
            if not USER32.IsWindowVisible(hwnd):
                return True
            length = USER32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            USER32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            USER32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 40 or height < 40:
                return True
            covers_screen = width >= screen_w - 8 and height >= screen_h - 8
            if covers_screen and hwnd != foreground:
                return True
            found.append({"title": title[:140], "x": rect.left, "y": rect.top, "width": width, "height": height})
            return len(found) < 25

        USER32.EnumWindows(visit, 0)
        return found

    def capture(self) -> tuple[str, str]:
        image = ImageGrab.grab(all_screens=True)
        image.thumbnail((1280, 800))
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=60)
        return base64.b64encode(buffer.getvalue()).decode("ascii"), f"{image.width}x{image.height}"

    def observe(self, vision: bool) -> tuple[str, str]:
        left, top, width, height = screen_bounds()
        lines = [f"Screen origin ({left}, {top}), size {width}x{height}."]
        windows = self.windows()
        if windows:
            lines.append("Windows:")
            for item in windows:
                lines.append(f"- {item['title']} | x={item['x']} y={item['y']} {item['width']}x{item['height']}")
        else:
            lines.append("No visible windows.")
        image = ""
        if vision:
            try:
                image, size = self.capture()
                lines.append(f"Screenshot attached ({size}).")
            except OSError as exc:
                lines.append(f"Screenshot failed: {exc}")
        else:
            lines.append("The vision projector is off, so there is no picture. Click using the window coordinates.")
        return "\n".join(lines), image

    def mouse(self, action: str, x: int | None, y: int | None, amount: int) -> str:
        action = (action or "click").lower()
        if action not in {"move", "click", "double_click", "right_click", "scroll"}:
            return "Unknown mouse action. Use move, click, double_click, right_click, or scroll."
        if action != "scroll" or x is not None:
            if x is None or y is None:
                return "This mouse action needs x and y."
            x, y = self._clamp(int(x), int(y))
            self._move(x, y)
        if action == "move":
            return f"Moved the pointer to {x}, {y}."
        if action == "scroll":
            if x is not None and y is not None:
                self._move(x, y)
            notches = max(-10, min(10, int(amount or -3)))
            _send(_mouse(MOUSEEVENTF_WHEEL, data=notches * 120))
            where = f" at {x}, {y}" if x is not None else ""
            return f"Scrolled {notches}{where}."
        clicks = 2 if action == "double_click" else 1
        down = MOUSEEVENTF_RIGHTDOWN if action == "right_click" else MOUSEEVENTF_LEFTDOWN
        up = MOUSEEVENTF_RIGHTUP if action == "right_click" else MOUSEEVENTF_LEFTUP
        for _ in range(clicks):
            _send(_mouse(down), _mouse(up))
            time.sleep(0.05)
        name = {"click": "Clicked", "double_click": "Double-clicked", "right_click": "Right-clicked"}[action]
        return f"{name} at {x}, {y}."

    def keyboard(self, text: str, keys: str) -> str:
        notes = []
        if text:
            self._type_text(text[:2000])
            notes.append(f"Typed {min(len(text), 2000)} characters")
        if keys:
            self._press_chord(keys)
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
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
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

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        left, top, width, height = screen_bounds()
        return (max(left, min(x, left + width - 1)), max(top, min(y, top + height - 1)))

    def _move(self, x: int, y: int) -> None:
        left, top, width, height = screen_bounds()
        absolute_x = int(round((x - left) * 65535 / max(width - 1, 1)))
        absolute_y = int(round((y - top) * 65535 / max(height - 1, 1)))
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, absolute_x, absolute_y))

    def _type_text(self, text: str) -> None:
        items = []
        for char in text:
            code = ord(char)
            items.append(_key(scan=code, flags=KEYEVENTF_UNICODE))
            items.append(_key(scan=code, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            if len(items) >= 40:
                _send(*items)
                items = []
        if items:
            _send(*items)

    def _press_chord(self, spec: str) -> None:
        names = [part.strip().lower() for part in re.split(r"[+\s]+", spec) if part.strip()]
        codes = [self._vk(name) for name in names]
        if any(code is None for code in codes):
            unknown = ", ".join(name for name, code in zip(names, codes) if code is None)
            raise ValueError(f"Unknown key: {unknown}")
        down = []
        for code in codes:
            flags = KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0
            down.append(_key(vk=code, flags=flags))
        up = []
        for code in reversed(codes):
            flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if code in EXTENDED else 0)
            up.append(_key(vk=code, flags=flags))
        _send(*down, *up)

    def _vk(self, name: str) -> int | None:
        if name in VK:
            return VK[name]
        if len(name) == 1 and name.isalnum():
            return ord(name.upper())
        return None
