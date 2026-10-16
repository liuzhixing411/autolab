# Field notes — numerical methods and HPC algorithms

## What counts as evidence
- **Correctness first.** Every result of a new algorithm is compared, at every step, against an independent reference implementation (`[validation] reference_agreement`: a file with the number of steps checked, the number of mismatches, and how the reference was validated). One mismatch anywhere means the claim fails; record the case, do not drop it.
- **Timing protocol** (`[validation] timing_protocol`): hardware and node, compiler/interpreter versions, thread/process counts, warm-up runs, number of repeats, median and spread, fixed seeds and inputs. Time the complete pipeline (data preparation, the algorithm, communication); report each part separately as well.
- Fewer visited cells or operations is not a speedup; only measured wall-clock time is.
- Keep theoretical complexity, measured scaling and worst case as three separate statements. Never claim a worst-case bound without a proof.

## Benchmarks
- Include cases designed to break the method (adversarial or worst-case inputs), not only favorable ones; report where it loses.
- Sweep the parameters that drive cost (problem size, change rate, event frequency) and report speedup as a function of them.
- Compare against the strongest reasonable baseline, tuned with the same effort.

## Practical
- Prototype for correctness in a high-level language; move to compiled code only for measured bottlenecks. Timing claims come from the version you would publish.
- Parallel runs: check correctness at every process count; report strong and weak scaling separately; time communication on its own.
