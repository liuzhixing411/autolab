## autolab: debugging research code
- `superpowers:<name>` above means the mounted skill `<name>` (e.g. `test-driven-development`, `verification-before-completion`); invoke it with the Skill tool.
- Reproduce on the smallest input that still fails (a tiny grid, one seed, a few steps) before reading large logs; keep that case as a regression test.
- Compare against an independent reference (a brute-force or library implementation, a conserved quantity, a known analytic case) to locate where results diverge.
- Never make a failure disappear by changing the plan's metric, tolerance or data; record the root cause and the fix in the handoff card under Anomalies.
- Long reproductions run as SLURM jobs, not on the login node.
