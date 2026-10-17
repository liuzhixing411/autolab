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

import hashlib
import json
import shutil
import tomllib
from pathlib import Path

from autolab.config import Config
from autolab.human import request_action
from autolab.knowledge import plan_gate_passed
from autolab.util import (FileLock, next_id, now_iso, read_frontmatter, read_json, sha256_file,
                          sha256_tree, write_atomic, write_json)

GRADES = ("A", "B", "C", "D")
COPY_LIMIT_BYTES = 50 * 1024 * 1024


def _code_hash(exp: Path) -> str:
    """Hash of the code and config files of an experiment (outputs excluded)."""
    from autolab.slurm import _CODE_EXT, _OUTPUT_DIRS
    h = hashlib.sha256()
    for f in sorted(x for x in exp.rglob("*") if x.is_file()):
        rel = f.relative_to(exp)
        if set(rel.parts) & _OUTPUT_DIRS:
            continue
        if f.suffix.lower() in _CODE_EXT or f.name.lower() in ("makefile", "plan.md"):
            h.update(rel.as_posix().encode())
            h.update(sha256_file(f).encode())
    return h.hexdigest()


def fingerprint(cfg: Config, cid: str) -> dict:
    """Content fingerprint of what a review judged: the claim (without pi_grade), every evidence
    and validation file, each cited experiment's locked plan and its code. A review or a human
    verdict counts only for the fingerprint it was given (a passed review used to keep approving
    after the claim and its evidence were edited)."""
    cdir = cfg.layout.claims / cid
    meta, body = read_frontmatter(cdir / "claim.md")
    stable = {k: v for k, v in meta.items() if k != "pi_grade"}
    parts = {"claim": hashlib.sha256((json.dumps(stable, sort_keys=True, default=str) + "\n" + body)
                                     .encode()).hexdigest()}
    for e in list(meta.get("evidence", [])) + list(meta.get("validation", {}).values()):
        path = cfg.layout.root / e
        parts[f"evidence:{e}"] = sha256_tree(path) if path.exists() else "missing"
    for eid in meta.get("experiments", []):
        exp = cfg.layout.experiments / eid
        gate = read_json(exp / "plan_gate.json", {}) or {}
        plan = exp / "plan.md"
        parts[f"plan:{eid}"] = gate.get("plan_hash") or (sha256_file(plan) if plan.is_file() else "missing")
        parts[f"code:{eid}"] = _code_hash(exp) if exp.is_dir() else "missing"
    fp = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    return {"fingerprint": fp, "parts": parts}


def human_verdict(cfg: Config, cid: str, fp: str) -> tuple[str | None, str]:
    """(grade, why) from 00_human/verdicts/<C>.toml, valid only if it names the current fingerprint
    (at least its first 12 characters). The human zone is not writable by agents; claim.md is."""
    p = cfg.layout.human / "verdicts" / f"{cid}.toml"
    if not p.is_file():
        return None, "no human verdict file"
    try:
        v = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return None, f"00_human/verdicts/{cid}.toml is not valid TOML: {e}"
    given = str(v.get("fingerprint", "")).strip()
    if len(given) < 12 or not fp.startswith(given):
        return None, (f"the human verdict in 00_human/verdicts/{cid}.toml is for another version of the claim "
                      f"(fingerprint {given[:16] or 'missing'}, current {fp[:16]})")
    grade = str(v.get("grade", "")).strip().upper()
    return (grade, "ok") if grade in GRADES else (None, "human verdict grade must be A/B/C/D")


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
    meta, body = read_frontmatter(cdir / "claim.md")
    from autolab.util import sections
    import re as _re
    closest = _re.sub(r"<!--.*?-->", "", sections(body).get("closest prior work", ""), flags=_re.S).strip()
    if not (_re.search(r"\bL\d{4}\b", closest) or ("none found" in closest.lower() and "search" in closest.lower())):
        problems.append("section '## Closest prior work' is missing or empty: list the closest works (L-ids) with "
                        "the mechanism difference and where the evidence shows it, or 'none found' with the search log")
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
    with FileLock(cfg.layout.findings / ".gate"):          # one acceptance at a time
        return _claim_gate(cfg, cid)


def _claim_gate(cfg: Config, cid: str) -> dict:
    cdir = cfg.layout.claims / cid
    pre = precheck(cfg, cid)
    if not pre["ok"]:
        res = {"claim": cid, "decision": "blocked", "reasons": pre["problems"]}
        write_json(cdir / "gate.json", {**res, "checked": now_iso()})
        return res
    meta = pre["meta"]

    fp = fingerprint(cfg, cid)
    support = [r for r in _reviews(cfg, cdir) if r["task"] == "claim_support"]
    if not support:
        res = {"claim": cid, "decision": "needs_review",
               "reasons": ["no claim_support review yet (`autolab review run claim_support --claim ...`)"]}
        write_json(cdir / "gate.json", {**res, "checked": now_iso()})
        return res
    last = support[-1]
    tier_ok = cfg["reviewer"]["tiers"][last["route"]["tier"]].get("verdict_allowed")
    reasons = []
    reviewed = last.get("subject") or {}
    if reviewed.get("claim") != cid or reviewed.get("fingerprint") != fp["fingerprint"]:
        old = reviewed.get("parts", {})
        changed = sorted(k for k in set(old) | set(fp["parts"]) if old.get(k) != fp["parts"].get(k))
        decision = "needs_review"
        reasons.append("the last claim_support review was for another version of this claim "
                       + (f"(changed: {', '.join(changed[:8])})" if old else "(the review has no fingerprint)")
                       + "; run the review again")
    elif not tier_ok:
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
            hgrade, why = human_verdict(cfg, cid, fp["fingerprint"])
            if hgrade:
                combined, decision = hgrade, None
            else:
                if why != "no human verdict file":
                    reasons.append(why)
                request_action(cfg, "decision",
                               f"Claim {cid}: PI graded {meta['pi_grade']}, reviewer {last['grade']} (claim "
                               f"fingerprint {fp['fingerprint'][:16]}). Read claims/{cid}/ and the review; to decide, "
                               f"write 00_human/verdicts/{cid}.toml with grade = \"<A|B|C|D>\" and fingerprint = "
                               f"\"{fp['fingerprint'][:16]}\". Any later change to the claim or its evidence voids it.",
                               blocking=True, dedupe=f"{fp['fingerprint'][:16]}")
        else:
            decision = None
        if decision is None:
            minimum = cfg["gates"]["min_grade_to_accept"]
            if GRADES.index(combined) <= GRADES.index(minimum):
                return _accept(cfg, cid, meta, combined, last, fp)
            decision = "rejected"
            reasons.append(f"grade {combined} below minimum {minimum}")

    if decision == "rejected":
        _reject(cfg, cid, reasons)
    res = {"claim": cid, "decision": decision, "reasons": reasons}
    target = cfg.layout.rejected / cid if decision == "rejected" else cdir
    write_json(target / "gate.json", {**res, "checked": now_iso()})
    return res


def _accept(cfg: Config, cid: str, meta: dict, grade: str, review: dict, fp: dict) -> dict:
    cdir = cfg.layout.claims / cid
    supersedes = None
    for d in sorted(cfg.layout.findings.glob("F*")):          # idempotent per claim version
        old = read_json(d / "finding.json") or {}
        if old.get("claim") == cid and not old.get("superseded_by"):
            if old.get("fingerprint") == fp["fingerprint"]:
                res = {"claim": cid, "decision": "accepted", "finding": old["id"], "grade": old["grade"],
                       "reasons": ["this version of the claim was already accepted"]}
                write_json(cdir / "gate.json", {**res, "checked": now_iso()})
                return res
            supersedes = old
    fid = next_id(cfg.layout.ids, "finding")
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
              "experiments": meta.get("experiments", []), "evidence": evidence,
              "fingerprint": fp["fingerprint"], "fingerprint_parts": fp["parts"]}
    if supersedes:
        record["supersedes"] = supersedes["id"]
        write_json(cfg.layout.findings / supersedes["id"] / "finding.json", {**supersedes, "superseded_by": fid})
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
        if rec and not rec.get("superseded_by"):
            rows.append(f"| {rec['id']} | {rec['grade']} | {rec['statement']} | "
                        f"{', '.join(rec['experiments'])} | {rec['claim']} |")
    write_atomic(cfg.layout.findings / "INDEX.md",
                 "# Findings (written only by `autolab gate claim`)\n\n"
                 "| ID | Grade | Statement | Experiments | Claim |\n|---|---|---|---|---|\n" +
                 "\n".join(rows) + "\n")
