# Changelog

## 0.3.2 — 2026-10-17

- Three more mature MIT skills, with overlays that map them onto autolab files: `brainstorming-research-ideas` and `ara-rigor-reviewer` (Orchestra-Research/AI-Research-SKILLs) and K-Dense `literature-review`. Routes: PI ideation and retrospectives, PI claim review and manuscript review, new student tasks `review` (systematic literature review) and `rigor` (six-dimension rigour review of a claim).

## 0.3.1 — 2026-10-16

From the first automatic retrospective and a blocked run:
- **Resource overrides after the plan lock**: `experiments/<E>/resources_override.toml` (`reason` plus `[resources]` / `[parts.<p>.resources]`, resource keys only). A replay part had been blocked for hours because its locked plan asked for too much memory.
- **Clearer commit check**: submit lists the uncommitted code and config files (outputs ignored) and says who commits them and how; the default checked paths are the experiment folder and `src/`.
- **Scripts must exist**: scripts named in `run_command` or `analysis_command` must exist before a job is submitted (a plan had named an analysis script nobody wrote).
- **Idle-core warning**: requesting several cpus for a command without a parallel launcher or thread setting warns at validate/submit (earlier jobs used 3-11 % of their cores); `cpus_reason` documents deliberate reservations.

## 0.3.0 — 2026-10-16

- **Skills are actually used.** In a 32-turn run the Skill tool was never called: students had no Skill tool and only a menu of names. Students now have the tool; routes in `core/skills.toml` have `required` skills; `autolab task new` puts them in the task card; the handoff and plan gates verify invocations in the turn logs; the tick digest reports skill use.
- **Event-driven students.** The PI dispatches as many students as there are ready, independent tasks, in the background, and reacts to each report (continue, stop another student with `TaskStop`, escalate). The runner waits for background students (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`) instead of stopping them 10 minutes after the PI's last message.
- **Model by difficulty**: `--difficulty mechanical|standard|hard` maps to haiku / sonnet / opus per task; the PI keeps all decisions.
- **Hypothesis map and decision log**: `state/HYPOTHESES.md` (plans must name existing H-ids), append-only `state/DECISIONS.md` via `autolab decide`.
- **Precision section** required by the plan gate (planned n and expected CI), with the `statistical-power` skill.
- **Job arrays** (`array = "0-25%8"`) with array-aware state and caps; **usage records** (CPU efficiency, peak memory) after every job, shown in the digest.
- **Retrospectives** every `gates.retro_every` ticks; `autolab lesson add` rules feed later context packs.
- **New skills** from obra/superpowers (MIT): systematic-debugging, verification-before-completion, test-driven-development, dispatching-parallel-agents, with research-code overlays.
- Inbox files named after a request (e.g. `...-A016-....md`) close it automatically.

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
