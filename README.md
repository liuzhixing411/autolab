# autolab — an autonomous research loop for AI agents

autolab runs a small "research lab" of AI agents that works on a scientific question while you are away, and stops to ask you when a decision is yours to make.

- **PI** (Claude, default Opus): reads the state of the lab every turn, decides what to do next, writes task cards, and keeps the lab's memory in files.
- **Students** (Claude subagents, default Sonnet/Haiku): literature, experiment, analysis, figure and writing roles. They execute task cards and report back with structured handoff cards; they ask the PI only when stuck.
- **Reviewer** (a different model family, default OpenAI Codex CLI): independently checks whether evidence supports each claim, at a tier chosen by difficulty. Only claims that pass the PI *and* the reviewer become findings.
- **You** steer through one folder (`00_human/`): goals, hard rules, what counts as valuable, uploads, ad-hoc instructions, pause/resume. The agents ask you for downloads and decisions in `00_human/ACTION_NEEDED.md`.

Research methods come from mature open-source skill libraries (ARIS, K-Dense, obra/superpowers, Orchestra and others, 68 skills; see `THIRD_PARTY_NOTICES.md`). This repository adds the glue: project layout, gates, a SLURM backend, cost and compute caps, and a permission hook that keeps the agents inside their zones.

> Status: v0.2, used for real multi-day runs on Purdue RCAC clusters (Negishi). Expect rough edges; read `CHANGELOG.md`.

---

## 1. What you need

| Component | Required? | Used for | Options |
|---|---|---|---|
| **Claude Code CLI** (`claude`) | Yes | PI and students | Claude subscription login, or an Anthropic API key, or Amazon Bedrock / Google Vertex AI (see §7.2) |
| **Reviewer CLI** | Strongly recommended | Independent claim review | OpenAI Codex CLI (`codex`, default); any CLI that reads a prompt on stdin (§7.2); `mock` for tests only |
| **Linux host** with bash, git, tmux, Python ≥ 3.11 | Yes | Runs the loop and the `autolab` tool | A cluster login node, a workstation, or WSL on Windows. The framework itself needs only the Python standard library |
| **SLURM cluster** | Optional | Experiments as batch jobs | Profiles for Purdue RCAC Negishi and Gautschi are included; add your own in `sites/` (§7.1). Without a scheduler use `--site local` |
| Internet access | At install | Fetching vendored tools and `pypdf` | — |

Claude Code alone is enough to run the lab; GPT/Codex alone is not, because the PI and the students run inside Claude Code.

---

## 2. Install (once per machine)

```bash
# 1. get the framework
git clone https://github.com/liuzhixing411/autolab.git ~/autolab
cd ~/autolab

# 2. fetch pinned third-party tools (ARIS helper scripts) and pypdf into third_party/
scripts/fetch_vendor.sh

# 3. put a Python >= 3.11 and the `autolab` command on PATH (add this line to ~/.bashrc)
source ~/autolab/scripts/env.sh
autolab --help
```

Install and log in to the agent CLIs (see their own documentation for the current install command):

```bash
# Claude Code: install, then run `claude` once and log in (or export ANTHROPIC_API_KEY)
claude --version

# Codex CLI for the reviewer: install, then `codex login` (ChatGPT account or API key)
codex --version
```

If `scripts/env.sh` picks the wrong Python, export `AUTOLAB_PYTHON=/path/to/python3.11+` before sourcing it.

---

## 3. Create a project

```bash
autolab init ~/autolab-projects/my-study --domain generic --site negishi
cd ~/autolab-projects/my-study
```

- `--domain`: field pack in `domains/` (`generic`, `numerics`, `cfd`, or your own, §7.3).
- `--site`: cluster profile in `sites/` (`local`, `negishi`, `gautschi`, or your own, §7.1).

Then edit `autolab.toml` in the project:

| Section | Key | What to set |
|---|---|---|
| `[models]` | `pi`, `student_*` | Claude Code model aliases: `opus`, `sonnet`, `haiku`. `student_*` is each role's default; the PI picks a model per task by difficulty (haiku for mechanical, sonnet for standard, opus for hard or decisive work) |
| `[reviewer]` | `backend`, `command`, `[reviewer.tiers.*].model` | The reviewer CLI and the models your account offers for the light / regular / deep tiers (for Codex: `~/.codex/models_cache.json`) |
| `[budget]` | `total_usd` | Hard cap on agent spend for the whole run; the loop stops and asks you when it is reached (0 = no cap) |
| `[slurm]` | `account` | Your SLURM account (e.g. from `slist` or `sacctmgr show assoc user=$USER`) |
| `[slurm]` | `max_cpus`, `max_mem_gb`, `max_hours`, `max_gpus`, `total_core_hours` | Per-job and total compute caps (0 = no cap for `max_cpus` and `total_core_hours`) |
| `[slurm]` | `partition`, `qos`, `conda_env` | Defaults for job scripts |
| `[gates]` | `min_grade_to_accept`, `human_debounce_minutes`, … | How strict the claim gate is; how long an edit must be still before it counts |
| `[agents]` | `allow_bash`, `env` | Extra commands the agents may run without asking (e.g. compilers for an external code you approved) and extra environment variables |

After editing `autolab.toml`, run `autolab skills mount` (re-renders the agents and permissions), then:

```bash
claude            # once, interactively, in the project folder: accept the "trust this folder" prompt, then exit
autolab doctor    # every line should be [ok] except the human-input checks you have not done yet
```

The runner refuses to start in a folder Claude Code does not trust, because an untrusted folder silently ignores the project's permissions and hook.

---

## 4. Before the first run: the human zone

Everything you write lives in `00_human/`. Follow `00_human/START_HERE.md`:

| Step | File | What to write |
|---|---|---|
| 1 | `GOAL.md` | One-sentence goal, research questions, success criteria, scope, starting point, deliverable |
| 2 | `RULES.md` | Mode (test/full), budget, forbidden actions, required standards, what needs your approval ("ask me first"), writing requirements |
| 3 | `TASTE.md` (+ `taste/good/`, `taste/bad/`) | Who the work is for, what the field values, thresholds; example good/bad results to calibrate value grading |
| 4 | `uploads/` | Your data or reference results: one folder each with a `meta.toml` (copy `_TEMPLATE/`) |
| 5 | `touch 00_human/READY` | "Input complete, go". Without it the PI dispatches nothing |

Replace every `<<FILL: ...>>`; a file that still contains `<<FILL` counts as unfilled. Each template ends with a worked example inside an HTML comment.

Keep `RULES.md` and `autolab.toml` consistent: `autolab.toml` enforces the numbers, `RULES.md` explains them to the agents.

---

## 5. Start the lab

Try one turn first and read what it did:

```bash
~/autolab/scripts/run_pi.sh ~/autolab-projects/my-study --once
"$AUTOLAB_PYTHON" ~/autolab/scripts/pi_log_summary.py ~/autolab-projects/my-study/state/pi_logs/<latest>.jsonl
```

Then run it unattended inside tmux on a login node (one turn every 15 minutes):

```bash
P=~/autolab-projects/my-study
tmux new -d -s autolab-loop "bash ~/autolab/scripts/run_pi.sh $P --interval 900 >> $P/state/pi_logs/runner-loop.out 2>&1"
```

How the loop behaves:

- Each turn is a fresh `claude -p` session; the lab's memory is in files (`state/STATE.md`, task cards, handoff cards, findings).
- Before each turn `autolab wake check` looks for anything new (human edits, handoff cards, escalations, job state changes). If nothing changed, the turn is skipped at no cost; a turn is forced after `PI_MAX_IDLE_MIN` minutes (default 45).
- The PI starts students in the background and reacts to each report as it arrives (continue, redirect another student, or escalate); the turn ends when every student has reported or been stopped. A turn is killed after `PI_TURN_TIMEOUT` seconds (default 3600).
- The loop stops when `[budget] total_usd` is reached and asks you in `ACTION_NEEDED.md`.
- tmux sessions live on one login node; `state/pi_status.json` (shared file system) says whether a turn is running, on which host.

Stop the loop with `tmux kill-session -t autolab-loop` (on the same login node), or pause the agents without stopping it with `touch 00_human/STOP`.

---

## 6. While it runs: monitor and steer

| You want to | Do this |
|---|---|
| See what the lab thinks | `state/STATE.md` (the PI's memory), `state/digests/TICK-*.md` (per-turn digest) |
| See what it needs from you | `00_human/ACTION_NEEDED.md`: downloads, decisions, data. Mark done with `[x]`; answer decisions in `00_human/inbox/` |
| Check that it understood your changes | `00_human/ACK.md` |
| Give an instruction | A new `.md` file in `00_human/inbox/` (one note per file), or edit GOAL / RULES / TASTE. A change counts once the file has been still for 5 minutes |
| Supply a paper it cannot download | Save it as `00_human/pdf_drop/L00xx.pdf` (the ID from the request) |
| Pause everything | `touch 00_human/STOP` (delete to resume) |
| Let the PI decide while you are away | Create `00_human/DELEGATED` (TOML): `until = "2026-10-17T08:00:00-04:00"` and `to = "pi"`. Until then, the "ask me first" decisions are made by the PI and logged in ACTION_NEEDED for your review |
| See cost and compute | `autolab budget status`, `autolab slurm status`, `autolab slurm usage` |
| See the search and how well it goes | `state/TREE.md` (`autolab tree`), `autolab candidate list`, `autolab report metrics` |
| Inspect a turn | `scripts/pi_log_summary.py <log>`: tool calls, blocked actions, errors, final report, cost |
| Watch from another machine | `scripts/watch_turn.sh <project> <turn-number>` prints DONE / RUNNING / WAITING / STALLED / LOOP_DOWN |

Accepted results appear in `findings/` (`findings/INDEX.md`); rejected claims stay in `claims/_rejected/` as negative knowledge.

---

## 7. Adapting it

### 7.1 Another cluster

Copy `sites/_template.toml` to `sites/<name>.toml` and fill it in from `sinfo`, `sacctmgr show qos` and your center's documentation: partitions (cores and memory per node), QOS names and walltime limits, whether memory is billed as cores (`mem_gb_per_core`), Python/conda paths, scratch path, and `notes` (facts the agents should know, e.g. which QOS to use for short jobs and how to load MPI). The notes are appended to `CLAUDE.md` and to every student agent as "Cluster notes". Set `site = "<name>"` in the project's `autolab.toml` and run `autolab skills mount`, then `scripts/cluster_check.sh <account> <site>` to dry-run a job through the real scheduler (`sbatch --test-only`, nothing is submitted). Only SLURM is supported.

Jobs: an experiment's `plan.md` declares `[resources]` (`cpus`, `mem_gb`, `hours`, `qos`, optional `nodes` and `ntasks` for MPI, `mem_reason` for large memory) and optionally `[parts.<name>]` for runs that should go out as separate jobs. Agents submit only through `autolab slurm validate|submit`, which checks the plan gate, the caps, committed code and one unfinished job per part.

### 7.2 Another model provider

- **Claude via API key / Bedrock / Vertex**: Claude Code reads its usual environment variables (`ANTHROPIC_API_KEY`; `CLAUDE_CODE_USE_BEDROCK=1` with AWS credentials; `CLAUDE_CODE_USE_VERTEX=1` with `CLOUD_ML_REGION` and `ANTHROPIC_VERTEX_PROJECT_ID`). Export them in the shell that starts the loop. Keep the `[models]` aliases or use model names your provider accepts.
- **Another reviewer**: set `[reviewer] backend = "command"` and `command = [...]`, a command line in which `{model}`, `{effort}`, `{workdir}` and `{out_file}` are substituted; the prompt arrives on stdin and the answer is read from `{out_file}` (or stdout). Set the tier models accordingly. Using Claude as its own reviewer works technically but loses the independence the design relies on.
- **Cost reporting**: the budget uses the cost Claude Code reports for each turn. With a subscription this is an estimate, not an invoice; reviewer calls are reserved before execution; unknown API cost is estimated rather than treated as free. See [the accounting and integrity upgrade](docs/SECURITY_UPGRADE.md).

### 7.3 Another field

Copy `domains/_template/` to `domains/<name>/` and follow its README: title, extra skill routes, evidence a claim in this field must carry (`[validation].required_evidence`), hard topics that send reviews to the deep tier, field notes, and field-specific overlays or skills. Set `domain = "<name>"` and run `autolab skills mount`. Nothing in `core/` changes.

### 7.4 Parallel research tracks

By default the PI pursues one experiment line at a time. To let it run several independent lines, say so in `RULES.md` or an inbox note (e.g. "up to 3 tracks, each with its own experiment IDs; several students may work in one turn"). The PI then keeps a `## Tracks` table in `STATE.md` and dispatches independent students together. Expect agent cost to grow with the number of tracks.

---

## 8. How the lab works

```
human (00_human/) ──► PI turn: autolab tick → digest → decide → task cards → STATE.md
                          │                     ▲
                          ▼                     │ handoff cards (validated)
                    students (subagents) ───────┘
                          │ experiments: preflight → plan.md → plan gate → SLURM jobs
                          ▼
                    claims/<C>  ──► PI grade ──► reviewer (tier by difficulty) ──► claim gate ──► findings/<F>
                                                                                   └─► claims/_rejected/
```

- **Knowledge gate**: before any new direction, `autolab preflight` builds a context pack from failed ideas, rejected claims, findings, literature (with reading receipts for key papers) and your uploads. `plan.md` must answer every item, and `autolab gate plan` must pass before anything is submitted. Plans are locked once a job runs.
- **Skills are invoked, not just listed**: `autolab task new` writes a task card with the model for its difficulty (haiku / sonnet / opus) and the *required* skills of its route (`required` in `core/skills.toml`). Students must invoke them with the Skill tool; the handoff gate and the plan gate check the turn logs and send work back when a required skill was never invoked. Each tick digest lists the skills used.
- **Research map and memory**: `state/HYPOTHESES.md` (the hypotheses, their status and evidence; every plan names the H-ids it tests), `state/DECISIONS.md` (append-only, written by `autolab decide`), and a retrospective every `gates.retro_every` ticks whose rules (`autolab lesson add`) appear in later context packs.
- **Candidates and discrimination**: every hypothesis a plan tests has a card in `state/candidates/<H>.md` (`autolab candidate new|list|check|set`): closest prior work, mechanism difference, evidence for and against, key uncertainty, the smallest discriminating experiment, cost, and continue / pause / kill conditions. The plan gate requires the cards and a `## Discrimination` section: which explanations the experiment separates and which decision each outcome leads to. Rankings between candidates are advice only.
- **Outcomes in three layers**: `autolab outcome <E>` records engineering (valid / invalid), hypothesis (supports / refutes / inconclusive) and value (continue / refine / pivot / stop). An invalid run is rerun, never counted against the direction; pivot and stop go to you automatically (or are logged for review under DELEGATED). New plans wait until every finished experiment has an outcome. The analysis script is fingerprinted at the first submission; later changes must be explained in `plan_amendments.md`. Plans name their `parent` experiment and `autolab tree` draws the search (`state/TREE.md`).
- **Scoped lessons**: `autolab lesson add` requires the conditions a lesson was learned under (`--when data=... --when resolution=...`), its source experiment, fingerprinted evidence, a confidence and a recheck condition; `autolab lesson support|counter|retire` records later evidence. Context packs show each lesson with its scope and confidence, so a rule learned on synthetic 256^3 data is not applied silently to a real 3D case.
- **Metrics**: `autolab report metrics` (`state/METRICS.md`) reports claim send-back rate, valid hypothesis tests, repeated failures, lesson use, human corrections and cost per credible result; every PI turn and reviewer call is logged in one format in `state/calls.jsonl` (models, tokens, cost, duration, status). `docs/EVALUATION_DESIGN.md` is a design (not yet run) for comparing autolab with a plain coding agent and with its own ablations.
- **Precision before compute**: the plan gate requires a `## Precision` section (unit of replication, planned n, expected CI or detectable effect, source of the variance estimate), made with the `statistical-power` skill.
- **Jobs**: many similar runs go out as one job array (`array = "0-25%8"`); after each job the digest shows its real CPU efficiency and peak memory so the next request is sized from data.
- **Handoff cards**: students report in a fixed format (what was done, numbers with their source files, deviations, anomalies); numbers in experiment cards are checked against the result files.
- **Claim gate**: the combined grade is the worse of the PI's and the reviewer's; a large disagreement goes to you (`00_human/verdicts/<C>.toml`, tied to the claim version). Claims must name their closest prior work and the mechanism difference. Review rounds are tracked in a ledger that escalates oscillation or stalls.
- **Writing stage**: starts only after you approve readiness; the PI and the reviewer then alternate revise/review rounds until the reviewer has no remaining objections.
- **Literature**: papers get IDs and tiers; open-access PDFs are fetched and converted to text; paywalled ones are requested from you through ACTION_NEEDED.

---

## 9. Safety model

For existing projects, follow [SECURITY_UPGRADE.md](docs/SECURITY_UPGRADE.md) before restarting. Be clear about what each layer guarantees.

- **Permission lists and the PreToolUse hook (filters, not a boundary).** Unattended sessions can run only the commands listed in `.claude/settings.json` (rendered by `autolab skills mount`, extended through `[agents] allow_bash`); `sbatch`, `scancel`, `ssh`, `git push`, `rm -rf`, `sudo`, credential reads and scheduling tools are denied, and the Write/Edit tools are denied on human-owned files (`autolab.toml`, `CLAUDE.md`, `findings/`, `state/DECISIONS.md`, `00_human/`). The hook adds zone rules (locked plans, mounted skills, package installs, shell redirects into protected paths, interpreter commands that write to them). These checks read command text, so a program an agent runs can still write any file your account can write. They catch the common cases, not a determined bypass.
- **OS sandbox (required by the unattended runner).** With `[agents] sandbox = true`, Claude Code runs every Bash command and the programs it starts inside a bubblewrap sandbox (Linux; needs unprivileged user namespaces): writes to the protected paths are blocked at the OS level, writes elsewhere are limited to the project and the scratch roots, and network access is limited to `sandbox_domains`. `autolab` commands run outside it because they are the sanctioned writers (gates, decisions, requests), as do read-only scheduler queries. The settings fail closed: if the sandbox cannot start, Claude Code exits instead of running unsandboxed. Try one `--once` turn after enabling it.
- **Integrity guard.** The runner snapshots protected files outside the project. Trusted CLI calls record exact changed hashes there; later edits invalidate those receipts. Unexplained changes pause the lab, including changes made indirectly by scripts or ambiguous human edits during a turn. The runner stops on guard or cost-recording failure. Approval and budget records are checked before a CLI call consumes them.
- **Gates bound to content.** A review counts only for the version it judged: claim, evidence files, plans and experiment code are fingerprinted, and any change requires a new review. Human verdicts on PI/reviewer splits live in `00_human/verdicts/<C>.toml` and name the fingerprint they approve. Accepting the same claim version twice returns the same finding. ID counters and the ACTION_NEEDED file use cross-process locks; review records are never overwritten.
- **Caps.** API budget: reviewer admission uses provisional costs, not a provider-enforced ceiling; each PI turn is capped with `claude -p --max-budget-usd` at what is left, a turn starts only if a reserve remains, turns that end without a cost report are charged provisionally and listed for reconciliation, and reviewer calls are recorded. Compute: per-job CPU/memory/time, and a total core-hour cap that counts the remaining reservation of queued and running jobs, checked and reserved under a lock.
- **STOP** pauses every agent at once; only reading is allowed while it exists.

**Residual risks.** SLURM jobs run your code on compute nodes outside any sandbox and can write what your account can write. The guard detects unexplained changes after a turn; it cannot undo effects already produced on a compute node. Mutation receipts are not a separate OS identity. For strong isolation run the lab under a separate account or in a container whose only shared directory is the project, and keep the project in git so every change can be inspected. The agents act with your cluster and model-provider accounts: start with `mode: test`, small budgets and `--once` turns, and read what the lab did before you leave it alone.

---

## 10. Repository layout

```
autolab/
├── core/          field-neutral part: skills (verbatim upstream), routing (skills.toml), overlays, student agent templates
├── domains/       field packs: generic, numerics, cfd, _template
├── sites/         cluster profiles: local, negishi, gautschi, _template
├── templates/     new-project template (human zone, autolab.toml, CLAUDE.md protocol for the PI)
├── src/autolab/   the `autolab` tool (Python standard library only)
├── scripts/       env.sh, run_pi.sh (loop), watch_turn.sh, pi_log_summary.py, fetch_vendor.sh, cluster/reviewer checks
├── docs/          design notes (EVALUATION_DESIGN.md: comparative evaluation, not yet run)
└── tests/         interface tests: python -m unittest discover -s tests
```

## 11. Development

```bash
source scripts/env.sh
python -m unittest discover -s tests     # run on Linux; the scheduler is mocked
```

Report issues and lessons from real runs in GitHub issues; `CHANGELOG.md` lists what changed and why.

## Licence

MIT for this repository (`LICENSE`). The skills in `core/skills/` keep their own licences (MIT and Apache-2.0); see `THIRD_PARTY_NOTICES.md`.
