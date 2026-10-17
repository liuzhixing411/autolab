## autolab: reviewing a claim or a draft with this rubric (no ARA directory here)
autolab projects are not ARA directories. Skip Level 1 structural validation and map the read order onto the project:
- `PAPER.md` → the claim under review, `claims/<C>/claim.md` (statement, scope, evidence list, limits); in the writing stage, the paper draft.
- `logic/claims.md` → `claims/<C>/claim.md` plus the accepted findings it builds on (`findings/INDEX.md`).
- `logic/experiments.md`, `logic/problem.md` → `experiments/<E>/plan.md` of every experiment the claim cites (hypothesis, prediction, decision rule, Precision) and `00_human/GOAL.md`.
- `logic/related_work.md` → `literature/REVIEW.md` and the reading receipts `literature/notes/<L>.md`.
- `trace/exploration_tree.yaml` → `state/DECISIONS.md`, `state/HYPOTHESES.md` (refuted and parked entries), `claims/_rejected/` and the research wiki's failed ideas: these are the dead ends and decisions.
- `evidence/` → the result files listed in the claim's `evidence` (metrics.json, CSV tables, analysis outputs); spot-check 2-3 numbers against them.
Score the six dimensions as written. Write the report as `claims/<C>/rigor_review.json` (draft stage: `paper/rigor_review.json`), not at an artifact root. Every critical or major finding becomes a required fix before the claim goes to the reviewer and the gate; record the outcome with `autolab decide`.
