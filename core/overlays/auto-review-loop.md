## autolab: stop rules for the manuscript loop
Keep the cross-model acceptance rule above. In addition, after each round record it: `autolab review run review_loop_round <files> --loop paper` (or import the round), and mark fixes with `autolab ledger set paper --issue I0xx --status fixed`. Then `autolab ledger check paper`:
- `escalate` (max rounds, an issue reopened twice, or new serious issues not decreasing) → stop and write a blocking request with `autolab human request --kind decision --blocking`.
- `candidate_done` is necessary but not sufficient: you still need the reviewer's positive verdict, passing paper-claim-audit / citation-audit / integrity-forensics, and a final fresh `final_cold_read` review.
MAX_ROUNDS for autolab projects is `gates.max_review_rounds` in autolab.toml. Never pass the ledger or your change notes to the reviewer.
