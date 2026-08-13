from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .evidence import MAX_GIT_BLOB, TimewarpError, canonical_json, read_json, sha256_bytes, sha256_file, write_json

AGENT_NAME = "Codex Reconstruction Agent"
AGENT_EMAIL = "codex@timewarp.local"
NOTES_REF = "refs/notes/timewarp"


def run(
    cwd: Path,
    *arguments: str,
    check: bool = True,
    input_data: str | bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    if isinstance(input_data, bytes):
        input_data = input_data.decode(errors="surrogateescape")
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        env=process_env,
        input=input_data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(arguments)} failed"
        raise TimewarpError(message)
    return result


def repository_root(path: Path) -> Path:
    result = run(path.resolve(), "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def common_git_dir(root: Path) -> Path:
    value = run(root, "rev-parse", "--git-common-dir").stdout.strip()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def run_dir(root: Path, run_id: str) -> Path:
    return common_git_dir(root) / "timewarp" / "runs" / run_id


def load_run(root: Path, run_id: str) -> tuple[Path, dict[str, Any]]:
    directory = run_dir(root, run_id)
    config_path = directory / "run.json"
    if not config_path.is_file():
        raise TimewarpError(f"Unknown Timewarp run: {run_id}")
    return directory, read_json(config_path)


def save_run(directory: Path, config: dict[str, Any]) -> None:
    write_json(directory / "run.json", config)


def git_snapshot(root: Path) -> dict[str, Any]:
    head = run(root, "rev-parse", "--verify", "HEAD", check=False)
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "branch": run(root, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip() or None,
        "status": run(root, "status", "--porcelain=v2", "--untracked-files=all").stdout,
        "staged_diff": run(root, "diff", "--cached", "--binary").stdout,
        "unstaged_diff": run(root, "diff", "--binary").stdout,
        "log": run(root, "log", "--all", "--date=iso-strict", "--format=%H%x09%aI%x09%cI%x09%s", check=False).stdout,
        "reflog": run(root, "reflog", "--all", "--date=iso-strict", "--format=%H%x09%gD%x09%gs", check=False).stdout,
        "objects": run(root, "rev-list", "--objects", "--all", check=False).stdout,
        "remotes": run(root, "remote", "-v").stdout,
    }


def tracked_files(root: Path) -> list[str]:
    output = run(root, "ls-files", "-z").stdout
    return [item for item in output.split("\0") if item]


def ignored(root: Path, relative: str) -> bool:
    result = run(root, "check-ignore", "-q", "--", relative, check=False)
    return result.returncode == 0


def start_worktree(root: Path, run_id: str, anchor: str | None = None, worktree: Path | None = None) -> dict[str, str]:
    directory, config = load_run(root, run_id)
    if config.get("worktree"):
        existing = Path(config["worktree"])
        if existing.is_dir():
            return {"branch": config["branch"], "worktree": str(existing), "anchor": str(config.get("anchor") or "empty")}
        raise TimewarpError(f"Recorded worktree is missing: {existing}")

    branch = f"timewarp/{run_id}"
    if run(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise TimewarpError(f"Branch already exists: {branch}")
    destination = (worktree or root.parent / f"{root.name}-timewarp-{run_id}").resolve()
    if destination.exists():
        raise TimewarpError(f"Worktree path already exists: {destination}")

    selected_anchor = anchor
    if not selected_anchor:
        head = run(root, "rev-parse", "--verify", "HEAD", check=False)
        selected_anchor = head.stdout.strip() if head.returncode == 0 else "empty"
    if selected_anchor == "empty":
        result = run(root, "worktree", "add", "--orphan", "-b", branch, str(destination), check=False)
        if result.returncode:
            raise TimewarpError("This Git version cannot create an orphan worktree; use a repository with an anchor commit")
    else:
        verified = run(root, "rev-parse", "--verify", f"{selected_anchor}^{{commit}}").stdout.strip()
        run(root, "worktree", "add", "-b", branch, str(destination), verified)
        selected_anchor = verified

    config.update({"branch": branch, "worktree": str(destination), "anchor": selected_anchor, "replayed_events": []})
    save_run(directory, config)
    return {"branch": branch, "worktree": str(destination), "anchor": selected_anchor}


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_unified_diff(original: bytes, patch: str, *, reverse: bool = False) -> bytes:
    try:
        source = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TimewarpError("Cannot apply a text patch to a binary file") from exc
    old = source.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    result: list[str] = []
    source_index = 0
    index = 0
    found = False
    while index < len(lines):
        match = HUNK.match(lines[index].rstrip("\r\n"))
        if not match:
            index += 1
            continue
        found = True
        old_start = int(match.group(1))
        new_start = int(match.group(3))
        target_start = new_start if reverse else old_start
        target_index = max(target_start - 1, 0)
        if target_index < source_index:
            raise TimewarpError("Overlapping or out-of-order patch hunks")
        result.extend(old[source_index:target_index])
        source_index = target_index
        index += 1
        while index < len(lines) and not HUNK.match(lines[index].rstrip("\r\n")):
            line = lines[index]
            if line.startswith(("--- ", "+++ ", "diff ", "index ")):
                index += 1
                continue
            if line.startswith("\\ No newline"):
                if result:
                    result[-1] = result[-1].rstrip("\r\n")
                index += 1
                continue
            marker = line[:1]
            body = line[1:]
            if reverse:
                marker = {"+": "-", "-": "+"}.get(marker, marker)
            if marker == " ":
                if source_index >= len(old) or old[source_index] != body:
                    raise TimewarpError("Patch context does not match the reconstructed file")
                result.append(body)
                source_index += 1
            elif marker == "-":
                if source_index >= len(old) or old[source_index] != body:
                    raise TimewarpError("Patch deletion does not match the reconstructed file")
                source_index += 1
            elif marker == "+":
                result.append(body)
            elif marker:
                break
            index += 1
    if not found:
        raise TimewarpError("Patch contains no unified-diff hunks")
    result.extend(old[source_index:])
    return "".join(result).encode("utf-8")


def _safe_path(worktree: Path, relative: str) -> Path:
    candidate = (worktree / relative).resolve(strict=False)
    try:
        candidate.relative_to(worktree.resolve())
    except ValueError as exc:
        raise TimewarpError(f"Evidence path escapes the worktree: {relative}") from exc
    return candidate


def apply_change(worktree: Path, change: dict[str, Any], evidence_id: str) -> dict[str, Any]:
    relative = str(change.get("path") or "")
    if not relative:
        raise TimewarpError("File change has no path")
    target = _safe_path(worktree, relative)
    operation = change.get("operation")
    if operation == "delete":
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        else:
            raise TimewarpError(f"Cannot delete missing path: {relative}")
        return {"path": relative, "deleted": True, "evidence_id": evidence_id}

    move_path = change.get("move_path")
    moved_from: str | None = None
    if operation == "move" or isinstance(move_path, str):
        moved_from = relative
        destination = _safe_path(worktree, str(move_path))
        if not target.exists():
            raise TimewarpError(f"Cannot move missing path: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        target.rename(destination)
        target = destination
        relative = str(move_path)

    content = change.get("content")
    patch = change.get("unified_diff")
    if isinstance(content, str):
        data = content.encode()
        method = "full-content"
    elif isinstance(patch, str):
        data = apply_unified_diff(target.read_bytes() if target.is_file() else b"", patch)
        method = "forward-patch"
    elif operation == "move":
        data = target.read_bytes()
        method = "move"
    else:
        raise TimewarpError(f"No deterministic bytes for {relative}")
    if len(data) > MAX_GIT_BLOB:
        raise TimewarpError(f"Refusing Git blob over 100 MiB: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    result = {"path": relative, "sha256": sha256_bytes(data), "evidence_id": evidence_id, "method": method}
    if moved_from:
        result["moved_from"] = moved_from
    return result


def _identity_env(author_date: str | None = None) -> dict[str, str]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "GIT_AUTHOR_NAME": AGENT_NAME,
        "GIT_AUTHOR_EMAIL": AGENT_EMAIL,
        "GIT_COMMITTER_NAME": AGENT_NAME,
        "GIT_COMMITTER_EMAIL": AGENT_EMAIL,
        "GIT_AUTHOR_DATE": author_date or now,
        "GIT_COMMITTER_DATE": now,
    }


def _git_evidence_matches(worktree: Path, relative: str, evidence: list[Any]) -> bool:
    current = run(worktree, "hash-object", "--", relative)
    for evidence_id in evidence:
        match = re.fullmatch(r"git:([0-9a-fA-F]{40,64}):(.+)", str(evidence_id))
        if not match or match.group(2) != relative:
            continue
        commit = run(worktree, "rev-parse", "--verify", f"{match.group(1)}^{{commit}}", check=False)
        blob = run(worktree, "rev-parse", "--verify", f"{match.group(1)}:{relative}", check=False)
        if commit.returncode == 0 and blob.returncode == 0 and blob.stdout.strip() == current.stdout.strip():
            return True
    return False


def commit_reconstruction(root: Path, run_id: str, manifest_path: Path, message: str) -> tuple[str, dict[str, Any]]:
    directory, config = load_run(root, run_id)
    if not config.get("worktree"):
        raise TimewarpError("Run has no reconstruction worktree; run `timewarp start` first")
    worktree = Path(config["worktree"])
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise TimewarpError("Manifest must be a JSON object")
    supplied = {entry.get("path"): entry for entry in manifest.get("files", []) if isinstance(entry, dict) and entry.get("path")}
    proof = config.get("proof", {}) if isinstance(config.get("proof"), dict) else {}

    changed = [
        line
        for line in run(worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split("\0")
        if line
    ]
    if not changed:
        raise TimewarpError("Candidate tree has no changes to commit")
    parent = run(worktree, "rev-parse", "--verify", "HEAD", check=False).stdout.strip() or None
    expanded: list[dict[str, Any]] = []
    indexed_files = set(tracked_files(worktree))
    current_files = sorted(
        {
            line
            for line in run(worktree, "ls-files", "-co", "--exclude-standard", "-z").stdout.split("\0")
            if line and (worktree / line).is_file()
        }
    )
    for relative in current_files:
        path = worktree / relative
        if path.stat().st_size > MAX_GIT_BLOB:
            raise TimewarpError(f"Refusing Git blob over 100 MiB: {relative}")
        digest = sha256_file(path)
        entry = dict(supplied.get(relative) or {})
        if not entry and parent:
            old = run(worktree, "rev-parse", "--verify", f"{parent}:{relative}", check=False)
            current = run(worktree, "hash-object", "--", relative)
            if old.returncode == 0 and old.stdout.strip() == current.stdout.strip():
                entry = {
                    "path": relative,
                    "classification": "exact",
                    "evidence": [f"git:{parent}:{relative}"],
                    "method": "unchanged-anchor-file",
                }
        if not entry:
            raise TimewarpError(f"Manifest is missing changed file: {relative}")
        classification = entry.get("classification")
        if classification not in {"exact", "reconstructed", "inferred"}:
            raise TimewarpError(f"Invalid classification for {relative}: {classification}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise TimewarpError(f"Manifest needs evidence for {relative}")
        if classification in {"exact", "reconstructed"}:
            candidates = proof.get(relative, [])
            supported = any(
                isinstance(item, dict)
                and item.get("sha256") == digest
                and item.get("evidence_id") in evidence
                for item in candidates
            )
            if not supported:
                supported = _git_evidence_matches(worktree, relative, evidence)
            if not supported:
                raise TimewarpError(f"No deterministic proof supports {classification} file {relative}")
        entry["path"] = relative
        entry["sha256"] = digest
        expanded.append(entry)

    for relative in sorted(path for path in indexed_files if not (worktree / path).exists()):
        entry = dict(supplied.get(relative) or {})
        if not entry:
            raise TimewarpError(f"Manifest is missing deleted file: {relative}")
        classification = entry.get("classification")
        if classification not in {"exact", "reconstructed", "inferred"}:
            raise TimewarpError(f"Invalid classification for {relative}: {classification}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise TimewarpError(f"Manifest needs evidence for {relative}")
        if classification in {"exact", "reconstructed"}:
            supported = any(
                isinstance(item, dict) and item.get("deleted") and item.get("evidence_id") in evidence
                for item in proof.get(relative, [])
            )
            if not supported:
                raise TimewarpError(f"No deterministic proof supports {classification} deletion {relative}")
        entry.update({"path": relative, "sha256": None, "deleted": True})
        expanded.append(entry)

    rank = {"exact": 0, "reconstructed": 1, "inferred": 2}
    overall = max((entry["classification"] for entry in expanded), key=lambda value: rank[value])
    target_time = str(manifest.get("target_time") or config.get("target_time") or datetime.now(timezone.utc).isoformat())
    author_date = target_time.split("/", 1)[0]
    final_manifest = {
        **manifest,
        "run_id": run_id,
        "agent": "codex",
        "reconstructed_at": datetime.now(timezone.utc).isoformat(),
        "classification": overall,
        "excluded_artifacts": config.get("exclusions", []),
        "hash_only_artifacts": config.get("hash_only", []),
        "files": sorted(expanded, key=lambda item: item["path"]),
    }
    manifest_bytes = canonical_json(final_manifest)
    manifest_hash = sha256_bytes(manifest_bytes)
    full_message = "\n".join(
        [
            message.strip(),
            "",
            "Timewarp-Reconstructed: true",
            "Timewarp-Agent: codex",
            f"Timewarp-Run: {run_id}",
            f"Timewarp-Target-Time: {target_time}",
            f"Timewarp-Classification: {overall}",
            f"Timewarp-Manifest: {manifest_hash}",
        ]
    )
    run(worktree, "add", "-A")
    run(worktree, "commit", "--no-gpg-sign", "-m", full_message, env=_identity_env(author_date))
    commit = run(worktree, "rev-parse", "HEAD").stdout.strip()
    stored = directory / "manifests" / f"{commit}.json"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(manifest_bytes)
    run(worktree, "notes", f"--ref={NOTES_REF}", "add", "-f", "-F", str(stored), commit, env=_identity_env())
    config.setdefault("commits", []).append({"commit": commit, "manifest": str(stored), "sha256": manifest_hash})
    save_run(directory, config)
    return commit, final_manifest


def publish(root: Path, run_id: str, remote: str) -> str:
    _, config = load_run(root, run_id)
    branch = config.get("branch")
    worktree = Path(config.get("worktree") or root)
    if not branch:
        raise TimewarpError("Run has no reconstruction branch")
    local = run(worktree, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    for item in config.get("commits", []):
        if not isinstance(item, dict):
            continue
        commit = str(item.get("commit") or "")
        manifest_path = Path(str(item.get("manifest") or ""))
        if not commit or not manifest_path.is_file():
            raise TimewarpError("A reconstruction manifest is missing; refusing to publish")
        manifest_hash = sha256_bytes(canonical_json(read_json(manifest_path)))
        if manifest_hash != item.get("sha256"):
            raise TimewarpError(f"Manifest hash mismatch for {commit}")
        if run(worktree, "merge-base", "--is-ancestor", commit, local, check=False).returncode:
            raise TimewarpError(f"Reconstructed commit is not on {branch}: {commit}")
        note = run(worktree, "notes", f"--ref={NOTES_REF}", "show", commit, check=False)
        try:
            note_hash = sha256_bytes(canonical_json(json.loads(note.stdout))) if note.returncode == 0 else ""
        except ValueError:
            note_hash = ""
        if note_hash != manifest_hash:
            raise TimewarpError(f"Provenance note mismatch for {commit}")
    if remote not in run(root, "remote").stdout.split():
        raise TimewarpError(f"Unknown Git remote: {remote}")
    existing = run(root, "ls-remote", "--heads", remote, f"refs/heads/{branch}").stdout.strip()
    if existing and existing.split()[0] != local:
        remote_tip = existing.split()[0]
        if run(worktree, "cat-file", "-e", f"{remote_tip}^{{commit}}", check=False).returncode or run(
            worktree, "merge-base", "--is-ancestor", remote_tip, local, check=False
        ).returncode:
            raise TimewarpError(f"Remote branch already has incompatible history: {branch}")
    refspecs = [f"refs/heads/{branch}:refs/heads/{branch}"]
    if run(root, "show-ref", "--verify", "--quiet", NOTES_REF, check=False).returncode == 0:
        refspecs.append(f"{NOTES_REF}:{NOTES_REF}")
    run(root, "push", "--atomic", remote, *refspecs)
    url = run(root, "remote", "get-url", remote).stdout.strip()
    match = re.search(r"github\.com[/:]([^/]+)/(.+)$", url)
    if match:
        repository = match.group(2).removesuffix(".git")
        return f"https://github.com/{match.group(1)}/{repository}/tree/{branch}"
    return f"{remote}:{branch}"
