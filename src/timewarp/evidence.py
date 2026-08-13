from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from pathspec import GitIgnoreSpec

MAX_GIT_BLOB = 100 * 1024 * 1024
MAX_INLINE_TEXT = 1024 * 1024


class TimewarpError(RuntimeError):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode() + b"\n")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TimewarpError(f"Cannot read JSON from {path}: {exc}") from exc


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise TimewarpError(f"Malformed JSONL at {path}:{line_number}: {exc}") from exc
                if isinstance(value, dict):
                    values.append(value)
    except OSError as exc:
        raise TimewarpError(f"Cannot read {path}: {exc}") from exc
    return values


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def repository_path(path: str | Path, root: Path) -> str | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return None


def load_ignore_patterns(root: Path, explicit: Iterable[str]) -> list[str]:
    patterns = [pattern for pattern in explicit if pattern]
    ignore_file = root / ".timewarpignore"
    if ignore_file.is_file():
        for raw in ignore_file.read_text(errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


@lru_cache(maxsize=64)
def _ignore_spec(patterns: tuple[str, ...]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(patterns)


def matches_patterns(relative_path: str, patterns: Iterable[str]) -> bool:
    return _ignore_spec(tuple(patterns)).match_file(relative_path.strip("/"))


def inventory_path(path: Path, *, label: str, max_blob: int = MAX_GIT_BLOB) -> dict[str, Any]:
    result: dict[str, Any] = {"label": label, "path": str(path), "exists": path.exists()}
    if not path.exists():
        return result
    if path.is_symlink():
        result.update({"kind": "symlink", "target": os.readlink(path)})
        return result
    if path.is_file():
        size = path.stat().st_size
        result.update(
            {
                "kind": "file",
                "size": size,
                "mtime_ns": path.stat().st_mtime_ns,
                "sha256": sha256_file(path),
                "storage": "hash-only" if size > max_blob else "eligible",
            }
        )
        return result
    files: list[dict[str, Any]] = []
    for child in sorted(item for item in path.rglob("*") if item.is_file() or item.is_symlink()):
        relative = child.relative_to(path).as_posix()
        entry = inventory_path(child, label=relative, max_blob=max_blob)
        entry["path"] = str(child)
        files.append(entry)
    result.update({"kind": "directory", "files": files})
    return result


def materialize_field(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def store_large_text(
    value: str,
    *,
    source: Path,
    line: int,
    field: str | list[str],
    transform: str | None = None,
) -> str | dict[str, Any]:
    encoded = value.encode()
    if len(encoded) <= MAX_INLINE_TEXT:
        return value
    result = {
        "source": str(source),
        "line": line,
        "field": field,
        "sha256": sha256_bytes(encoded),
        "size": len(encoded),
        "preview": value[:4096],
    }
    if transform:
        result["transform"] = transform
    return result


def resolve_large_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not {"source", "line", "field"} <= value.keys():
        return None
    source = Path(value["source"])
    line_number = int(value["line"])
    try:
        raw = source.read_text().splitlines()[line_number - 1]
        record = json.loads(raw)
    except (OSError, IndexError, json.JSONDecodeError, ValueError):
        return None
    current: Any = record
    field = value["field"]
    components = field if isinstance(field, list) else str(field).split(".")
    for component in components:
        if not isinstance(current, dict) or component not in current:
            return None
        current = current[component]
    if value.get("transform") == "content_text" and isinstance(current, list):
        current = "\n".join(
            str(item.get("text") or item.get("input_text") or item.get("output_text"))
            for item in current
            if isinstance(item, dict) and isinstance(item.get("text") or item.get("input_text") or item.get("output_text"), str)
        )
    elif value.get("transform") == "json":
        current = json.dumps(current, sort_keys=True)
    if not isinstance(current, str) or sha256_bytes(current.encode()) != value.get("sha256"):
        return None
    return current
