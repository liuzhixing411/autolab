## autolab: reviewer tier
Before the first reviewer call, run `autolab review route <task>` (add `--central` for a central claim, `--disagreement` if PI and reviewer disagreed before, `--text "<topic>"` so domain hard topics can bump the tier). Use the returned `model` and `effort` instead of the defaults above. Verdict-bearing reviews never run below `xhigh`. Pass the reviewer file paths only, never your own summary or the handoff cards.

Task name for this skill: `novelty`.
