"""`autolab report metrics`: is the lab getting better at research, or only busier?

Counts come from the records the framework already keeps (claims, reviews, outcomes, lessons,
context packs, human verdicts, the budget ledger and the call record), so the numbers can be
compared between projects and between versions of the framework (docs/EVALUATION_DESIGN.md).
"""

from __future__ import annotations

import re
from collections import Counter

from autolab.config import Config
from autolab.util import read_frontmatter, read_json, read_jsonl, write_atomic


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


def metrics(cfg: Config) -> dict:
    L = cfg.layout
    # claims and independent review
    claims = [d for d in L.claims.glob("C*") if d.is_dir()] if L.claims.is_dir() else []
    rejected = [d for d in L.rejected.glob("C*") if d.is_dir()] if L.rejected.is_dir() else []
    findings = [read_json(d / "finding.json") or {} for d in L.findings.glob("F*")] if L.findings.is_dir() else []
    current = [f for f in findings if not f.get("superseded_by")]
    reviews = [read_json(p) or {} for p in L.reviews.glob("R*.json")] if L.reviews.is_dir() else []
    claim_reviews = [r for r in reviews if (r.get("subject") or {}).get("claim")]
    sent_back = [r for r in claim_reviews if r.get("verdict") in ("no", "partial", "unparsed")]
    rounds = Counter(r["subject"]["claim"] for r in claim_reviews)

    # experiments and outcomes
    outcomes = []
    for e in sorted(L.experiments.glob("E*")):
        outcomes += read_jsonl(e / "outcomes.jsonl")
    latest = {o["experiment"]: o for o in outcomes}
    valid_tests = [o for o in latest.values() if o["engineering"] == "valid" and o["hypothesis"] in ("supports", "refutes")]
    invalid = [o for o in outcomes if o["engineering"] == "invalid"]
    refuted = Counter(h for o in latest.values() if o["hypothesis"] == "refutes" for h in o.get("hypotheses", []))
    inv_by = Counter(o["experiment"] for o in invalid)
    from autolab import research
    no_outcome = research.missing_outcomes(cfg)

    # memory: lessons and whether plans used them
    from autolab.tasks import lessons
    ls = lessons(cfg, include_retired=True)
    packs = [read_json(p) or {} for p in L.context_packs.glob("CP*.json")] if L.context_packs.is_dir() else []
    lesson_hits = sum(1 for p in packs for it in p.get("items", []) if it.get("kind") == "lesson")
    failed_hits = sum(1 for p in packs for it in p.get("items", []) if it.get("kind") in ("failed_idea", "rejected_claim"))

    # human corrections
    verdicts = list((L.human / "verdicts").glob("C*.toml")) if (L.human / "verdicts").is_dir() else []
    actions = L.action_needed.read_text(encoding="utf-8").splitlines() if L.action_needed.is_file() else []
    decisions_asked = [a for a in actions if "[decision]" in a]
    inbox = [p for p in L.inbox.glob("*.md")] if L.inbox.is_dir() else []
    dec = (L.state / "DECISIONS.md").read_text(encoding="utf-8") if (L.state / "DECISIONS.md").is_file() else ""
    human_dec = sum(1 for by in re.findall(r"(?m)^## D\d+ .* — (\S+)\s*$", dec) if by != "PI")

    # cost
    from autolab import budget
    spent = budget.status(cfg)["spent_usd"]
    ch = sum(research.core_hours(e) for e in L.experiments.glob("E*"))
    calls = read_jsonl(L.state / "calls.jsonl")
    by_kind: dict[str, Counter] = {}
    for c in calls:
        by_kind.setdefault(c["kind"], Counter())[c["status"]] += 1
    credible = len(current)

    m = {
        "claims": {"open": len(claims), "accepted_findings": credible, "rejected": len(rejected),
                   "claim_reviews": len(claim_reviews), "sent_back": len(sent_back),
                   "send_back_rate": _ratio(len(sent_back), len(claim_reviews)),
                   "mean_review_rounds_per_claim": _ratio(sum(rounds.values()), len(rounds))},
        "experiments": {"with_outcome": len(latest), "valid_hypothesis_tests": len(valid_tests),
                        "supports": sum(o["hypothesis"] == "supports" for o in valid_tests),
                        "refutes": sum(o["hypothesis"] == "refutes" for o in valid_tests),
                        "invalid_runs": len(invalid), "repeated_invalid": {k: v for k, v in inv_by.items() if v > 1},
                        "hypotheses_refuted_more_than_once": {k: v for k, v in refuted.items() if v > 1},
                        "pivots": sum(o["value"] == "pivot" for o in outcomes),
                        "stops": sum(o["value"] == "stop" for o in outcomes),
                        "finished_without_outcome": no_outcome},
        "memory": {"lessons": len(ls), "scoped_lessons": sum(1 for l in ls if l.get("when")),
                   "lessons_with_counterexamples": sum(1 for l in ls if l["counter"]),
                   "context_packs": len(packs), "lesson_items_in_packs": lesson_hits,
                   "failed_idea_items_in_packs": failed_hits},
        "human": {"verdict_files": len(verdicts), "decision_requests": len(decisions_asked),
                  "open_requests": sum(a.startswith("- [ ]") for a in actions), "inbox_notes": len(inbox),
                  "decisions_by_human": human_dec},
        "cost": {"usd": spent, "core_hours": round(ch, 1),
                 "usd_per_accepted_finding": _ratio(spent, credible),
                 "usd_per_valid_test": _ratio(spent, len(valid_tests)),
                 "core_hours_per_valid_test": _ratio(ch, len(valid_tests))},
        "calls": {k: dict(v) for k, v in by_kind.items()},
    }
    return m


def render(m: dict) -> str:
    lines = ["# Lab metrics (generated by `autolab report metrics`)", ""]
    for sec, vals in m.items():
        lines.append(f"## {sec}")
        lines += [f"- {k.replace('_', ' ')}: {v}" for k, v in vals.items()]
        lines.append("")
    return "\n".join(lines)


def write(cfg: Config) -> dict:
    m = metrics(cfg)
    write_atomic(cfg.layout.state / "METRICS.md", render(m))
    return m
