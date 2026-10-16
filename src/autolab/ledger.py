"""Issue ledger for review loops (manuscript stage, or any multi-round review).

The ledger is the PI's private bookkeeping; it is never sent to the reviewer.
It can only DRIVE the loop (continue / escalate to the human). Acceptance stays with
the cross-model reviewer verdict plus the deterministic audits (ARIS acceptance-gate).
"""

from __future__ import annotations

from autolab.config import Config
from autolab.util import now_iso, read_json, tokens, write_json

SEVERITY = ("critical", "major", "minor")
SAME_ISSUE_JACCARD = 0.5


def _path(cfg: Config, loop: str):
    return cfg.layout.ledger / f"{loop}.json"


def load(cfg: Config, loop: str) -> dict:
    return read_json(_path(cfg, loop), None) or {"loop": loop, "rounds": [], "issues": []}


def _similar(a: str, b: str) -> bool:
    ta, tb = tokens(a), tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= SAME_ISSUE_JACCARD


def record_round(cfg: Config, loop: str, review: dict) -> dict:
    """Import one reviewer round. Matches issues to earlier ones; a fixed issue that comes
    back is counted as reopened (oscillation signal)."""
    led = load(cfg, loop)
    rnd = len(led["rounds"]) + 1
    new_ids, reopened = [], []
    for raw in review.get("issues", []):
        sev = raw.get("severity", "minor")
        match = next((i for i in led["issues"] if _similar(i["text"], raw.get("text", ""))), None)
        if match is None:
            iid = f"I{len(led['issues']) + 1:03d}"
            led["issues"].append({"id": iid, "text": raw.get("text", ""), "severity": sev,
                                  "status": "open", "first_round": rnd, "rounds": [rnd],
                                  "reopened": 0})
            new_ids.append(iid)
        else:
            match["rounds"].append(rnd)
            if match["status"] == "fixed":
                match["status"] = "open"
                match["reopened"] += 1
                reopened.append(match["id"])
            if SEVERITY.index(sev) < SEVERITY.index(match["severity"]):
                match["severity"] = sev
    led["rounds"].append({"round": rnd, "review": review.get("id"), "verdict": review.get("verdict"),
                          "new": new_ids, "reopened": reopened,
                          "new_serious": sum(1 for i in led["issues"]
                                             if i["id"] in new_ids and i["severity"] != "minor"),
                          "at": now_iso()})
    write_json(_path(cfg, loop), led)
    return led


def set_status(cfg: Config, loop: str, iid: str, status: str, note: str = "") -> dict:
    if status not in ("open", "fixed", "disputed", "wontfix"):
        raise ValueError("status: open | fixed | disputed | wontfix")
    led = load(cfg, loop)
    issue = next(i for i in led["issues"] if i["id"] == iid)
    issue["status"] = status
    if note:
        issue.setdefault("notes", []).append({"at": now_iso(), "status": status, "note": note})
    write_json(_path(cfg, loop), led)
    return issue


def check(cfg: Config, loop: str) -> dict:
    """continue | candidate_done | escalate. candidate_done still needs the reviewer's
    positive verdict and the audits; the ledger alone never accepts."""
    g = cfg["gates"]
    led = load(cfg, loop)
    rounds = led["rounds"]
    reasons = []
    serious_open = [i["id"] for i in led["issues"]
                    if i["status"] == "open" and i["severity"] != "minor"]
    if len(rounds) >= g["max_review_rounds"]:
        reasons.append(f"reached max_review_rounds={g['max_review_rounds']}")
    osc = [i["id"] for i in led["issues"] if i["reopened"] >= 2]
    if osc:
        reasons.append(f"oscillating issues (reopened twice): {osc}")
    k = g["stall_rounds"]
    if len(rounds) > k:
        tail = [r["new_serious"] for r in rounds[-(k + 1):]]
        if all(tail[j + 1] >= tail[j] > 0 for j in range(k)):
            reasons.append(f"new serious issues not decreasing over {k} rounds: {tail}")
    if reasons:
        return {"state": "escalate", "reasons": reasons, "serious_open": serious_open}
    clean_streak = 0
    for r in reversed(rounds):
        if r["new_serious"] == 0 and not r["reopened"]:
            clean_streak += 1
        else:
            break
    if not serious_open and clean_streak >= 2 and rounds and rounds[-1]["verdict"] in ("ready", "almost"):
        return {"state": "candidate_done", "reasons": ["two clean rounds, no open critical/major"],
                "serious_open": []}
    return {"state": "continue", "reasons": [], "serious_open": serious_open}
