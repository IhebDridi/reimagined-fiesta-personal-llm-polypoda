"""Search Hugging Face for GGUF files and download them into the models folder."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://huggingface.co"
USER_AGENT = "llama.cpp-launcher"
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SHARD_RE = re.compile(r"^(?P<base>.+)-(?P<idx>\d{5})-of-(?P<count>\d{5})\.gguf$", re.IGNORECASE)
QUANT_RE = re.compile(r"(IQ\d(?:_[A-Z0-9]+)+|Q\d(?:_[A-Z0-9]+)*|BF16|F16|F32)", re.IGNORECASE)
PREFERRED = ("q4_k_m", "q5_k_m", "q6_k", "q4_k_s", "q8_0", "q3_k_m")


def format_bytes(size: int) -> str:
    if size >= 1024 ** 3:
        return f"{size / (1024 ** 3):.2f} GB"
    if size >= 1024 ** 2:
        return f"{size / (1024 ** 2):.0f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def compact_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


class HuggingFace:
    def __init__(self, engine, models_dir: Path) -> None:
        self.engine = engine
        self.models_dir = models_dir
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.job = self._idle()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.job)

    def search(self, query: str, mode: str = "model") -> dict:
        mode = "projector" if str(mode) == "projector" else "model"
        query = " ".join(str(query).split())[:120]
        if mode == "projector" and "mmproj" not in query.lower():
            query = f"{query} mmproj".strip()
        params = urllib.parse.urlencode(
            {
                "search": query,
                "filter": "gguf",
                "sort": "downloads",
                "direction": "-1",
                "limit": "20" if mode == "projector" else "12",
            }
        )
        try:
            payload = self._get_json(f"{API}/api/models?{params}")
        except Exception as exc:
            return {"ok": False, "error": self._explain(exc), "models": []}
        if not isinstance(payload, list):
            return {"ok": False, "error": "Hugging Face returned an unexpected response.", "models": []}
        models = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            repo = str(item.get("id") or "")
            if not REPO_RE.match(repo):
                continue
            downloads = int(item.get("downloads") or 0)
            likes = int(item.get("likes") or 0)
            models.append(
                {
                    "repo": repo,
                    "downloads": downloads,
                    "likes": likes,
                    "detail": f"{compact_count(downloads)} downloads · {compact_count(likes)} likes",
                }
            )
        self._attach_sizes(models, mode)
        if mode == "projector":
            models = [model for model in models if model.get("match")]
        return {"ok": True, "models": models[:12]}

    def _attach_sizes(self, models: list[dict], mode: str) -> None:
        if not models:
            return
        summaries: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._size_summary, model["repo"], mode): model["repo"] for model in models}
            try:
                for future in as_completed(futures, timeout=20):
                    repo = futures[future]
                    try:
                        summaries[repo] = future.result()
                    except Exception:
                        summaries[repo] = {"label": "size unknown", "match": mode != "projector"}
            except TimeoutError:
                for future in futures:
                    future.cancel()
        for model in models:
            summary = summaries.get(model["repo"]) or {"label": "size unknown", "match": mode != "projector"}
            model["size_label"] = summary["label"]
            model["match"] = summary["match"]

    def _size_summary(self, repo: str, mode: str) -> dict:
        payload = self._get_json(f"{API}/api/models/{urllib.parse.quote(repo, safe='/')}?blobs=true", timeout=12)
        siblings = payload.get("siblings") if isinstance(payload, dict) else None
        if not isinstance(siblings, list):
            return {"label": "size unknown", "match": mode != "projector"}
        raw = []
        for item in siblings:
            if not isinstance(item, dict):
                continue
            name = str(item.get("rfilename") or "").replace("\\", "/")
            if not name.lower().endswith(".gguf"):
                continue
            lfs = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
            size = int(item.get("size") or lfs.get("size") or 0)
            try:
                self._dest(repo, name)
            except ValueError:
                continue
            raw.append({"path": name, "size": size})
        groups = [item for item in self._group(repo, raw) if item["size"] > 0]
        projectors = [item for item in groups if item["kind"] == "mmproj"]
        weights = [item for item in groups if item["kind"] == "model"]
        if mode == "projector":
            if not projectors:
                return {"label": "No projector file", "match": False}
            sizes = [item["size"] for item in projectors]
            label = format_bytes(sizes[0]) if len(set(sizes)) == 1 else f"{format_bytes(min(sizes))}–{format_bytes(max(sizes))}"
            if len(projectors) > 1:
                label += f" · {len(projectors)} files"
            return {"label": label, "match": True}
        chosen = weights or groups
        if not chosen:
            return {"label": "size unknown", "match": False}
        recommended = next((item for item in chosen if item["recommended"]), chosen[0])
        quant = QUANT_RE.findall(recommended["label"])
        size = format_bytes(recommended["size"])
        head = f"{quant[-1].upper()} · {size}" if quant else size
        smallest = min(item["size"] for item in chosen)
        largest = max(item["size"] for item in chosen)
        if len(chosen) > 1 and smallest != largest:
            head = f"{head} · {format_bytes(smallest)}–{format_bytes(largest)}"
        return {"label": head, "match": bool(weights)}

    def files(self, repo: str, mode: str = "model") -> dict:
        try:
            repo = self._repo(repo)
            payload = self._get_json(f"{API}/api/models/{repo}/tree/main?recursive=1")
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "files": []}
        except Exception as exc:
            return {"ok": False, "error": self._explain(exc), "files": []}
        if not isinstance(payload, list):
            return {"ok": False, "error": "Hugging Face returned an unexpected file list.", "files": []}
        raw = []
        for item in payload:
            if not isinstance(item, dict) or str(item.get("type") or "") != "file":
                continue
            path = str(item.get("path") or "").replace("\\", "/")
            if not path.lower().endswith(".gguf"):
                continue
            try:
                self._dest(repo, path)
            except ValueError:
                continue
            raw.append({"path": path, "size": int(item.get("size") or 0)})
        mode = "projector" if str(mode) == "projector" else "model"
        groups = self._group(repo, raw)
        if mode == "projector":
            groups = [item for item in groups if item["kind"] == "mmproj"]
        else:
            groups = [item for item in groups if item["kind"] == "model"]
        return {"ok": True, "repo": repo, "files": groups}

    def download(self, repo: str, files, label: str) -> dict:
        try:
            repo = self._repo(repo)
            if isinstance(files, str):
                files = [files]
            if not isinstance(files, list) or not files:
                raise ValueError("Choose a GGUF file to download.")
            paths = [self._file(repo, str(item)) for item in files]
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        kind = "mmproj" if all("mmproj" in Path(path).name.lower() for path in paths) else "model"
        shown = str(label or Path(paths[0]).name)[:180]
        with self.lock:
            if self.job.get("active"):
                return {"ok": False, "error": "A download is already running."}
            self.cancel_event.clear()
            self.job = {
                "active": True,
                "status": "downloading",
                "label": shown,
                "received": 0,
                "total": 0,
                "error": "",
                "path": "",
            }
        threading.Thread(target=self._download, args=(repo, paths, kind, shown), daemon=True).start()
        return {"ok": True}

    def cancel(self) -> dict:
        self.cancel_event.set()
        return {"ok": True}

    def _download(self, repo: str, paths: list[str], kind: str, label: str) -> None:
        received = 0
        try:
            dests = [self._dest(repo, path) for path in paths]
            sizes = [self._known_size(repo, path, dest) for path, dest in zip(paths, dests)]
            total = sum(size for size in sizes if size > 0)
            self._set(total=total)
            wrote = False
            for path, dest, size in zip(paths, dests, sizes):
                if self.cancel_event.is_set():
                    raise InterruptedError("Download cancelled.")
                if dest.is_file() and (size <= 0 or dest.stat().st_size == size):
                    received += dest.stat().st_size
                    self._set(received=received)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                partial = dest.with_name(dest.name + ".partial")
                url = f"{API}/{repo}/resolve/main/{urllib.parse.quote(path)}"
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as handle:
                    while True:
                        if self.cancel_event.is_set():
                            raise InterruptedError("Download cancelled.")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        self._set(received=received, total=max(total, received))
                partial.replace(dest)
                wrote = True
            chosen = self._primary(dests, kind)
            self.engine.adopt_download(chosen, kind)
            self._set(
                active=False,
                status="done",
                label=label,
                path=str(chosen),
                received=max(received, total),
                total=max(total, received),
                error="",
            )
            verb = "Downloaded" if wrote else "Selected"
            self.engine.log(f"{verb} {chosen.name}")
        except InterruptedError:
            self._cleanup_partials(repo, paths)
            self._set(**self._idle())
            self.engine.log("Download cancelled.")
        except Exception as exc:
            self._cleanup_partials(repo, paths)
            message = self._explain(exc)
            self._set(active=False, status="error", label=label, error=message, received=received)
            self.engine.log(f"Download failed: {message}")

    def _known_size(self, repo: str, path: str, dest: Path) -> int:
        if dest.is_file():
            return dest.stat().st_size
        url = f"{API}/{repo}/resolve/main/{urllib.parse.quote(path)}"
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return int(response.headers.get("Content-Length") or 0)
        except Exception:
            return 0

    def _primary(self, dests: list[Path], kind: str) -> Path:
        if kind == "model":
            for dest in dests:
                if "mmproj" not in dest.name.lower():
                    return dest
        return dests[0]

    def _cleanup_partials(self, repo: str, paths: list[str]) -> None:
        for path in paths:
            try:
                partial = self._dest(repo, path).with_name(self._dest(repo, path).name + ".partial")
                if partial.is_file():
                    partial.unlink()
            except (OSError, ValueError):
                pass

    def _group(self, repo: str, raw: list[dict]) -> list[dict]:
        shards: dict[str, list[dict]] = {}
        singles: list[dict] = []
        for item in raw:
            match = SHARD_RE.match(Path(item["path"]).name)
            if match:
                shards.setdefault(match.group("base"), []).append(item)
            else:
                singles.append(item)
        groups = [self._entry(repo, [item], Path(item["path"]).name) for item in singles]
        for base, parts in shards.items():
            parts.sort(key=lambda item: item["path"])
            groups.append(self._entry(repo, parts, f"{base}.gguf"))
        groups.sort(key=self._rank)
        for entry in groups:
            if entry["kind"] == "model" and "q4_k_m" in entry["label"].lower():
                entry["recommended"] = True
                break
        return groups

    def _rank(self, entry: dict) -> tuple:
        name = entry["label"].lower()
        preference = next((index for index, token in enumerate(PREFERRED) if token in name), len(PREFERRED))
        return (entry["kind"] == "mmproj", preference, entry["size"], name)

    def _entry(self, repo: str, parts: list[dict], label: str) -> dict:
        paths = [item["path"] for item in parts]
        size = sum(int(item["size"] or 0) for item in parts)
        kind = "mmproj" if all("mmproj" in Path(path).name.lower() for path in paths) else "model"
        local = True
        for item, path in zip(parts, paths):
            try:
                dest = self._dest(repo, path)
            except ValueError:
                local = False
                break
            expected = int(item["size"] or 0)
            if not dest.is_file() or (expected and dest.stat().st_size != expected):
                local = False
                break
        return {
            "label": label,
            "files": paths,
            "size": size,
            "size_label": format_bytes(size) if size else "size unknown",
            "parts": len(paths),
            "kind": kind,
            "local": local,
            "recommended": False,
        }

    def _repo(self, repo: str) -> str:
        repo = str(repo).strip()
        if not REPO_RE.match(repo):
            raise ValueError("That repository name is not valid.")
        return repo

    def _file(self, repo: str, filename: str) -> str:
        filename = filename.replace("\\", "/").lstrip("/")
        self._dest(repo, filename)
        return filename

    def _dest(self, repo: str, filename: str) -> Path:
        repo = self._repo(repo)
        relative = Path(filename.replace("\\", "/"))
        if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
            raise ValueError("That file name is not valid.")
        if not relative.name.lower().endswith(".gguf"):
            raise ValueError("Only GGUF files can be downloaded.")
        org, name = repo.split("/", 1)
        target = (self.models_dir / org / name / relative).resolve()
        root = self.models_dir.resolve()
        if target != root and root not in target.parents:
            raise ValueError("That file would land outside the models folder.")
        return target

    def _get_json(self, url: str, timeout: int = 30):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _set(self, **kwargs) -> None:
        with self.lock:
            self.job.update(kwargs)

    def _idle(self) -> dict:
        return {
            "active": False,
            "status": "idle",
            "label": "",
            "received": 0,
            "total": 0,
            "error": "",
            "path": "",
        }

    def _explain(self, exc: Exception) -> str:
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code in (401, 403):
                return "Hugging Face refused this model. It may require you to accept its terms on the website first."
            if exc.code == 404:
                return "Hugging Face could not find that model."
            return f"Hugging Face returned HTTP {exc.code}."
        if isinstance(exc, urllib.error.URLError):
            return "Could not reach Hugging Face. Check the network connection."
        if isinstance(exc, TimeoutError):
            return "Hugging Face took too long to respond."
        text = str(exc).strip() or exc.__class__.__name__
        return text[:240]
