from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from .evidence import is_within, repository_path, sha256_bytes, store_large_text


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def session_files(home: Path | None = None) -> list[Path]:
    base = home or codex_home()
    files: list[Path] = []
    for directory in (base / "sessions", base / "archived_sessions"):
        if directory.is_dir():
            files.extend(directory.rglob("*.jsonl"))
    return sorted(set(files))


def _records(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None, str]]:
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield line_number, None, str(exc), line.rstrip("\r\n")
                    continue
                yield line_number, value if isinstance(value, dict) else None, None, line.rstrip("\r\n")
    except OSError as exc:
        yield 0, None, str(exc), ""


def session_matches(path: Path, root: Path) -> bool:
    session_cwd: str | None = None
    for _, record, _, _ in _records(path):
        if not record:
            continue
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and is_within(Path(cwd), root):
                return True
            if isinstance(cwd, str):
                session_cwd = cwd
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            changes = payload.get("changes")
            if isinstance(changes, dict):
                for changed_path in changes:
                    if repository_path(changed_path, root) is not None:
                        return True
        if record.get("type") == "response_item" and isinstance(payload, dict):
            if payload.get("type") in {"custom_tool_call", "function_call"}:
                raw_input = _tool_input(payload)
                cwd = _tool_working_directory(raw_input, session_cwd)
                if cwd and is_within(Path(cwd), root):
                    return True
    return False


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("input_text") or item.get("output_text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _tool_input(payload: dict[str, Any]) -> str:
    value = payload.get("input") if "input" in payload else payload.get("arguments", "")
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _tool_working_directory(raw_input: str, session_cwd: str | None) -> str | None:
    for key in ("workdir", "cwd"):
        match = re.search(rf'["\']{key}["\']\s*:\s*["\']([^"\']+)', raw_input)
        if match:
            return match.group(1).replace("\\/", "/")
    return session_cwd


def _shell_commands(raw_input: str) -> list[str]:
    commands: list[str] = []
    for match in re.finditer(r'(?:["\']cmd["\']|\bcmd)\s*:\s*"((?:\\.|[^"\\])*)"', raw_input):
        try:
            commands.append(json.loads(f'"{match.group(1)}"'))
        except json.JSONDecodeError:
            continue
    return commands or [raw_input]


def _may_mutate_files(raw_input: str) -> bool:
    if "tools.apply_patch" in raw_input:
        return True
    for command in _shell_commands(raw_input):
        sanitized = re.sub(r"\d*>\s*/dev/null", "", command)
        if re.search(r"(?:^|[;&|]\s*)\s*(?:rm|mv|cp|touch|mkdir|truncate|install)\b", sanitized):
            return True
        if re.search(r"\bsed\s+-i\b|\btee\s+(?!/dev/null|/tmp/)", sanitized):
            return True
        for match in re.finditer(r"(?:^|\s)>{1,2}[ \t]*([^\s;&|]+)", sanitized):
            target = match.group(1).strip("'\"")
            if target != "/dev/null" and not target.startswith("/tmp/"):
                return True
    return False


def normalize_session(path: Path, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    session_id = path.stem
    session_cwd: str | None = None
    metadata: dict[str, Any] = {"source": str(path)}

    for line_number, record, error, raw_line in _records(path):
        if error:
            warnings.append({"source": str(path), "line": line_number, "warning": error})
            events.append(
                {
                    "id": f"{session_id}:{line_number}",
                    "session_id": session_id,
                    "timestamp": None,
                    "kind": "raw_malformed",
                    "source": {
                        "path": str(path),
                        "line": line_number,
                        "sha256": sha256_bytes(raw_line.encode()),
                    },
                }
            )
            continue
        assert record is not None
        record_type = record.get("type")
        payload = record.get("payload")
        timestamp = record.get("timestamp")
        if record_type == "session_meta" and isinstance(payload, dict):
            session_id = str(payload.get("id") or payload.get("session_id") or session_id)
            metadata.update(
                {
                    "id": session_id,
                    "cwd": payload.get("cwd"),
                    "timestamp": payload.get("timestamp") or timestamp,
                    "git": payload.get("git"),
                    "source_kind": payload.get("source"),
                }
            )
            session_cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
            continue

        event: dict[str, Any] | None = None
        if record_type == "event_msg" and isinstance(payload, dict):
            event_type = str(payload.get("type") or "event")
            if event_type == "patch_apply_end":
                normalized_changes: list[dict[str, Any]] = []
                raw_changes = payload.get("changes")
                if isinstance(raw_changes, dict):
                    for changed_path, raw_change in raw_changes.items():
                        if not isinstance(raw_change, dict):
                            continue
                        relative = repository_path(str(changed_path), root)
                        if relative is None:
                            continue
                        change: dict[str, Any] = {
                            "path": relative,
                            "operation": raw_change.get("type"),
                        }
                        for field in ("content", "unified_diff", "move_path"):
                            value = raw_change.get(field)
                            if isinstance(value, str):
                                change[field] = store_large_text(
                                    value,
                                    source=path,
                                    line=line_number,
                                    field=["payload", "changes", str(changed_path), field],
                                )
                        if isinstance(change.get("move_path"), str):
                            move = repository_path(change["move_path"], root)
                            change["move_path"] = move or change["move_path"]
                        normalized_changes.append(change)
                event = {
                    "kind": "file_change",
                    "call_id": payload.get("call_id"),
                    "turn_id": payload.get("turn_id"),
                    "success": bool(payload.get("success")),
                    "changes": normalized_changes,
                    "stdout": payload.get("stdout"),
                    "stderr": payload.get("stderr"),
                }
            elif event_type in {
                "turn_aborted",
                "thread_rolled_back",
                "context_compacted",
                "task_started",
                "task_complete",
                "user_message",
                "agent_message",
            }:
                event = {"kind": event_type, "data": payload}

        elif record_type == "response_item" and isinstance(payload, dict):
            item_type = payload.get("type")
            if item_type == "message" and payload.get("role") in {"user", "assistant"}:
                event = {
                    "kind": "message",
                    "role": payload.get("role"),
                    "text": store_large_text(
                        _content_text(payload.get("content")),
                        source=path,
                        line=line_number,
                        field=["payload", "content"],
                        transform="content_text",
                    ),
                }
            elif item_type in {"custom_tool_call", "function_call"}:
                raw_input = _tool_input(payload)
                event = {
                    "kind": "tool_call",
                    "tool": payload.get("name"),
                    "call_id": payload.get("call_id"),
                    "input": store_large_text(
                        raw_input,
                        source=path,
                        line=line_number,
                        field=["payload", "input" if "input" in payload else "arguments"],
                        transform=None if isinstance(payload.get("input") if "input" in payload else payload.get("arguments"), str) else "json",
                    ),
                    "cwd": _tool_working_directory(raw_input, session_cwd),
                    "may_mutate_files": _may_mutate_files(raw_input),
                    "captured_by_patch_event": "tools.apply_patch" in raw_input,
                }
            elif item_type in {"custom_tool_call_output", "function_call_output"}:
                output = payload.get("output")
                if not isinstance(output, str):
                    output = json.dumps(output, sort_keys=True)
                event = {
                    "kind": "tool_result",
                    "call_id": payload.get("call_id"),
                    "output": store_large_text(
                        output,
                        source=path,
                        line=line_number,
                        field=["payload", "output"],
                        transform=None if isinstance(payload.get("output"), str) else "content_text" if isinstance(payload.get("output"), list) else "json",
                    ),
                }

        if event is None:
            event = {
                "kind": "raw_record",
                "record_type": record_type,
                "payload_type": payload.get("type") if isinstance(payload, dict) else None,
            }
        event.update(
            {
                "id": f"{session_id}:{line_number}",
                "session_id": session_id,
                "timestamp": timestamp,
                "source": {"path": str(path), "line": line_number, "sha256": sha256_bytes(raw_line.encode())},
            }
        )
        events.append(event)

    return events, warnings, metadata
