# Field notes — CFD

Read once per session. Append solved problems to experience/error-cookbook.jsonl, not here; this file changes only by human review.

## What counts as evidence
- Grid (mesh) convergence on at least three systematically refined meshes; report observed order and GCI (Roache). A claim without it cannot pass the gate (`[validation] mesh_convergence`).
- Transient runs: time-step convergence or a Courant-number argument.
- Validation against experiment or a recognised benchmark when one exists (uploads with `use_as = "validation_reference"` are the first place to look).
- Separate verification (solving the equations right) from validation (solving the right equations).

## Health checks while a run is going (replace W&B checks)
- Residuals: falling and flat at the target level; oscillating residuals mean the answer is not converged.
- Courant number within the scheme's limit; mass/volume-fraction conservation for multiphase.
- Monitor points / integral quantities reach a steady or statistically stationary state before averaging.

## Practical
- Pilot = coarse mesh + short physical time. Scale up only when the pilot shows the expected flow features.
- Solver output creates many small files; on clusters with file-count quotas, write fewer time directories and archive finished cases.
- Record solver, version, schemes, tolerances and mesh statistics in results/metrics.json with the results.
