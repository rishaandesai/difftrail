from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from timewarp.codex import normalize_session, session_matches
from timewarp.evidence import TimewarpError, matches_patterns
from timewarp.git import apply_change, apply_unified_diff


PROJECT = Path(__file__).resolve().parents[1]


def shell(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result


class TimewarpFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "project"
        self.repo.mkdir()
        shell(self.repo, "git", "init", "-q")
        shell(self.repo, "git", "config", "user.name", "Test User")
        shell(self.repo, "git", "config", "user.email", "test@example.com")
        shell(self.repo, "git", "config", "commit.gpgsign", "false")
        (self.repo / "app.py").write_text("value = 'base'\n")
        shell(self.repo, "git", "add", "app.py")
        shell(self.repo, "git", "commit", "-q", "-m", "base")
        self.base_commit = shell(self.repo, "git", "rev-parse", "HEAD").stdout.strip()

        self.codex_home = self.base / "codex"
        session_dir = self.codex_home / "sessions" / "2026" / "08" / "01"
        session_dir.mkdir(parents=True)
        self.session = session_dir / "rollout-test.jsonl"
        records = [
            {
                "timestamp": "2026-08-01T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "session-a", "cwd": str(self.repo), "timestamp": "2026-08-01T10:00:00Z", "git": {"commit_hash": self.base_commit}},
            },
            {
                "timestamp": "2026-08-01T10:01:00Z",
                "type": "response_item",
                "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Make the intermediate implementation work"}]},
            },
            {
                "timestamp": "2026-08-01T10:02:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "patch-1",
                    "turn_id": "turn-1",
                    "success": True,
                    "changes": {
                        str(self.repo / "app.py"): {"type": "update", "unified_diff": "@@ -1 +1 @@\n-value = 'base'\n+value = 'lost-working-state'\n"},
                        str(self.repo / "generated.txt"): {"type": "add", "content": "historical artifact\n"},
                    },
                },
            },
            {
                "timestamp": "2026-08-01T10:03:00Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "test-1", "input": "pytest -q"},
            },
            {
                "timestamp": "2026-08-01T10:03:10Z",
                "type": "response_item",
                "payload": {"type": "custom_tool_call_output", "call_id": "test-1", "output": "1 passed\n"},
            },
            {
                "timestamp": "2026-08-01T10:04:00Z",
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "turn_id": "turn-2"},
            },
            {
                "timestamp": "2026-08-01T10:05:00Z",
                "type": "event_msg",
                "payload": {
                    "type": "patch_apply_end",
                    "call_id": "patch-2",
                    "turn_id": "turn-3",
                    "success": True,
                    "changes": {
                        str(self.repo / "app.py"): {"type": "update", "unified_diff": "@@ -1 +1 @@\n-value = 'lost-working-state'\n+value = 'later-state'\n"}
                    },
                },
            },
        ]
        with self.session.open("w") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
            handle.write("{ malformed\n")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(PROJECT / "src")
        environment["CODEX_HOME"] = str(self.codex_home)
        result = subprocess.run(
            [sys.executable, "-m", "timewarp", *args],
            cwd=self.repo,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and result.returncode:
            self.fail(f"CLI failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def test_end_to_end_recovery_commit_explain_and_publish(self) -> None:
        scan_result = self.cli("scan", str(self.repo))
        scan = json.loads(scan_result.stdout)
        self.assertEqual(scan["sessions"], 1)
        self.assertEqual(scan["warnings"], 1)
        self.assertGreaterEqual(scan["duration_seconds"], 0)
        self.assertIn("Searching", scan_result.stderr)
        self.assertIn("Complete:", scan_result.stderr)
        run_id = scan["run_id"]
        malformed = json.loads(self.cli("evidence", run_id, "session-a:8", "--repo", str(self.repo), "--json").stdout)
        self.assertEqual(malformed["kind"], "raw_malformed")
        self.assertEqual(malformed["raw"], "{ malformed")

        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        worktree = Path(start["worktree"])
        self.assertEqual((self.repo / "app.py").read_text(), "value = 'base'\n")

        replay = self.cli("replay", run_id, "--repo", str(self.repo), "--through", "session-a:3")
        report = json.loads(replay.stdout)
        self.assertFalse(report["gaps"])
        self.assertEqual((worktree / "app.py").read_text(), "value = 'lost-working-state'\n")
        self.assertEqual((worktree / "generated.txt").read_text(), "historical artifact\n")

        manifest = self.base / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "target_time": "2026-08-01T10:02:00+00:00",
                    "files": [
                        {"path": "app.py", "classification": "reconstructed", "evidence": ["session-a:3"], "method": "verified patch replay"},
                        {"path": "generated.txt", "classification": "exact", "evidence": ["session-a:3"], "method": "full content in patch event"},
                    ],
                }
            )
        )
        committed = json.loads(
            self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "recover working intermediate").stdout
        )
        commit = committed["commit"]
        author = shell(worktree, "git", "show", "-s", "--format=%an <%ae>", commit).stdout.strip()
        self.assertEqual(author, "Codex Reconstruction Agent <codex@timewarp.local>")
        author_date, committer_date = shell(worktree, "git", "show", "-s", "--format=%aI%n%cI", commit).stdout.splitlines()
        self.assertTrue(author_date.startswith("2026-08-01T10:02:00"))
        self.assertNotEqual(author_date, committer_date)
        self.assertIn("Timewarp-Reconstructed: true", shell(worktree, "git", "show", "-s", "--format=%B", commit).stdout)
        self.assertEqual(shell(self.repo, "git", "status", "--porcelain").stdout, "")
        self.assertEqual(shell(self.repo, "git", "rev-parse", "HEAD").stdout.strip(), self.base_commit)

        explained = self.cli("explain", commit, "--repo", str(self.repo), "--file", "app.py").stdout
        self.assertIn('"classification": "reconstructed"', explained)

        verified = json.loads(
            self.cli(
                "verify",
                run_id,
                "--repo",
                str(self.repo),
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('generated.txt').read_text() == 'historical artifact\\n'",
            ).stdout
        )
        self.assertEqual(verified["exit_code"], 0)

        bare = self.base / "remote.git"
        shell(self.base, "git", "init", "-q", "--bare", str(bare))
        shell(self.repo, "git", "remote", "add", "origin", str(bare))
        published = self.cli("publish", run_id, "--repo", str(self.repo)).stdout.strip()
        self.assertEqual(published, f"origin:timewarp/{run_id}")
        self.assertEqual(shell(bare, "git", "rev-parse", f"refs/heads/timewarp/{run_id}").stdout.strip(), commit)
        self.assertTrue(shell(bare, "git", "show-ref", "--verify", "refs/notes/timewarp", check=False).returncode == 0)

    def test_init_is_human_readable_and_remote_is_optional(self) -> None:
        result = self.cli("init", str(self.repo), "--no-remote-check")
        self.assertIn("Timewarp setup", result.stdout)
        self.assertIn("Ready to reconstruct.", result.stdout)
        self.assertIn("only needed for `timewarp publish`", result.stdout)
        self.assertNotIn('"python": {', result.stdout)

    def test_init_json_is_machine_readable(self) -> None:
        result = json.loads(self.cli("init", str(self.repo), "--no-remote-check", "--json").stdout)
        self.assertTrue(result["ready"])
        self.assertIn("python", result["checks"])

    def test_scan_quiet_preserves_json_and_suppresses_progress(self) -> None:
        result = self.cli("scan", str(self.repo), "--quiet")
        self.assertEqual(result.stderr, "")
        self.assertIn("run_id", json.loads(result.stdout))

    def test_reconstruct_defaults_to_complete_history_and_can_reuse_scan(self) -> None:
        first = self.cli("reconstruct", "--repo", str(self.repo), "--quiet", "--print-prompt")
        self.assertIn("complete-history reconstruction", first.stdout)
        self.assertIn("the entire recoverable project history", first.stdout)
        run_count = len(list((self.repo / ".git" / "timewarp" / "runs").glob("*/run.json")))

        second = self.cli(
            "reconstruct",
            "before the refactor",
            "--repo",
            str(self.repo),
            "--run",
            "latest",
            "--print-prompt",
        )
        self.assertIn("single-state reconstruction", second.stdout)
        self.assertIn("before the refactor", second.stdout)
        self.assertEqual(len(list((self.repo / ".git" / "timewarp" / "runs").glob("*/run.json"))), run_count)

    def test_reconstruct_invokes_codex_exec_with_existing_auth_path(self) -> None:
        scan = json.loads(self.cli("scan", str(self.repo), "--quiet").stdout)
        fake_codex = self.base / "fake-codex"
        fake_codex.write_text("#!/bin/sh\nprintf 'args:%s\\n' \"$*\"\ncat\n")
        fake_codex.chmod(0o755)
        result = self.cli(
            "reconstruct",
            "--repo",
            str(self.repo),
            "--run",
            scan["run_id"],
            "--codex",
            str(fake_codex),
        )
        self.assertIn("args:exec --cd", result.stdout)
        self.assertIn("--sandbox workspace-write", result.stdout)
        self.assertIn("--add-dir", result.stdout)
        self.assertIn("complete-history reconstruction", result.stdout)

    def test_false_exact_claim_is_rejected(self) -> None:
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        worktree = Path(start["worktree"])
        (worktree / "app.py").write_text("invented\n")
        manifest = self.base / "false.json"
        manifest.write_text(json.dumps({"target_time": "2026-08-01T10:02:00Z", "files": [{"path": "app.py", "classification": "exact", "evidence": ["session-a:3"]}]}))
        result = self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "false", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No deterministic proof", result.stderr)

    def test_fabricated_git_evidence_is_rejected(self) -> None:
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        Path(start["worktree"], "app.py").write_text("not the anchor bytes\n")
        manifest = self.base / "false-git.json"
        manifest.write_text(
            json.dumps(
                {
                    "target_time": "2026-08-01T10:02:00Z",
                    "files": [
                        {
                            "path": "app.py",
                            "classification": "exact",
                            "evidence": [f"git:{self.base_commit}:app.py"],
                        }
                    ],
                }
            )
        )
        result = self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "false", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No deterministic proof", result.stderr)

    def test_verified_later_git_blob_is_valid_exact_evidence(self) -> None:
        (self.repo / "later.txt").write_text("surviving later bytes\n")
        shell(self.repo, "git", "add", "later.txt")
        shell(self.repo, "git", "commit", "-q", "-m", "later checkpoint")
        later_commit = shell(self.repo, "git", "rev-parse", "HEAD").stdout.strip()
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        Path(start["worktree"], "later.txt").write_text("surviving later bytes\n")
        manifest = self.base / "later-git.json"
        manifest.write_text(
            json.dumps(
                {
                    "target_time": "2026-08-01T10:02:00Z",
                    "files": [
                        {
                            "path": "later.txt",
                            "classification": "exact",
                            "evidence": [f"git:{later_commit}:later.txt"],
                            "method": "bytes preserved in a later Git tree",
                        }
                    ],
                }
            )
        )
        result = json.loads(
            self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "recover later bytes").stdout
        )
        self.assertEqual(result["classification"], "exact")

    def test_inferred_claim_is_allowed_and_labelled(self) -> None:
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        worktree = Path(start["worktree"])
        (worktree / "app.py").write_text("agent inferred this\n")
        manifest = self.base / "inferred.json"
        manifest.write_text(json.dumps({"target_time": "2026-08-01T10:02:00Z", "files": [{"path": "app.py", "classification": "inferred", "evidence": ["session-a:2"], "method": "agent inference"}]}))
        result = json.loads(self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "candidate").stdout)
        self.assertEqual(result["classification"], "inferred")

    def test_ignore_and_artifact_override(self) -> None:
        (self.repo / ".gitignore").write_text("cache/\n")
        shell(self.repo, "git", "add", ".gitignore")
        shell(self.repo, "git", "commit", "-q", "-m", "ignore cache")
        cache = self.repo / "cache"
        cache.mkdir()
        artifact = cache / "result.bin"
        artifact.write_bytes(b"result")
        output = json.loads(self.cli("scan", str(self.repo), "--artifact", str(artifact)).stdout)
        run = json.loads((Path(output["run_dir"]) / "run.json").read_text())
        self.assertIn(str(artifact.resolve()), run["artifact_paths"])
        events = [json.loads(line) for line in (Path(output["run_dir"]) / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(event.get("kind") == "evidence_file" and event.get("sha256") for event in events))

    def test_timewarpignore_excludes_a_tracked_file(self) -> None:
        (self.repo / "secret.txt").write_text("tracked but excluded\n")
        (self.repo / ".timewarpignore").write_text("secret.txt\n")
        shell(self.repo, "git", "add", "secret.txt", ".timewarpignore")
        shell(self.repo, "git", "commit", "-q", "-m", "add ignored evidence")
        output = json.loads(self.cli("scan", str(self.repo)).stdout)
        events = [json.loads(line) for line in (Path(output["run_dir"]) / "events.jsonl").read_text().splitlines()]
        self.assertFalse(any(event.get("kind") == "file_state" and event.get("path") == "secret.txt" for event in events))

    def test_mutating_historical_command_is_reported_as_a_gap(self) -> None:
        with self.session.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-01T10:06:00Z",
                        "type": "response_item",
                        "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "write-1", "input": "touch shell-created.txt"},
                    }
                )
                + "\n"
            )
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit)
        replay = self.cli("replay", run_id, "--repo", str(self.repo), "--through", "session-a:9", check=False)
        self.assertEqual(replay.returncode, 2)
        self.assertIn("may have changed files", replay.stdout)

    def test_replayed_deletion_is_committed_with_provenance(self) -> None:
        with self.session.open("a") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-08-01T10:06:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "patch_apply_end",
                            "success": True,
                            "changes": {str(self.repo / "app.py"): {"type": "delete"}},
                        },
                    }
                )
                + "\n"
            )
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        start = json.loads(self.cli("start", run_id, "--repo", str(self.repo), "--anchor", self.base_commit).stdout)
        replay = self.cli("replay", run_id, "--repo", str(self.repo), "--through", "session-a:9")
        self.assertFalse(json.loads(replay.stdout)["gaps"])
        manifest = self.base / "deletion.json"
        manifest.write_text(
            json.dumps(
                {
                    "target_time": "2026-08-01T10:06:00Z",
                    "files": [
                        {"path": "app.py", "classification": "exact", "evidence": ["session-a:9"], "method": "recorded deletion"},
                        {"path": "generated.txt", "classification": "exact", "evidence": ["session-a:3"], "method": "full content"},
                    ],
                }
            )
        )
        committed = json.loads(
            self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "recover deletion").stdout
        )
        note = json.loads(shell(Path(start["worktree"]), "git", "notes", "--ref=refs/notes/timewarp", "show", committed["commit"]).stdout)
        deleted = next(item for item in note["files"] if item["path"] == "app.py")
        self.assertTrue(deleted["deleted"])
        self.assertIsNone(deleted["sha256"])

    def test_empty_anchor_creates_a_root_reconstruction_commit(self) -> None:
        run_id = json.loads(self.cli("scan", str(self.repo)).stdout)["run_id"]
        started = self.cli("start", run_id, "--repo", str(self.repo), "--anchor", "empty", check=False)
        if started.returncode:
            self.skipTest("installed Git does not support orphan worktrees")
        worktree = Path(json.loads(started.stdout)["worktree"])
        (worktree / "recovered.txt").write_text("model-recovered bytes\n")
        manifest = self.base / "root.json"
        manifest.write_text(
            json.dumps(
                {
                    "target_time": "2026-08-01T10:00:00Z",
                    "files": [
                        {"path": "recovered.txt", "classification": "inferred", "evidence": ["session-a:2"], "method": "agent inference"}
                    ],
                }
            )
        )
        commit = json.loads(
            self.cli("commit", run_id, "--repo", str(self.repo), "--manifest", str(manifest), "--message", "root recovery").stdout
        )["commit"]
        parents = shell(worktree, "git", "show", "-s", "--format=%P", commit).stdout.strip()
        self.assertEqual(parents, "")

    def test_tool_workdir_associates_a_cross_session_repo(self) -> None:
        other = self.codex_home / "sessions" / "rollout-cross.jsonl"
        other.write_text(
            "\n".join(
                json.dumps(record)
                for record in [
                    {"timestamp": "2026-08-01T09:00:00Z", "type": "session_meta", "payload": {"id": "cross", "cwd": str(self.base)}},
                    {
                        "timestamp": "2026-08-01T09:01:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "name": "exec",
                            "input": json.dumps({"cmd": "git status", "workdir": str(self.repo)}),
                        },
                    },
                ]
            )
            + "\n"
        )
        self.assertTrue(session_matches(other, self.repo))
        events, warnings, _ = normalize_session(other, self.repo)
        self.assertFalse(warnings)
        self.assertEqual(events[0]["cwd"], str(self.repo))


class PatchTests(unittest.TestCase):
    def test_gitignore_patterns_use_pathspec_semantics(self) -> None:
        patterns = ["build/", "*.log", "!/keep.log", "/root-only.txt"]
        self.assertTrue(matches_patterns("build/output.bin", patterns))
        self.assertTrue(matches_patterns("nested/error.log", patterns))
        self.assertFalse(matches_patterns("keep.log", patterns))
        self.assertTrue(matches_patterns("root-only.txt", patterns))
        self.assertFalse(matches_patterns("nested/root-only.txt", patterns))

    def test_forward_and_reverse_patch(self) -> None:
        patch = "@@ -1,2 +1,2 @@\n-one\n+ONE\n two\n"
        updated = apply_unified_diff(b"one\ntwo\n", patch)
        self.assertEqual(updated, b"ONE\ntwo\n")
        self.assertEqual(apply_unified_diff(updated, patch, reverse=True), b"one\ntwo\n")

    def test_mismatched_patch_fails(self) -> None:
        with self.assertRaises(TimewarpError):
            apply_unified_diff(b"different\n", "@@ -1 +1 @@\n-old\n+new\n")

    def test_move_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            (worktree / "old.txt").write_text("bytes\n")
            moved = apply_change(
                worktree,
                {"path": "old.txt", "operation": "move", "move_path": "nested/new.txt"},
                "move-evidence",
            )
            self.assertEqual(moved["path"], "nested/new.txt")
            self.assertEqual((worktree / "nested/new.txt").read_text(), "bytes\n")
            deleted = apply_change(worktree, {"path": "nested/new.txt", "operation": "delete"}, "delete-evidence")
            self.assertTrue(deleted["deleted"])
            self.assertFalse((worktree / "nested/new.txt").exists())

    def test_zero_length_patch_creates_a_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            applied = apply_change(
                worktree,
                {"path": "new.txt", "operation": "update", "unified_diff": "@@ -0,0 +1,2 @@\n+one\n+two\n"},
                "patch-add",
            )
            self.assertEqual(applied["method"], "forward-patch")
            self.assertEqual((worktree / "new.txt").read_text(), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
