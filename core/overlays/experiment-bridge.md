## autolab: bridge inputs and outputs
- Input plan: `experiments/<E>/plan.md` (pre-registered, gate-checked) instead of only `refine-logs/EXPERIMENT_PLAN.md`. Before writing a new plan, run `autolab preflight "<topic>" --keywords a,b,c` and answer every context-pack item in `## Prior knowledge consulted`.
- Deploy with `autolab slurm submit` (see run-experiment overlay). "GPU" means the compute resources declared in the plan.
- Sanity milestone M0 must include the domain pack's required checks (`domain.toml` → `[validation]`).
