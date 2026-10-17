# Comparative evaluation of autolab: design

Status: **design for review, not run.** Nothing in this document has been executed. It needs the
maintainer's approval before any budget is spent.

## 1. Question

Does the autolab structure (gates, independent reviewer, memory, candidate search) produce more
credible research per unit of cost than a capable coding agent working alone, and which of its
parts are responsible? A single successful project cannot answer this: the outcome depends on the
task, the model and luck. We therefore compare *arms* on the same tasks, with the same model and
the same budget, and measure process and outcome separately.

## 2. Arms

All arms use the same models (PI/agent: Claude Opus; students: the same model mix; reviewer where
present: the same Codex model), the same cluster site file, the same API budget and core-hour budget
per task, and the same wall-clock limit.

| Arm | Description |
|---|---|
| **A0 plain agent** | One Claude Code session (`claude -p` loop with the same runner and turn limit), the same task text, GOAL, RULES and data, the same SLURM access through plain `sbatch` wrappers. No gates, no reviewer, no context packs, no candidate cards, no lessons. It may write any notes it likes. |
| **A1 autolab full** | The current release, all gates on. |
| **A2 no memory** | A1 without lessons, failed-idea and rejected-claim items in context packs (`experience/` and `wiki/ideas` empty and not written), and without the retrospective. |
| **A3 no independent reviewer** | A1 with the claim gate accepting the PI's own grade (reviewer backend `mock`, verdict = PI grade). Everything else on. |
| **A4 no candidate search** | A1 with `gates.require_candidates = false` and no `## Discrimination` requirement; outcomes and tree still recorded. |

A2-A4 are ablations: each removes one component so that its contribution can be estimated. If the
budget allows only part of the design, run A0, A1 and A3 first (the reviewer is the most expensive
component and the one whose value is least obvious).

## 3. Tasks

Use tasks with a **known answer or an external check**, so outcome quality does not depend only on
our own judgement. Each task is a fixed bundle: GOAL, RULES, TASTE, data, starter code, and a
hidden answer key held by the maintainer.

1. **Replication tasks (known answer).** Published results with open code and data where a key
   number or conclusion is known (e.g. a benchmark speedup, a convergence order, a scaling law).
   The agent receives the paper's question but not its answer. Score: whether the final claim
   matches the answer key within its stated uncertainty.
2. **Planted-flaw tasks.** A starter code base with a known flaw that produces an attractive but
   wrong result (a timing measurement that includes caching effects, a leak between training and
   test data, an analysis that peeks at the outcome). Score: whether the arm reports the
   attractive wrong result as a finding.
3. **Open tasks (no known answer).** Small open questions from the maintainer's own area (e.g. a
   variant of the connected-component-labelling problem of the first project). Score: expert blind
   review (section 5).

Suggested size for a first round: 3 replication, 2 planted-flaw and 2 open tasks, 3 independent
runs per arm and task (different seeds for the agent's sampling where the API allows, otherwise
different start times). With five arms this is 7 x 5 x 3 = 105 runs; the reduced round (A0, A1, A3)
is 63 runs.

## 4. Metrics

Measured automatically from the project records (`autolab report metrics`, `state/calls.jsonl`,
`state/budget.jsonl`; for A0 the same quantities are extracted from its logs and files by a script
written before the runs):

| Metric | Definition |
|---|---|
| Correct-claim rate | accepted findings that agree with the answer key / accepted findings (replication tasks) |
| False-finding rate | accepted findings that are wrong or rest on the planted flaw / accepted findings |
| Flaw detection | planted flaw found and reported (yes/no, and turn at which it was found) |
| Valid hypothesis tests | experiments with engineering = valid and hypothesis = supports or refutes |
| Wasted runs | invalid runs, and repeated runs of an already refuted configuration |
| Cost per credible result | API USD and core-hours per accepted finding that survives expert review |
| Human interventions | blocking requests, human verdicts, inbox notes needed to keep the run going |
| Time to first credible result | wall-clock and API USD until the first finding that survives review |
| Claim send-back rate | reviewer verdicts no/partial over all claim reviews (A1, A2, A4 only) |

## 5. Expert blind review

For every run, the final state (accepted findings with their evidence, the plan of the next step,
and for open tasks the proposed idea or direction) is exported to a neutral format that hides the
arm: the same Markdown layout, framework-specific file names and wording removed, IDs renumbered.
Two or three domain experts who did not see the runs score each export on:

- **Correctness** (1-5): are the claims supported by the evidence shown?
- **Novelty** (1-5): relative to the closest prior work the expert knows (the expert also checks
  the closest-work table given by the run).
- **Significance** (1-5): would this matter to the field if true?
- **Clarity of uncertainty** (1-5): are limits, failed attempts and the regime of validity stated?
- **Would you continue this line?** (yes / no / with changes).

Experts score independently; agreement is reported (weighted kappa). The order of exports is
randomised per expert. Experts also mark any export they believe they can attribute to an arm, so
the blinding can be checked.

## 6. Analysis plan (fixed before the runs)

- Unit of analysis: one run. Primary comparison A1 vs A0 on false-finding rate and cost per
  credible result; secondary comparisons A1 vs each ablation.
- Report each metric per arm with 95 % bootstrap intervals over runs (resampling tasks and runs
  hierarchically). With 3 runs x 7 tasks the design can detect only large differences (roughly a
  halving of the false-finding rate); say so in any report, and treat the first round as a pilot
  that sizes a second round.
- No metric is dropped or added after the data are seen; any change goes into an amendment file
  with its reason before the analysis is rerun.

## 7. Cost estimate (to check before approval)

From the first project: a PI turn costs roughly 2-6 USD with students; a useful run of a small
task needs about 20-60 turns. That suggests 50-300 USD per run, i.e. about 3,000-20,000 USD for
the reduced round and 5,000-30,000 USD for the full round, plus cluster time. A cheaper pilot:
one replication task and one planted-flaw task, arms A0 and A1, 2 runs each (8 runs, about
400-2,400 USD), mainly to test the export, the metric extraction and the blinding.

## 8. Threats to validity

- **Task selection**: tasks that suit structured workflows favour autolab. Mitigation: tasks are
  chosen and frozen before any run, including at least one task where a quick single answer is
  enough.
- **Contamination**: replication answers may be in the model's training data. Mitigation: prefer
  results published after the model's training cutoff, or perturbed variants with a recomputed
  answer.
- **Prompt effort**: autolab's protocol is long and tuned; A0 gets the same task text but no tuning.
  Mitigation: give A0 a short, reasonable research-practice instruction written before the runs,
  and report its text.
- **Budget accounting**: subscription logins report estimated costs. Mitigation: use API keys with
  billed usage for all arms, or compare token counts.
- **Human influence**: interventions differ between arms. Mitigation: a fixed intervention policy
  (only answer blocking requests, with scripted answers where possible) and report every
  intervention.

## 9. Decisions needed from the maintainer

1. Approve the arms (all five, or the reduced A0/A1/A3 round first).
2. Choose the tasks and supply the answer keys; name the experts.
3. Set the budget for the pilot and for the full round.
4. Decide whether results will be published (then pre-register sections 3-6).
