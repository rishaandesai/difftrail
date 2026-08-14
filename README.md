<div align="center"> 

# DiffTrail

### Reconstruct Git history—even if you never committed it.

**Recover lost states and turn them into an evidence-backed Git timeline.**

[![PyPI](https://img.shields.io/pypi/v/difftrail?style=flat-square&color=3775A9)](https://pypi.org/project/difftrail/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![MIT license](https://img.shields.io/badge/license-MIT-f4c430?style=flat-square)](LICENSE)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Commands](#commands) · [Safety](#safety)

<br />

<code>uvx difftrail init</code>

</div>

---

Git only remembers what you commit. Useful intermediate states often survive elsewhere, such as in patches, tool calls, test outputs, uncommitted diffs, later file versions, local session history, etc.

DiffTrail retrospectively collects that evidence, replays every change it can verify, and uses a coding agent model to reconstruct the missing pieces in a separate branch with clearly marked commits that you can revert or refer to.

## Quick start

### Run without installing

From the Git repository you want to reconstruct:

```bash
uvx difftrail init
uvx difftrail reconstruct
```

Recover one particular state instead:

```bash
uvx difftrail reconstruct "before the auth refactor"
```

If reconstruction is interrupted:

```bash
uvx difftrail reconstruct --resume
```

### Install the `difftrail` command

```bash
uv tool install difftrail
difftrail init
difftrail reconstruct
```

Upgrade later with `uv tool upgrade difftrail`. DiffTrail requires Python 3.11+, Git, and the [Codex CLI](https://developers.openai.com/codex/cli/). 

## How it works

| | |
| --- | --- |
| **Collect** | Finds Git history, uncommitted diffs, patches, file versions, tool calls, test output, supplied artifacts, and matching Codex sessions. |
| **Reconstruct** | Replays byte-exact changes first, then lets Codex resolve only the remaining gaps. |
| **Preserve** | Writes reconstructed milestones to a separate branch and worktree with provenance attached as Git notes. |

Running `difftrail reconstruct` with no target reconstructs the complete recoverable history. DiffTrail shows a persistent task progress bar whose total grows when Codex discovers more work. Commands appear as `[codex] $ ...`; detailed model events and command output stay in the run log.

## Commands

| Command | Purpose |
| --- | --- |
| `difftrail init` | Check Python, Git, Codex, local history, and repository readiness. |
| `difftrail reconstruct [TARGET]` | Scan and reconstruct the full history or one described state. |
| `difftrail reconstruct --resume` | Continue the latest reconstruction without rescanning or resetting completed work. |
| `difftrail scan [REPO]` | Collect and normalize evidence without starting reconstruction. |
| `difftrail evidence RUN EVENT` | Inspect one normalized evidence record. |
| `difftrail explain COMMIT` | Explain the evidence and classification behind a reconstructed commit. |
| `difftrail publish RUN` | Push the reconstructed branch and provenance notes to the existing remote. |

The lower-level `start`, `replay`, `commit`, and `verify` commands support the reconstruction workflow and advanced manual use. Run `difftrail COMMAND --help` for every option.

## Safety

DiffTrail doesn't switch or modify the original worktree or branch, stores no model credentials, and never arbitrarily executes a historical command merely because it appeared in agent history.

Every reconstructed file is classified as:

- `exact` — original bytes were preserved;
- `reconstructed` — the bytes follow from verified evidence;
- `inferred` — model judgment affected the result.

Full manifests are located under `.git/difftrail/` and published commits through `refs/notes/difftrail`.

## License

[MIT](LICENSE)
