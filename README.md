<div align="center">

# DiffTrail

### Reconstruct project history that Git never captured.

**Recover lost states and turn them into an evidence-backed Git timeline.**

[![PyPI](https://img.shields.io/pypi/v/difftrail?style=flat-square&color=3775A9)](https://pypi.org/project/difftrail/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![MIT license](https://img.shields.io/badge/license-MIT-f4c430?style=flat-square)](LICENSE)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Commands](#commands) · [Safety](#safety)

<br />

<code>uvx difftrail init</code>

</div>

---

Git only remembers what you commit. AI coding agents often leave useful intermediate states scattered across patches, tool calls, test output, uncommitted diffs, later file versions, and local session history.

DiffTrail collects that evidence, replays every change it can verify, and uses Codex to reconstruct the missing pieces. The result is a separate `difftrail/*` branch with clearly marked commits and file-level provenance.

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

Upgrade later with `uv tool upgrade difftrail`. DiffTrail requires Python 3.11+, Git, the [Codex CLI](https://developers.openai.com/codex/cli/), and existing Codex authentication.

Until the first PyPI release is published, run the GitHub version directly:

```bash
uvx --from git+https://github.com/rishaandesai/difftrail.git difftrail init
```

## How it works

| | |
| --- | --- |
| **Collect** | Finds matching Codex sessions, Git history, diffs, patches, file versions, tool calls, test output, and supplied artifacts. |
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

DiffTrail is local-first. It never switches or modifies the original worktree, never publishes automatically, stores no model credentials, and never executes a historical command merely because it appeared in agent history.

Every reconstructed file is classified as:

- `exact` — original bytes were preserved;
- `reconstructed` — the bytes follow from verified evidence;
- `inferred` — model judgment affected the result.

Full manifests remain under `.git/difftrail/` and travel with published commits through `refs/notes/difftrail`.

## License

[MIT](LICENSE)
