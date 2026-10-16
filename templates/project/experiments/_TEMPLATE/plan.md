+++
id = "<<FILL: E001>>"
context_pack = "<<FILL: CP001 from `autolab preflight`>>"
hypotheses = ["<<FILL: H-ids from state/HYPOTHESES.md that this experiment tests>>"]
hypothesis = "<<FILL: one falsifiable sentence>>"
prediction = "<<FILL: expected value/range of the primary metric, with confidence>>"
decision_rule = "<<FILL: if X then next do A; if Y then B; if invalid then C>>"
budget_hours = 0.0
run_command = "<<FILL: command run inside experiments/<E>/ by the job script>>"
wandb = false

[resources]
partition = "cpu"
cpus = 4
mem_gb = 16
hours = 2
gpus = 0
+++

# Plan

## Prior knowledge consulted
<!-- One line per context-pack item: `- CP-ITEM-ID: how it changes this plan / why it does not apply`.
     Failed ideas and rejected claims: say exactly what is different this time. -->

## Design
- Conditions:
- Baseline / reference case (tuned with the same budget):
- Controls and sanity checks:
- Replicates and unit of replication:
- Primary metric (written to results/metrics.json):

## Precision
<!-- Required by the plan gate. Made with the `statistical-power` skill (invoke it). For the primary statistic:
     the unit of replication, planned n (seeds, runs, frames), the expected confidence-interval half-width or the
     smallest effect the design can detect, and where the variance estimate comes from (pilot, earlier experiment,
     assumption). If n is too small to decide the decision rule, say so and increase n before submitting. -->

## Pilot first
<!-- the cheap version that runs first and the signal that justifies the full run -->

## Success / kill criteria
