# Integrity and accounting upgrade

This update changes acceptance and unattended execution deliberately. It does not
automatically trust old approvals or turn estimated API costs into provider invoices.

## Existing projects

1. Stop the runner and wait for active agents to finish. Keep a copy of the project.
2. Review the project state, especially reviews, plan gates, jobs, and budget records.
   A snapshot cannot establish whether records were tampered with before it was taken.
3. Set `[agents] sandbox = true`. Unattended `run_pi.sh` now refuses to run without it.
   Install/configure the OS sandbox on the execution host and test a single turn.
4. Run `autolab guard snapshot` and `autolab skills mount` as the operator. The runner
   also takes a baseline and refreshes settings at startup. Do not edit protected files
   during a turn: changes without an exact trusted-CLI receipt cause a stop, even when
   they might have come from a human.
5. Revalidate experiments and record a new outcome before reviewing their claims.
   Legacy outcomes lack a version binding and cannot support new acceptances. The binding
   includes configured `slurm.clean_paths` (including shared `src` by default), the plan,
   run ledger, and results. Include other shared dependencies in `clean_paths`.
   Evidence explicitly passed to `autolab outcome --evidence` is hashed too.
6. Re-run claim reviews after updating outcomes. Claims with invalid/missing/stale outcomes,
   unverified review statements, or critical/major review issues cannot be accepted.

`analysis_command` requires a frozen evaluator. To explain a subsequent evaluator
change, add one dated line to `plan_amendments.md` containing the script path, its full
original SHA-256, its full new SHA-256, and `reason: <explanation>`. Each further version
needs its own line. This records an amendment; it is not independent approval of a
post-hoc change, and does not make exploratory results confirmatory.

## API billing

`[reviewer] billing = "api"` is the default regardless of the executable name.
Only select `billing = "subscription"` when that is actually your billing mode.
Every real review reserves cost atomically before the subprocess starts. API calls
without a configured fixed `usd_per_call` remain unknown, charged provisionally at
`unknown_call_usd` (default 1 USD). Timeouts, command errors, and malformed answers keep
their charge; they are never treated as free. Reconcile estimates with provider usage.

This is an admission check against recorded/estimated spend, not a provider-enforced
hard dollar cap. A reviewer can cost more than its estimate, and a PI turn's final cost
is not available until it ends. Use provider account limits for a strict spending ceiling.
The runner stops if cost recording or integrity verification fails.

## Recovery

- A lock is never stolen just because it is old. Inspect `owner.json`, the process and
  host, and confirm there is no live holder before removing an abandoned lock directory.
  `state/runner.lock` likewise requires checking the prior runner before recovery.
- If `experiments/<E>/submission_pending.json` remains, submission may have reached the
  scheduler even though the CLI failed. Its resource request remains reserved. Check
  the scheduler, reconcile the actual job into the job ledger (or confirm no job was
  created), and only then remove the pending marker as the operator. Do not retry blindly.
- Review `00_human/STOP` and the guard report before restarting. A guard cache failure
  cannot be bypassed by silently continuing to another paid turn.

## Boundary and deployment checks

The hook and generated sandbox protect approval records, run and cost ledgers, lock
ownership, runner logs, and the framework checkout. Exact CLI mutation receipts live
outside the project, in the guard cache; the sandbox cannot read or write that cache.
The operator and trusted CLI still run under the same account. SLURM jobs run outside
the local agent sandbox. For an adversarial agent or untrusted cluster code, deploy a
separate controller identity/service and restrict compute-job filesystem permissions.

Sandbox command exclusions use the documented `autolab *` syntax, not `Bash(autolab:*)`.
See [Claude Code sandbox configuration](https://code.claude.com/docs/en/sandboxing).
Verify the actual sandbox and scheduler on the target Linux host; Windows unit tests
exercise the controls with simulated scheduler/model interfaces, not a real cluster.
