## autolab: from verdict to the findings vault
1. Write the claim as `claims/<C>/claim.md` from claims/_TEMPLATE (statement, experiments, evidence, validation, pi_grade).
2. PI screen: set `pi_grade` (A/B/C/D) using the taste anchors in 00_human/taste/ (shared-references/taste-calibration.md).
3. Reviewer: `autolab review run claim_support --claim <C>` (tier chosen automatically).
4. `autolab gate claim <C>` decides: accepted → findings/F-id; partial → needs_work; no or low grade → claims/_rejected (kept as negative knowledge for future context packs). Only this command writes findings/.
