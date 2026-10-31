"""Novelty audits: has this been done, and what exactly would be new?

first-test spent about 490 USD and 5600 core-hours with no such check after day 1 and ended with "the
report makes no claim of a new principle", although the human's TASTE ranks an independent
contribution third. Its `novelty-check` skill was never invoked, and could not have run as written
(it searches ML venues and calls a tool the lab does not have).

An audit judges contribution statements against papers, one paper at a time:
- retrieval is open: `autolab lit snowball|search|snippets`. Asta's Theorizer compares a law only
  with the papers it was built from, so its tool cannot answer "has anyone done this";
- the scale comes from Theorizer: seven novelty types, four degrees (references/novelty-scale.md);
- the verdict limits come from ARIS `novelty-check`: ABANDON must name a paper, neighbours do not
  kill an idea.

Decoys make the audit testable: known results taken from the lab's own reading notes are mixed in
blind among the statements, and an audit that does not recognise them as known is void, in the
same spirit as a detector that must catch its mutant. The mapping lives in the guard folder, which
agents cannot read. Nothing here can prove that nobody did it; the record says what was searched.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from autolab.config import Config
from autolab.util import (next_id, now_iso, overlap, read_frontmatter, read_json, read_jsonl, sections,
                          write_atomic, write_json)

TYPES = ("phenomenon_effect", "explanatory_mechanistic", "unification", "generalization_scope_expansion",
         "constraint_limitation", "conceptual_reframing_abstraction", "empirical_synthesis_meta_regularity")
DEGREES = ("explicit_established", "explicit_peripheral", "derivable_unstated", "genuinely_new")
KNOWN = DEGREES[:2]                     # Theorizer's binary reading: these two are "not novel"
VERDICTS = ("PROCEED", "CAUTION", "ABANDON")
READ_LEVELS = ("full", "skim", "abstract")
NONE_CLAIMED = "none-claimed"           # claim.md `novelty`: a measurement that claims nothing new

REPORT = """# Novelty audit {nid}

## What was searched
<!-- sources, queries, seed papers, dates; the search log has the details. What could not be read and why. -->

## Closest prior work
<!-- for each statement: the closest paper, what it does, and the difference one could verify -->

## Positioning
<!-- the delta in one sentence a referee could check -->

## Risks
<!-- what a referee would cite against this; concurrent work -->
"""


def _dir(cfg: Config, nid: str) -> Path:
    return cfg.layout.novelty / nid


def _secret(cfg: Config, nid: str) -> Path:
    from autolab import guard
    return guard._dir(cfg) / "novelty" / f"{nid}.json"


def load(cfg: Config, nid: str) -> dict:
    audit = read_json(_dir(cfg, nid) / "audit.json")
    if audit is None:
        raise KeyError(f"no novelty audit {nid}")
    return audit


def _key_points(cfg: Config, lid: str) -> list[str]:
    p = cfg.layout.lit_notes / f"{lid}.md"
    if not p.is_file():
        return []
    try:
        sec = sections(read_frontmatter(p)[1])
    except ValueError:                          # a damaged note is not a reason to stop an audit
        return []
    text = next((t for name, t in sec.items() if name.startswith("key points")), "")
    points = [_plain(s[2:]) for s in map(str.strip, text.splitlines()) if s.startswith("- ")]
    return [p for p in points if _is_claim(p)]


def _plain(point: str) -> str:
    """A note's bullet as a bare statement: page marks and paper ids would give a control away."""
    text = re.sub(r"\s*\([^()]*(?:\bpp?\.|§|\bEqs?\.|\bSec\.?|\bFig\.?)[^()]*\)", "", point)
    text = re.sub(r"\[?\bL\d{4}\b[^\]\s]*\]?", "", text)
    text = re.sub(r"^[^:.]{1,30}:\s+", "", text.strip())        # "Upper bound A: ..." -> "..."
    text = " ".join(text.split())[:300]
    return text[:1].upper() + text[1:]


_VERB = re.compile(r"\b(?:is|are|can|must|cannot|maintains?|requires?|needs?|gives?|shows?|proves?|holds?|takes?|uses?|reduces?|"
                   r"solves?|detects?|tracks?|scales?|depends?|yields?|runs?|stores?|supports?|answers?|identifies|produces?|"
                   r"follows?|grows?|falls?|preserves?|increases?|decreases?)\b")
_NOTE_TALK = re.compile(r"\b(?:none stated|not stated|not read|not analysed|not given|no timing|see above|receipt|full text|"
                        r"this report|the report|the paper|the authors|the review|our|we)\b", re.I)


def _is_claim(text: str) -> bool:
    """Reading notes also hold fragments and remarks about the paper ("none stated (no MPI)"); a
    control must read like a claim, or the student recognises it for what it is. Found on first-test."""
    return len(text) >= 80 and bool(_VERB.search(text)) and not _NOTE_TALK.search(text) and "?" not in text


def _pick_decoys(cfg: Config, nid: str, statements: list[str], sources: list[str], n: int) -> list[tuple[str, str]]:
    """(known result, source paper): related papers first, because a control from another topic
    says nothing about this search."""
    from autolab.knowledge import lit_records
    rng = random.Random(nid)
    ids = sources or [r["id"] for r in lit_records(cfg)]
    scored = []
    for lid in ids:
        points = _key_points(cfg, lid)
        if points:
            scored.append((overlap(statements, " ".join(points)), lid, points))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(rng.choice(points), lid) for score, lid, points in scored[:n] if score or sources]


def new(cfg: Config, target: str, statements: list[str], ctype: str = "", decoy_from: list[str] | None = None,
        no_decoy_why: str = "") -> dict:
    """Open an audit for a candidate (H-id), a claim (C-id) or the whole `project`."""
    statements = [s.strip() for s in statements if s.strip()]
    if not statements:
        raise ValueError("give at least one contribution statement (--statement)")
    if ctype and ctype not in TYPES:
        raise ValueError(f"--type must be one of {TYPES}")
    if re.fullmatch(r"H\d+[a-z]?", target):
        from autolab.research import candidate
        if candidate(cfg, target) is None:
            raise KeyError(f"no candidate card state/candidates/{target}.md")
    elif re.fullmatch(r"C\d+", target):
        if not (cfg.layout.claims / target).is_dir():
            raise KeyError(f"no claim {target}")
    elif target != "project":
        raise ValueError("an audit is for a candidate (H14), a claim (C003) or `project`")
    n = int(cfg["gates"]["novelty_decoys"])
    tag = hashlib.sha256(("\n".join(statements) + now_iso()).encode()).hexdigest()
    decoys = [] if (no_decoy_why or not n) else _pick_decoys(cfg, tag, statements, decoy_from or [], n)
    if n and not decoys and not no_decoy_why:
        raise ValueError("no control statement available: read related papers and write their `## Key points` "
                         "first (`autolab lit note`), or name them with --decoy-from; a human-approved audit "
                         "without controls takes --no-decoy \"<reason>\"")
    items = [("real", t, "") for t in statements] + [("decoy", t, lid) for t, lid in decoys]
    random.Random(tag).shuffle(items)
    nid = next_id(cfg.layout.ids, "novelty")
    rows = [{"sid": f"S{i}", "text": t} for i, (_k, t, _l) in enumerate(items, 1)]
    write_json(_secret(cfg, nid), {"real": [f"S{i}" for i, it in enumerate(items, 1) if it[0] == "real"],
                                   "decoys": {f"S{i}": it[2] for i, it in enumerate(items, 1) if it[0] == "decoy"},
                                   "type": ctype})
    audit = {"id": nid, "for": target, "created": now_iso(), "statements": rows,
             **({"no_decoy_why": no_decoy_why} if no_decoy_why else {})}
    d = _dir(cfg, nid)
    write_json(d / "audit.json", audit)
    write_atomic(d / "report.md", REPORT.format(nid=nid))
    return audit


def _clean(text: str) -> str:
    """Quote matching survives line breaks, hyphenation, ligatures and math italics from PDF extraction."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    return re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "", text)).strip().lower()


def _quote_state(cfg: Config, paper: str, quote: str) -> str:
    f = cfg.layout.literature / "text" / f"{paper}.txt"
    if not f.is_file():
        return "unverifiable"
    return "found" if _clean(quote) in _clean(f.read_text(encoding="utf-8", errors="ignore")) else "missing"


def fingerprint(cfg: Config, nid: str) -> str:
    h = hashlib.sha256()
    for name in ("audit.json", "judgments.jsonl", "report.md"):
        p = _dir(cfg, nid) / name
        h.update(name.encode())
        h.update(p.read_bytes() if p.is_file() else b"missing")
    return h.hexdigest()


def _search_problems(cfg: Config, created: str) -> tuple[list[str], list[str], dict]:
    from autolab.scholar import search_log
    log = [r for r in search_log(cfg) if r.get("at", "") >= created]
    snow = [r for r in log if r["kind"] == "snowball" and r.get("candidates")]
    found = [r for r in log if r["kind"] == "search" and r.get("hits") and len(r.get("failed") or []) < 2]
    text = [r for r in log if r["kind"] == "snippets" and r.get("hits")]
    problems, warnings = [], []
    if not snow:
        problems.append("no citation snowball since this audit opened: `autolab lit snowball --seeds <closest papers> "
                        "--keywords ... --audit <N>`")
    if not found:
        problems.append("no catalogue search since this audit opened: `autolab lit search \"<query>\" --audit <N>`")
    if not text:
        warnings.append("no full-text passages since this audit opened (`autolab lit snippets`); say so under 'What was searched'")
    return problems, warnings, {"snowballs": len(snow), "searches": len(found), "snippet_searches": len(text),
                                "candidates": sum(r.get("candidates", 0) for r in snow)}


def _check_row(cfg: Config, i: int, r: dict, sids: set[str], papers: set[str], problems: list[str],
               warnings: list[str]) -> None:
    at = f"judgments.jsonl line {i}"
    if r.get("sid") not in sids:
        problems.append(f"{at}: unknown statement {r.get('sid')}")
    if r.get("paper") not in papers:
        problems.append(f"{at}: paper {r.get('paper')} is not in the literature index (`autolab lit add`)")
    if r.get("read_level") not in READ_LEVELS:
        problems.append(f"{at}: read_level must be one of {READ_LEVELS}")
    known = False
    for t, v in (r.get("types") or {}).items():
        if t not in TYPES:
            problems.append(f"{at}: unknown novelty type {t}")
        elif v is not None:
            if v.get("degree") not in DEGREES:
                problems.append(f"{at}: {t}: degree must be one of {DEGREES}")
            for field in ("known", "introduced", "novel"):
                if not str(v.get(field, "")).strip():
                    problems.append(f"{at}: {t}: fill `{field}`")
            known = known or v.get("degree") in KNOWN
    if known:                                   # prior art is claimed: it must be quotable
        quote = str(r.get("quote", "")).strip()
        state = _quote_state(cfg, r.get("paper", ""), quote) if quote else "absent"
        if state in ("absent", "missing"):
            problems.append(f"{at}: a known result needs `quote`, found verbatim in literature/text/{r.get('paper')}.txt ({state})")
        elif state == "unverifiable":
            warnings.append(f"{at}: quote of {r.get('paper')} cannot be checked (no full text)")


def lowest(rows: list[dict]) -> dict[str, dict]:
    """type -> the lowest degree any paper reached, and that paper: one paper that already has it decides."""
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        for t, v in (r.get("types") or {}).items():
            if v and v.get("degree") in DEGREES:
                d = DEGREES.index(v["degree"])
                if t not in out or d < out[t][0]:
                    out[t] = (d, r.get("paper", ""))
    return {t: {"degree": DEGREES[d], "paper": p} for t, (d, p) in out.items()}


def check(cfg: Config, nid: str) -> dict:
    """Deterministic checks: search record, coverage per statement, quotes, blind controls. The
    result never says which statement is a control."""
    from autolab.knowledge import lit_records
    audit = load(cfg, nid)
    problems, warnings, searched = _search_problems(cfg, audit["created"])
    try:
        rows = read_jsonl(_dir(cfg, nid) / "judgments.jsonl")
    except json.JSONDecodeError as e:
        return {"id": nid, "ok": False, "problems": [f"judgments.jsonl is not valid JSON lines: {e}"], "warnings": []}
    sids = {s["sid"] for s in audit["statements"]}
    records = lit_records(cfg)
    papers = {r["id"] for r in records}
    for i, r in enumerate(rows, 1):
        _check_row(cfg, i, r, sids, papers, problems, warnings)
    judged = {r.get("paper") for r in rows}
    idle = [r["id"] for r in records if r.get("added", "") >= audit["created"] and r["id"] not in judged]
    if idle:
        warnings.append(f"registered during this audit, not judged: {', '.join(idle)}; judge them or say why not in the report")
    need_all, need_full = int(cfg["gates"]["novelty_min_papers"]), int(cfg["gates"]["novelty_min_full_text"])
    summary, coverage = {}, {}
    for sid in sorted(sids, key=lambda s: int(s[1:])):
        mine = [r for r in rows if r.get("sid") == sid]
        full = {r.get("paper") for r in mine if r.get("read_level") == "full"}
        coverage[sid] = {"papers": len({r.get("paper") for r in mine}), "full_text": len(full)}
        if coverage[sid]["papers"] < need_all:
            problems.append(f"{sid}: judged against {coverage[sid]['papers']} papers, at least {need_all} needed")
        if len(full) < need_full:
            problems.append(f"{sid}: {len(full)} papers read in full, at least {need_full} needed")
        summary[sid] = lowest(mine)
    secret = read_json(_secret(cfg, nid), {}) or {}
    missed = [s for s in secret.get("decoys", {}) if not any(v["degree"] in KNOWN for v in summary.get(s, {}).values())]
    if missed:
        problems.append("the control statements (published results mixed in blind) were not all recognised as known: "
                        "search and read every statement again")
    report = _dir(cfg, nid) / "report.md"
    body = re.sub(r"<!--.*?-->", "", report.read_text(encoding="utf-8"), flags=re.S) if report.is_file() else ""
    filled = sections(body)
    empty = [h for h in ("what was searched", "closest prior work", "positioning", "risks") if not filled.get(h, "").strip()]
    if empty:
        problems.append(f"report.md: fill {', '.join(empty)}")
    return {"id": nid, "ok": not problems, "problems": problems, "warnings": warnings, "searched": searched,
            "coverage": coverage, "summary": summary, "fingerprint": fingerprint(cfg, nid)}


def reviews(cfg: Config, nid: str) -> list[dict]:
    out = []
    for rid in read_json(_dir(cfg, nid) / "reviews.json", []) or []:
        rec = read_json(cfg.layout.reviews / f"{rid}.json")
        if rec:
            out.append(rec)
    return out


def close(cfg: Config, nid: str, verdict: str, closest: str, delta: str, why: str = "") -> dict:
    """Record the verdict. The limits are enforced here: PROCEED is closed when a real statement is
    already established, ABANDON needs the paper that establishes it, and a review must have seen
    this exact version. CAUTION is for a thin delta; say what is thin."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    d = _dir(cfg, nid)
    if (d / "verdict.json").exists():
        raise FileExistsError(f"{nid} is closed; open a new audit for a changed statement")
    res = check(cfg, nid)
    if not res["ok"]:
        raise ValueError("audit not ready: " + "; ".join(res["problems"]))
    if not delta.strip():
        raise ValueError("--delta: the difference from the closest work, in one sentence a referee could verify")
    last = next((r for r in reversed(reviews(cfg, nid)) if r.get("task") == "novelty"), None)
    if not last or (last.get("subject") or {}).get("fingerprint") != res["fingerprint"]:
        raise ValueError(f"needs a review of this version: `autolab review run novelty --audit {nid}`")
    if last.get("verdict") == "no":
        raise ValueError(f"review {last['id']}: the audit does not hold; fix what it lists and review again")
    open_issues = [i for i in last.get("issues", []) if isinstance(i, dict) and i.get("severity") in ("critical", "major")]
    if open_issues and (verdict != "CAUTION" or any(i["severity"] == "critical" for i in open_issues)):
        raise ValueError(f"review {last['id']} has unresolved issues: fix them and review again, or for major issues "
                         "that a revised audit cannot fix close as CAUTION and name each one in --why")
    unnamed = [str(i.get("id")) for i in open_issues if str(i.get("id")) not in why]
    if unnamed:
        raise ValueError(f"--why must name each open issue of review {last['id']} and say why it stands: {', '.join(unnamed)}")
    audit = load(cfg, nid)
    secret = read_json(_secret(cfg, nid), {}) or {}
    real = secret.get("real") or [s["sid"] for s in audit["statements"]]
    ctype = secret.get("type", "")
    picked = {s: ({ctype: res["summary"][s][ctype]} if ctype and ctype in res["summary"][s] else res["summary"][s])
              for s in real}
    established = {s: [v["paper"] for v in by.values() if v["degree"] == DEGREES[0]] for s, by in picked.items()}
    settled = {s: p for s, p in established.items() if p}
    if verdict == "PROCEED" and settled:
        raise ValueError(f"PROCEED is closed: {', '.join(sorted(settled))} already established by "
                         f"{', '.join(sorted({x for p in settled.values() for x in p}))}; use CAUTION or ABANDON")
    if verdict == "ABANDON" and not any(closest in p for p in settled.values()):
        raise ValueError("ABANDON must name a paper that establishes a statement (--closest <L-id>, degree "
                         "explicit_established with a verified quote). Neighbours are not a reason to abandon")
    if verdict == "CAUTION" and not why.strip():
        raise ValueError("CAUTION needs --why: the specific thing that makes the delta thin; without one the verdict is PROCEED")
    rec = {"id": nid, "for": audit["for"], "verdict": verdict, "closest": closest, "delta": delta.strip(),
           "why": why.strip(), "at": now_iso(), "type": ctype, "review": last["id"],
           "review_independence": last.get("independence", "cross_vendor"),
           "statements": [s for s in audit["statements"] if s["sid"] in real],
           "degrees": {s: picked[s] for s in real}, "coverage": {s: res["coverage"][s] for s in real},
           "searched": res["searched"], "controls": len(secret.get("decoys", {})), "fingerprint": res["fingerprint"]}
    write_json(d / "verdict.json", rec)
    from autolab.tasks import decide
    rec["decision"] = decide(cfg, f"novelty audit {nid} for {audit['for']}: {verdict}", f"{delta.strip()} {why.strip()}".strip(),
                             f"state/novelty/{nid}/verdict.json")
    if re.fullmatch(r"H\d+[a-z]?", audit["for"]):
        from autolab import research
        research.set_field(cfg, audit["for"], "novelty_audit", nid)
    write_ledger(cfg)
    return rec


def override(cfg: Config, nid: str, why: str) -> dict:
    """Continue after ABANDON, with the reason on record and the human told."""
    if not why.strip():
        raise ValueError("give the reason")
    v = read_json(_dir(cfg, nid) / "verdict.json")
    if not v:
        raise KeyError(f"{nid} has no verdict")
    rec = {"id": nid, "why": why.strip(), "at": now_iso()}
    write_json(_dir(cfg, nid) / "override.json", rec)
    from autolab import human
    from autolab.tasks import decide
    decide(cfg, f"novelty audit {nid}: continue despite {v['verdict']}", why, f"state/novelty/{nid}/verdict.json")
    human.notify(cfg, f"PI 决定在新颖性审计 {nid} 判为 {v['verdict']} 后继续。背景：{why.strip()[:300]}")
    return rec


def status(cfg: Config, nid: str) -> dict | None:
    v = read_json(_dir(cfg, nid) / "verdict.json")
    if not v:
        return None
    age = (datetime.now().astimezone() - datetime.fromisoformat(v["at"])).days
    return {"verdict": v["verdict"], "age_days": age, "overridden": (_dir(cfg, nid) / "override.json").is_file()}


def list_audits(cfg: Config) -> list[dict]:
    out = []
    for d in sorted(cfg.layout.novelty.glob("N*")):
        a = read_json(d / "audit.json") or {}
        st = status(cfg, d.name) or {}
        out.append({"id": d.name, "for": a.get("for"), "created": a.get("created"), "verdict": st.get("verdict", "open"),
                    "overridden": st.get("overridden", False)})
    return out


def plan_core_hours(cfg: Config, eid: str) -> float:
    from autolab import slurm
    meta, _ = read_frontmatter(cfg.layout.experiments / eid / "plan.md")
    total = 0.0
    for part in list(meta.get("parts") or {}) or [None]:
        try:
            r = slurm.resources(cfg, eid, part)
            total += float(r["billed_cpus"]) * float(r["hours"]) * int(r["array_size"])
        except (ValueError, KeyError, TypeError):      # the resource check reports these itself
            continue
    return total


def plan_check(cfg: Config, eid: str, meta: dict, problems: list[str], warnings: list[str]) -> None:
    """A new line of work, or an expensive plan, must not start without an audit of what it could
    contribute. Follow-ups cite their parent's audit on the candidate card. Only a verdict of
    ABANDON stops a plan; everything else moves on with the contribution on record."""
    if not cfg["gates"].get("require_novelty", True) or (cfg.layout.experiments / eid / "jobs.jsonl").is_file():
        return
    from autolab import research
    cost = plan_core_hours(cfg, eid)
    limit = float(cfg["gates"]["novelty_audit_core_hours"])
    new_line = not str(meta.get("parent", "") or "")
    for h in meta.get("hypotheses") or []:
        nid = str((research.candidate(cfg, h) or {}).get("novelty_audit", "") or "")
        if not nid:
            if new_line or cost >= limit:
                why = "a new line of work (no parent)" if new_line else f"about {cost:.0f} core-hours"
                problems.append(f"candidate {h} has no novelty audit ({why}): run one (task route `novelty`, skill "
                                "`novelty-audit`), or point `novelty_audit` on the card at the parent's audit")
            continue
        st = status(cfg, nid)
        if st is None:
            problems.append(f"novelty audit {nid} of {h} is not closed: `autolab novelty close {nid} ...`")
        elif st["verdict"] == "ABANDON" and not st["overridden"]:
            problems.append(f"novelty audit {nid} says ABANDON for {h}: kill the candidate, or record a reason with "
                            f"`autolab novelty override {nid} --why ...`")
        elif st["age_days"] > int(cfg["gates"]["novelty_max_age_days"]):
            warnings.append(f"novelty audit {nid} is {st['age_days']} days old; check `autolab lit watch` for new work")


def claim_problems(cfg: Config, meta: dict) -> list[str]:
    """`novelty` in claim.md: an audit id, or `none-claimed` for a measurement that claims nothing new."""
    value = str(meta.get("novelty", "") or "").strip()
    if not value:
        return (["central claim: set `novelty` to an audit id or \"none-claimed\""]
                if meta.get("central") and cfg["gates"].get("require_novelty", True) else [])
    if value == NONE_CLAIMED:
        return []
    st = status(cfg, value)
    if st is None:
        return [f"novelty audit {value} is not closed"]
    if st["verdict"] == "ABANDON" and not st["overridden"]:
        return [f"novelty audit {value} says ABANDON"]
    return []


def write_ledger(cfg: Config) -> Path:
    """state/CONTRIBUTIONS.md: what the lab would add to the field, statement by statement. The
    report and the paper are built from this, and `value_grade` reviews read it."""
    lines = ["# Contributions (generated from novelty audits and findings)", "",
             "| Audit | For | Verdict | Type | Closest | Delta |", "|---|---|---|---|---|---|"]
    for a in list_audits(cfg):
        v = read_json(_dir(cfg, a["id"]) / "verdict.json") or {}
        mark = (" (overridden)" if a["overridden"] else "") + (
            " (reviewer from the lab's own vendor)" if v.get("review_independence") == "same_vendor" else "")
        lines.append(f"| {a['id']} | {a['for']} | {a['verdict']}{mark} | {v.get('type', '')} | {v.get('closest', '')} | "
                     f"{v.get('delta', '')} |")
    audited, plain, unset = [], [], []
    for d in sorted(cfg.layout.findings.glob("F*")):
        rec = read_json(d / "finding.json") or {}
        if rec.get("superseded_by") or not (d / "claim.md").is_file():
            continue
        value = str(read_frontmatter(d / "claim.md")[0].get("novelty", "") or "")
        (plain if value == NONE_CLAIMED else audited if value else unset).append(f"{d.name}{f' ({value})' if value and value != NONE_CLAIMED else ''}")
    lines += ["", "## Findings", f"- with an audited contribution: {', '.join(audited) or 'none'}",
              f"- measurements claiming nothing new: {', '.join(plain) or 'none'}",
              f"- not stated: {', '.join(unset) or 'none'}", ""]
    path = cfg.layout.state / "CONTRIBUTIONS.md"
    write_atomic(path, "\n".join(lines))
    return path
