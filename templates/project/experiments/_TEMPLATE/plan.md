+++
id = "<<FILL: E001>>"
context_pack = "<<FILL: CP001 from `autolab preflight`>>"
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

## Pilot first
<!-- the cheap version that runs first and the signal that justifies the full run -->

## Success / kill criteria
