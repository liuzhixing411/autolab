# Hard rules (take effect immediately; conflicting tasks are cancelled)

## Mode [REQUIRED]
- Mode: <<FILL: test (small trial run) or full (real research)>>
- Total budget for this run: <<FILL: e.g. API at most 30 USD, compute at most 200 core-hours>>
  <!-- Put the same numbers into autolab.toml: [budget] total_usd and [slurm] total_core_hours / max_cpus (0 = no cap). Those are enforced; this file explains them to the agents. -->

## Forbidden [REQUIRED; write "none" if nothing]
- <<FILL>>

## Standards that must hold
- 

## Ask me first
<!-- Only what must wait for you. Everything else (stopping or redirecting a sub-direction, pilots within the hard limits, drafting reports) is the PI's decision; it tells you. A request you do not answer is carried out after [autonomy] decision_timeout_hours (autolab.toml), except the keys in [autonomy] always_ask. -->
- Changing the research goal or questions
- Bringing in a new data source
- 

## Writing (only needed for the writing stage)
- Target journal / format:
- Language: English

<!-- ===== Example =====
## Mode
- Mode: test
- Total budget: API at most 20 USD; compute at most 100 core-hours; no job longer than 2 hours
## Forbidden
- Do not modify the original case files in uploads/
- Do not request GPUs
## Standards that must hold
- Every Cd needs a three-mesh GCI
- Every number goes into results/metrics.json
## Ask me first
- Extending the Re range beyond 100
- Running more than 3 research tracks in parallel
-->
