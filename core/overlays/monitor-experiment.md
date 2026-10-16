## autolab: monitoring
Use `autolab slurm status [<E>]` and the job logs in `experiments/<E>/logs/` instead of screen sessions. Waiting costs no tokens: let `autolab tick` poll jobs and wake you through the digest. When a job finishes, write the handoff card (`experiments/<E>/handoff.md`, template in experiments/_TEMPLATE) and check it with `autolab gate handoff experiments/<E>/handoff.md`.
