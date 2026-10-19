# Changelog

## 0.6.3 — 2026-10-19

- **Human edits no longer stop the lab.** The integrity guard treated any unreceipted change in
  `00_human/` during a PI turn as tampering: an inbox note uploaded mid-turn created STOP and the
  runner exited (first-test, 2026-10-19). A human-zone change is now accepted (and reported, non-blocking)
  when the turn log shows no agent write into `00_human/`; a write tool, shell redirection or interpreter
  write aimed there is still serious (STOP). `autolab guard check` exits non-zero only for serious
  violations, so a restored configuration file no longer ends the runner either.
- `autolab human add <file> [--dest ...]`: the human's upload path into `00_human/`, leaving an
  integrity receipt; agents may not call it (hook).

## 0.6.2 — 2026-10-19

Framework problems reported by the first-test retrospective RETRO-133 (claims C009/C010 blocked):
- **Claim gate and out-of-scope items**: reviews now mark each unverified item `claimed: true|false`. Only
  items the claim asserts block acceptance; items the claim explicitly leaves out of scope are kept on the
  finding as `not_claimed_unverified`. Reviews in the old format (plain strings) still block and ask for a
  new review.
- **Failed secondary tasks**: `autolab outcome ... --failed-ok <job>_<task>=<reason>` closes a run as valid
  when only an excused array task or secondary job failed (one OOM task used to void the primary result);
  at least one non-excused task must have completed. Runs without jobs (offline analyses) are unaffected.
- **Post-outcome diagnosis**: an outcome records the code files the run used; files added later inside the
  experiment folder no longer make it stale. Changed or removed recorded files, and any change to shared
  code outside the folder, still do. Outcomes recorded before 0.6.2 keep the old check.
- `autolab decide ... --amends D<n>` for corrections instead of near-duplicate records.
- `autolab slurm validate` adds a `start_hint` when the account queue estimate is long and the job fits
  standby.
- Claim fingerprints skip optional shared paths that do not exist (no more `dependency:src: missing`).
- Timeout notices and reminders state the PI's recommendation and Chinese summary; the Weixin listener
  releases its lock on SIGHUP/SIGTERM (0.6.1 fixes, see git log).

## 0.6.0 — 2026-10-18

More autonomy for the PI. In the first test run three tracks sat idle for 37 hours behind
blocking requests without a deadline, and six Weixin answers were dropped on format:
- **Deadlines on blocking requests** (`autonomy.py`): `autolab human request --blocking` needs `--rule` and
  `--recommend`; without an answer after `[autonomy] decision_timeout_hours` (default 4) the tick closes the
  request, records the decision `by timeout`, notifies the human, and the PI carries out its recommendation
  (reversed if the human disagrees later). Keys in `[autonomy] always_ask` (default budget_increase, gpu,
  new_data_source, research_goal) and framework claim-verdict / integrity requests never time out.
  Requests filed before 0.6 get a deadline from deployment.
- **Narrower human gates**: stopping or redirecting a sub-direction (`autolab outcome ... --value stop|pivot`)
  is the PI's decision and only notifies; `--changes-goal` waits for the human. Drafting reports needs no
  approval; only the final version does. One blocking request = one gated action; everything else moves on.
- **Waiting costs less**: the wake check wakes on expired deadlines, forces turns only every
  `blocked_max_idle_min` (180) while nothing but human answers is awaited, and reminds the human every
  `reminder_hours` (runner side, no API turn).
- **Weixin**: no confirmation code; an A-number anywhere in a message answers that request, later messages
  correct earlier ones, plan changes no longer void answers, replies to closed requests and any other text
  reach the PI as notes, receipts repeat how a message was understood. Questions are answered within one
  or two minutes by a read-only `claude -p` run over the project files (`[weixin.qa]`, at most 0.5 USD per
  answer and 5 USD per day, charged to the budget). Claim grades stay bound to the claim version shown.

## 0.5.0 — 2026-10-17

Research steering, adapted from tree-search and hypothesis-tournament systems but kept auditable:
- **Candidate registry**: `state/candidates/<H>.md` cards (`autolab candidate new|list|check|set`) with closest prior work, mechanism difference, evidence for/against, key uncertainty, smallest discriminating experiment, cost and continue/pause/kill conditions. The plan gate requires a filled card for every tested hypothesis (killed ones are refused) and a `## Discrimination` section with the decision for each outcome. Rankings are advisory.
- **Frozen evaluator**: the scripts of `analysis_command` are fingerprinted at the first submission (`evaluator.json`); an outcome is refused if they changed without an explanation in `plan_amendments.md`.
- **Three-layer outcomes**: `autolab outcome <E>` (engineering valid/invalid; hypothesis supports/refutes/inconclusive/n/a; value continue/refine/pivot/stop). Invalid runs cannot carry a research conclusion or stop a line. Pivot/stop create a blocking human request (non-blocking review note under DELEGATED) and every outcome is logged with `autolab decide`. New plans are blocked while a finished experiment (or a rerun) has no outcome.
- **Search tree**: plan `parent` field; `autolab tree` writes `state/TREE.md` (outcome, core-hours and reason per experiment), refreshed every tick.
- **Scoped lessons**: lessons need conditions (`--when key=value`), a source (E/C/F/RETRO id), fingerprinted evidence, confidence and a recheck condition; `autolab lesson support|counter|retire|list`. Context packs (now also in their Markdown view) show scope and confidence.
- **Metrics and call record**: `autolab report metrics` (`state/METRICS.md`); `state/calls.jsonl` records every PI turn and reviewer call in one format (status ok/error/timeout/no_result, models, tokens, cost, duration). Job usage records allocated core-hours.
- Hook and integrity guard protect the new records (evaluator, outcomes, lessons, budget and call logs) from direct edits.
- `docs/EVALUATION_DESIGN.md`: design (not run) for comparing autolab with a plain coding agent and with ablations.

## 0.4.0 — 2026-10-17

Fixes from an external code review (all reproduced first):
- **Reviews bound to content**: a claim review stores a fingerprint of the claim (without pi_grade), its evidence files, the cited plans and the experiments' code; the claim gate accepts a review only for the current fingerprint and names what changed.
- **Human verdicts from the protected zone**: PI/reviewer splits are decided in `00_human/verdicts/<C>.toml` (grade + fingerprint); `human_grade` in claim.md is no longer read.
- **Idempotent acceptance**: the same claim version returns its existing finding; a new version supersedes the old finding.
- **Concurrency**: cross-process lock (atomic mkdir, works on NFS/GPFS) for ID counters, ACTION_NEEDED, review lists, the budget ledger and submissions; review records are written with no-overwrite.
- **Core-hour cap** counts the remaining reservation of queued and running jobs and is checked and reserved under a lock.
- **API budget**: turns without a cost report are booked as unknown (token usage kept, provisional charge, listed for reconciliation); a turn starts only if a reserve remains and is capped with `claude -p --max-budget-usd`; reviewer calls are recorded; turns are stopped with SIGINT so they report their cost.
- **Write isolation**: honest safety model in the README; optional Claude Code OS sandbox (`[agents] sandbox = true`, fail-closed, protected paths write-denied, `autolab` as the sanctioned writer); Edit/Write deny rules for protected files; post-turn integrity guard run by the runner (restores configuration changed by a tool call, pauses the lab on findings/decision-log/locked-plan/human-zone tampering); the hook also rejects interpreter commands that write to protected paths.
- **Reviewer pack**: every review gets the stage's review skills, the TASTE standard, an evidence checklist, a closest-work comparison and uncertainty rules; reviews return `closest_work` and `unverified`.
- **Novelty support**: claims need a "Closest prior work" table; context packs use stemming, Chinese bigrams and domain synonym groups.
- **Skill checks bound to the executor**: Skill calls are attributed to the student whose Agent dispatch names the task (or the PI); required skills must also leave their expected files.

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
