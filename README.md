# Timewarp

Timewarp reconstructs project states that were never committed or checkpointed. It combines local Codex session evidence with Git history, replays every byte-exact change it can prove, and lets Codex infer only the missing pieces. Reconstructed states live on a clearly marked `timewarp/*` branch in a separate worktree.

```bash
uvx timewarp init
uvx timewarp install-codex
```

`timewarp init` prints a readable setup checklist. Use `timewarp init --json` for machine-readable results; a Git remote and the optional Codex skill are not required for local reconstruction.

Then ask Codex to use `$timewarp` to recover one lost state or build a historical timeline.

`timewarp scan` uses a live `tqdm` progress bar plus phase, session, event, and elapsed-time messages on stderr while keeping its final JSON result on stdout. Pass `--quiet` for machine-only output.

The high-level workflow is `timewarp reconstruct`: with no target it asks the installed Codex CLI to reconstruct the full recoverable history of the current repository; a target phrase narrows it to one state. It uses the user's existing Codex authentication and never publishes automatically.

```bash
timewarp reconstruct
timewarp reconstruct "before the auth refactor"
timewarp reconstruct --run latest
timewarp reconstruct --resume
```

During reconstruction, Timewarp renders a persistent `tqdm` task bar and prints each invoked shell command as `[codex] $ ...`. Codex maintains a durable task ledger at `.git/timewarp/runs/<run-id>/progress.json`: its total grows when new milestones or gaps are discovered, completed tasks advance the bar, and reopened work can move it backward. Raw Codex events and command outputs are appended to `codex.jsonl` instead of flooding the terminal.

`timewarp reconstruct --resume` reuses the latest run without rescanning, preserves its ledger and reconstruction worktree, restores the prior target when one was supplied, and asks Codex to continue from the existing commits, manifests, and pending tasks. Use `--run RUN_ID --resume` to resume a specific older run.

Timewarp is local-first. It stores no model credentials and embeds no provider SDK; `reconstruct` delegates reasoning and tool use to the installed Codex CLI with the user's existing authentication. It does not modify the source worktree, push automatically, or execute historical shell commands merely because they appear in evidence.
