## autolab: compute backend (overrides the GPU-specific steps above)
In autolab projects every job goes through the project's scheduler adapter, not ssh/screen, Vast.ai or Modal:

1. The experiment must have `experiments/<E>/plan.md` whose plan gate passed (`autolab gate plan <E>`).
2. Put resources in the plan's `[resources]` table (cpus, mem_gb, hours, gpus, partition) and the command in `run_command`.
3. `autolab slurm validate <E>` (dry run, `sbatch --test-only`), then `autolab slurm submit <E>`.
4. Never call `sbatch`/`scancel` yourself; the hook blocks it.

"Training" in the text above means any compute job (simulation, solver run, analysis). W&B steps apply only if the plan sets `wandb = true`. Results go to `experiments/<E>/results/metrics.json` (machine-readable, every number you will quote).

For sweeps, write one experiment per configuration or a job array in `run_command`; the queue logic above (retries, waves) still applies, but submission is `autolab slurm submit`.
