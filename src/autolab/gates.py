"""Claim gate: the only code path that writes findings/.

claims/<C>/claim.md (TOML frontmatter) must carry:
    statement, experiments, evidence (paths), pi_grade, and a `validation` table
    covering the domain pack's required evidence.
claims/<C>/reviews.json lists reviewer record IDs (written by `autolab review run --claim`).

Order: deterministic checks first (files exist, plans passed their gate, domain
evidence present), then the cross-model verdict. Combined grade = worse of PI and
reviewer; a two-grade split goes to the human.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from autolab.config import Config
from autolab.human import request_action
from autolab.knowledge import plan_gate_passed
from autolab.util import (next_id, now_iso, read_frontmatter, read_json, sha256_file,
                          write_atomic, write_json)

GRADES = ("A", "B", "C", "D")
COPY_LIMIT_BYTES = 50 * 1024 * 1024


def worse(a: str, b: str) -> str:
    return GRADES[max(GRADES.index(a), GRADES.index(b))]


def _reviews(cfg: Config, cdir: Path) -> list[dict]:
    ids = read_json(cdir / "reviews.json", []) or []
    out = []
    for rid in ids:
        rec = read_json(cfg.layout.reviews / f"{rid}.json")
        if rec:
            out.append(rec)
    return out


def precheck(cfg: Config, cid: str) -> dict:
    cdir = cfg.layout.claims / cid
    problems = []
    if not (cdir / "claim.md").is_file():
        return {"ok": False, "problems": [f"{cdir}/claim.md missing"], "meta": {}}
    meta, _ = read_frontmatter(cdir / "claim.md")
    if not str(meta.get("statement", "")).strip():
        problems.append("statement is empty")
    if meta.get("pi_grade") not in GRADES:
        problems.append("pi_grade must be one of A/B/C/D (PI screen)")
    for e in meta.get("evidence", []):
        if not (cfg.layout.root / e).exists():
            problems.append(f"evidence path does not exist: {e}")
    if not meta.get("evidence"):
        problems.append("no evidence listed")
    for eid in meta.get("experiments", []):
        if not plan_gate_passed(cfg, eid):
            problems.append(f"experiment {eid} has no passing plan gate (plan not pre-registered with context)")
    required = cfg.domain.get("validation", {}).get("required_evidence", [])
    given = meta.get("validation", {})
    for name in required:
        path = given.get(name)
        if not path:
            problems.append(f"domain '{cfg.domain_name}' requires validation evidence '{name}'")
        elif not (cfg.layout.root / path).exists():
            problems.append(f"validation evidence '{name}' path missing: {path}")
    return {"ok": not problems, "problems": problems, "meta": meta}


def claim_gate(cfg: Config, cid: str) -> dict:
    cdir = cfg.layout.claims / cid
    pre = precheck(cfg, cid)
    if not pre["ok"]:
        res = {"claim": cid, "decision": "blocked", "reasons": pre["problems"]}
        write_json(cdir / "gate.json", {**res, "checked": now_iso()})
        return res
    meta = pre["meta"]

    support = [r for r in _reviews(cfg, cdir) if r["task"] == "claim_support"]
    if not support:
        res = {"claim": cid, "decision": "needs_review",
               "reasons": ["no claim_support review yet (`autolab review run claim_support --claim ...`)"]}
        write_json(cdir / "gate.json", {**res, "checked": now_iso()})
        return res
    last = support[-1]
    tier_ok = cfg["reviewer"]["tiers"][last["route"]["tier"]].get("verdict_allowed")
    reasons = []
    if not tier_ok:
        decision = "needs_review"
        reasons.append("last review ran on a non-verdict tier")
    elif last.get("verdict") == "no":
        decision = "rejected"
        reasons.append("reviewer: evidence does not support the claim")
    elif last.get("verdict") == "partial":
        decision = "needs_work"
        reasons.append("reviewer: partial support — narrow the claim or add evidence")
    elif last.get("verdict") != "yes" or last.get("grade") not in GRADES:
        decision = "needs_review"
        reasons.append(f"unusable review verdict={last.get('verdict')} grade={last.get('grade')}")
    else:
        combined = worse(meta["pi_grade"], last["grade"])
        split = abs(GRADES.index(meta["pi_grade"]) - GRADES.index(last["grade"]))
        if split >= 2:
            decision = "human_decision"
            reasons.append(f"PI {meta['pi_grade']} vs reviewer {last['grade']}")
            asked = (read_json(cdir / "gate.json", {}) or {}).get("decision") == "human_decision"
            if not asked and meta.get("human_grade") not in GRADES:
                request_action(cfg, "decision",
                           f"Claim {cid}: PI graded {meta['pi_grade']}, reviewer {last['grade']}. "
                           f"Read claims/{cid}/ and set `human_grade` in claim.md.", blocking=True)
            if meta.get("human_grade") in GRADES:
                combined, decision = meta["human_grade"], None
        else:
            decision = None
        if decision is None:
            minimum = cfg["gates"]["min_grade_to_accept"]
            if GRADES.index(combined) <= GRADES.index(minimum):
                return _accept(cfg, cid, meta, combined, last)
            decision = "rejected"
            reasons.append(f"grade {combined} below minimum {minimum}")

    if decision == "rejected":
        _reject(cfg, cid, reasons)
    res = {"claim": cid, "decision": decision, "reasons": reasons}
    target = cfg.layout.rejected / cid if decision == "rejected" else cdir
    write_json(target / "gate.json", {**res, "checked": now_iso()})
    return res


def _accept(cfg: Config, cid: str, meta: dict, grade: str, review: dict) -> dict:
    fid = next_id(cfg.layout.ids, "finding")
    cdir = cfg.layout.claims / cid
    fdir = cfg.layout.findings / fid
    (fdir / "evidence").mkdir(parents=True)
    shutil.copy2(cdir / "claim.md", fdir / "claim.md")
    evidence = []
    for e in list(meta.get("evidence", [])) + list(meta.get("validation", {}).values()):
        src = cfg.layout.root / e
        entry = {"path": e}
        if src.is_file():
            entry["sha256"] = sha256_file(src)
            if src.stat().st_size <= COPY_LIMIT_BYTES:
                dst = fdir / "evidence" / e.replace("/", "__")
                shutil.copy2(src, dst)
                entry["copied"] = dst.name
        evidence.append(entry)
    record = {"id": fid, "claim": cid, "grade": grade, "statement": meta["statement"],
              "pi_grade": meta["pi_grade"], "review": review["id"],
              "reviewer_model": review["route"]["model"], "accepted": now_iso(),
              "experiments": meta.get("experiments", []), "evidence": evidence}
    write_json(fdir / "finding.json", record)
    _rebuild_index(cfg)
    res = {"claim": cid, "decision": "accepted", "finding": fid, "grade": grade, "reasons": []}
    write_json(cdir / "gate.json", {**res, "checked": now_iso()})
    return res


def _reject(cfg: Config, cid: str, reasons: list[str]) -> None:
    src = cfg.layout.claims / cid
    dst = cfg.layout.rejected / cid
    cfg.layout.rejected.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    write_atomic(dst / "REJECTED.md", f"# {cid} rejected {now_iso()}\n\n" +
                 "\n".join(f"- {r}" for r in reasons) + "\n")


def _rebuild_index(cfg: Config) -> None:
    rows = []
    for d in sorted(cfg.layout.findings.glob("F*")):
        rec = read_json(d / "finding.json")
        if rec:
            rows.append(f"| {rec['id']} | {rec['grade']} | {rec['statement']} | "
                        f"{', '.join(rec['experiments'])} | {rec['claim']} |")
    write_atomic(cfg.layout.findings / "INDEX.md",
                 "# Findings (written only by `autolab gate claim`)\n\n"
                 "| ID | Grade | Statement | Experiments | Claim |\n|---|---|---|---|---|\n" +
                 "\n".join(rows) + "\n")
