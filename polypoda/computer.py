"""Platform computer control and the multi-device agent loop."""

from __future__ import annotations

import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections import deque

MAX_STEPS = 20


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


def platform_kind() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform or "unknown"


def create_computer():
    if sys.platform == "win32":
        from .computer_windows import WindowsComputer

        return WindowsComputer()
    if sys.platform.startswith("linux"):
        from .computer_linux import LinuxComputer

        return LinuxComputer()
    raise RuntimeError(f"Computer control is not supported on {sys.platform}.")


class Computer:
    """Compatibility wrapper around the platform-specific backend."""

    def __init__(self) -> None:
        self._backend = create_computer()

    def __getattr__(self, name: str):
        return getattr(self._backend, name)


class ComputerAgent:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.computer = Computer()
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.lines: deque[str] = deque(maxlen=80)
        self.job = self._idle()

    def snapshot(self) -> dict:
        with self.lock:
            data = dict(self.job)
            data["log"] = list(self.lines)
            return data

    def start(self, goal: str, max_steps: int = MAX_STEPS, device_id: str = "") -> dict:
        goal = " ".join(str(goal).split())[:2000]
        if not goal:
            return {"ok": False, "error": "Describe what the model should do."}
        with self.engine.lock:
            url = self.engine.server_url if self.engine.alive(self.engine.server_proc) else ""
            vision = bool(self.engine.config.get("use_mmproj"))
        if not url:
            return {"ok": False, "error": "Start the server before giving the model control of a device."}
        try:
            limit = int(max_steps)
        except (TypeError, ValueError):
            limit = MAX_STEPS
        if limit < 0:
            limit = 0
        elif limit > 100000:
            limit = 100000
        fleet = getattr(self.engine, "fleet", None)
        if fleet is None:
            return {"ok": False, "error": "Device manager is not ready."}
        chosen = str(device_id or "").strip()
        if chosen:
            selected = fleet.select(chosen)
            if not selected.get("ok"):
                return selected
        else:
            chosen = fleet.snapshot().get("selected") or "local"
        try:
            backend = fleet.backend(chosen)
            prompt = fleet.system_prompt(chosen)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        with self.lock:
            if self.job.get("active"):
                return {"ok": False, "error": "The model is already using a device."}
            self.cancel.clear()
            self.lines.clear()
            self.lines.append(f"Device: {backend.label}")
            self.lines.append(f"Task: {goal}")
            self.lines.append("Steps: unlimited" if limit == 0 else f"Steps: {limit}")
            self.job = {
                "active": True,
                "status": "running",
                "goal": goal,
                "device_id": chosen,
                "device_name": backend.label,
                "step": 0,
                "max_steps": limit,
                "error": "",
            }
        threading.Thread(target=self._run, args=(url, goal, vision, limit, backend, prompt), daemon=True).start()
        return {"ok": True}

    def stop(self) -> dict:
        self.cancel.set()
        return {"ok": True}

    def _run(self, url: str, goal: str, vision: bool, max_steps: int, backend, prompt: str) -> None:
        messages: list[dict] = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": goal},
        ]
        try:
            step = 0
            while True:
                if self.cancel.is_set():
                    self._finish("stopped", "Stopped.")
                    return
                step += 1
                if max_steps and step > max_steps:
                    self._note("Stopped after the step limit.")
                    self._finish("done", "Reached the step limit.")
                    return
                self._set(step=step)
                reply = self._chat(url, self._trim(messages))
                self._note("Model: " + " ".join(reply.split())[:360])
                messages.append({"role": "assistant", "content": reply})
                try:
                    action = parse_action(reply)
                except (ValueError, json.JSONDecodeError):
                    messages.append(
                        {
                            "role": "user",
                            "content": "Reply with one JSON object only, using the tools from the instructions.",
                        }
                    )
                    continue
                tool = str(action.get("tool") or "").lower()
                if tool == "done":
                    summary = str(action.get("summary") or "Finished.").strip()
                    self._note("Done: " + summary[:500])
                    self._finish("done", "")
                    return
                result, image = self._act(action, vision, backend)
                self._note(result.splitlines()[0][:240] if result else "No result.")
                if image:
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": result},
                                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}},
                            ],
                        }
                    )
                else:
                    messages.append({"role": "user", "content": result})
        except Exception as exc:
            message = self._explain(exc)
            self._note("Stopped: " + message)
            self._finish("error", message)

    def _act(self, action: dict, vision: bool, backend) -> tuple[str, str]:
        tool = str(action.get("tool") or "").lower()
        if self.cancel.is_set():
            return "Stopped.", ""
        if tool == "screen":
            return backend.observe(vision)
        if tool == "mouse":
            return (
                backend.mouse(
                    str(action.get("action") or "click"),
                    action.get("x"),
                    action.get("y"),
                    int(action.get("amount") or 0),
                ),
                "",
            )
        if tool == "keyboard":
            try:
                return backend.keyboard(str(action.get("text") or ""), str(action.get("keys") or "")), ""
            except ValueError as exc:
                return str(exc), ""
        if tool == "command":
            command = str(action.get("command") or "")
            self._note("$ " + command[:240])
            return backend.command(command, self.cancel.is_set), ""
        return "Unknown tool. Use screen, mouse, keyboard, command, or done.", ""

    def _chat(self, url: str, messages: list[dict]) -> str:
        payload = json.dumps({"messages": messages, "temperature": 0.2, "stream": False}).encode("utf-8")
        request = urllib.request.Request(
            url.rstrip("/") + "/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if messages and isinstance(messages[-1].get("content"), list):
                messages[-1] = {
                    "role": "user",
                    "content": "The screenshot could not be read. Continue from the text description.",
                }
                return self._chat(url, messages)
            raise RuntimeError(detail or f"The model server returned HTTP {exc.code}.") from exc
        message = body["choices"][0]["message"]
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)

    def _trim(self, messages: list[dict]) -> list[dict]:
        head = messages[:2]
        tail = []
        for index, message in enumerate(messages[2:]):
            content = message.get("content")
            if isinstance(content, list) and index != len(messages[2:]) - 1:
                text = " ".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
                tail.append({"role": message["role"], "content": text or "Earlier screenshot omitted."})
            else:
                tail.append(message)
        return head + tail[-8:]

    def _note(self, text: str) -> None:
        with self.lock:
            self.lines.append(text)
        self.engine.log(text)

    def _set(self, **kwargs) -> None:
        with self.lock:
            self.job.update(kwargs)

    def _finish(self, status: str, error: str) -> None:
        with self.lock:
            self.job.update({"active": False, "status": status, "error": error})

    def _idle(self) -> dict:
        return {
            "active": False,
            "status": "idle",
            "goal": "",
            "device_id": "local",
            "device_name": "This PC",
            "step": 0,
            "max_steps": MAX_STEPS,
            "error": "",
        }

    def _explain(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.URLError):
            return "Could not reach the model server."
        if isinstance(exc, TimeoutError):
            return "The model took too long to answer."
        text = str(exc).strip() or exc.__class__.__name__
        return text[:300]
