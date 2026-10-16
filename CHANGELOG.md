# Changelog

## 0.2.0 — 2026-10-16

First public version, after a multi-day unattended run on a SLURM cluster. Most changes come from problems seen in that run.

### Running unattended
- `scripts/run_pi.sh`: one Claude Code session per turn, hard time limit per turn (`PI_TURN_TIMEOUT`), API budget cap (`[budget] total_usd`), status on the shared file system (`state/pi_status.json`), refusal to run in an untrusted folder.
- `autolab wake check|mark`: a turn is skipped when nothing changed (no human edit, handoff card, escalation or job state change); a turn is forced after `PI_MAX_IDLE_MIN` minutes. Polling-only turns were the main cost of long runs.
- Scheduling and wake-up tools are disabled inside turns (one had kept a session alive for 30 minutes).
- `scripts/watch_turn.sh` and `scripts/pi_log_summary.py` for monitoring.

### Human control
- `00_human/DELEGATED`: time-limited delegation of "ask me first" decisions to the PI, each logged for review.
- `ACTION_NEEDED.md` requests are blocking or optional, deduplicated, and resolved automatically when the inbox or a supplied PDF answers them.
- English human-zone templates with worked examples; `READY` / `STOP`.

### Compute
- SLURM backend: `[parts.<name>]` for independent jobs of one experiment; one unfinished job per part; committed-code check; total core-hour cap; memory billed as cores is detected per site (`mem_gb_per_core`) and large requests need a stated reason.
- Multi-node MPI jobs: `nodes`, `ntasks` (ranks) and `cpus` (per rank) in `[resources]`; per-node core check; `max_cpus = 0` / `total_core_hours = 0` mean no cap.
- Site profiles for Purdue RCAC Negishi and Gautschi, a template for other clusters, and site `notes` rendered into the agents' instructions ("Cluster notes") instead of being hard-coded.

### Agents and safety
- Human-owned configuration (`autolab.toml`, `.claude/settings*`, `.claude/agents/`, `CLAUDE.md`) cannot be modified by agents.
- `[agents] allow_bash` / `[agents] env` let the human allow extra commands (e.g. compilers for an approved external code).
- Package installs are blocked outside the project's `.venv`; the venv interpreter is allowed.
- Shell checks are per command segment and ignore quoted text (fewer false positives).
- Parallel research tracks: protocol section for running several independent experiment lines with several students per turn.

### Knowledge and claims
- Context packs accept `--include` for items keyword matching misses; plan staleness counts only new relevant items; multi-word keywords.
- Reading receipts accept heading variants; PDF text extraction (`autolab lit text`, pypdf).
- Handoff number checks are strict for experiment cards only.
- Reviewer: any command-line reviewer (`backend = "command"`); non-verdict tiers cannot issue verdicts; whole-word matching for hard topics.

## 0.1.0 — 2026-10-15

Initial framework: core / domain / site / project layering, 61 vendored open-source skills with routing, knowledge and claim gates, tiered reviewer, SLURM backend, permission hook.
