"""Shared knowledge: literature index + reading receipts, context packs, and the plan gate.

The rule this module enforces: before any new experiment or direction, the PI must
build a context pack (failed ideas, rejected claims, accepted findings, relevant
literature, human uploads, known errors) and the plan must answer every item in it.
Must-read papers need a reading receipt (a notes file) before the plan passes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autolab.config import Config
from autolab.human import request_action, uploads
from autolab.util import (append_jsonl, next_id, now_iso, overlap, read_frontmatter, tokens,
                          read_json, read_jsonl, sections, sha256_tree, write_atomic,
                          write_json)

TIERS = ("A", "B", "C")
ROLES = ("baseline", "method", "benchmark", "theory", "contradicting", "background", "data")
ACCESS = ("oa", "institutional", "human_needed", "abstract_only", "have_pdf")
READ_LEVELS = ("abstract", "skim", "full")


# ---------------------------------------------------------------- literature

def lit_records(cfg: Config) -> list[dict]:
    latest: dict[str, dict] = {}
    for r in read_jsonl(cfg.layout.lit_index):
        latest[r["id"]] = r
    return list(latest.values())


def lit_add(cfg: Config, title: str, doi: str = "", year: int | None = None, tier: str = "B",
            role: str = "background", access: str = "abstract_only",
            keywords: list[str] | None = None) -> dict:
    if tier not in TIERS or role not in ROLES or access not in ACCESS:
        raise ValueError(f"tier∈{TIERS} role∈{ROLES} access∈{ACCESS}")
    for r in lit_records(cfg):
        if doi and r.get("doi", "").lower() == doi.lower():
            return r
    rec = {"id": next_id(cfg.layout.ids, "literature", width=4), "title": title, "doi": doi,
           "year": year, "tier": tier, "role": role, "access": access,
           "keywords": keywords or [], "added": now_iso()}
    append_jsonl(cfg.layout.lit_index, rec)
    if access == "human_needed" and tier in cfg["gates"]["download_request_tiers"]:
        request_action(cfg, "download",
                       f"Please download {rec['id']} \"{title}\" (doi:{doi or 'n/a'}) and put the PDF "
                       f"in 00_human/pdf_drop/ named `{rec['id']}.pdf`.", dedupe=f"download {rec['id']} ")
    return rec


def lit_update(cfg: Config, lid: str, **fields) -> dict:
    """Correct or extend a record (title, doi, year, tier, role, access, pdf, url, keywords).
    A PDF is moved to the canonical literature/pdf/<L-id>.pdf and its text extracted."""
    rec = next((r for r in lit_records(cfg) if r["id"] == lid), None)
    if rec is None:
        raise KeyError(lid)
    if fields.get("access") and fields["access"] not in ACCESS:
        raise ValueError(f"access ∈ {ACCESS}")
    if fields.get("tier") and fields["tier"] not in TIERS:
        raise ValueError(f"tier ∈ {TIERS}")
    if fields.get("role") and fields["role"] not in ROLES:
        raise ValueError(f"role ∈ {ROLES}")
    pdf = fields.get("pdf")
    if pdf:
        src = Path(pdf) if Path(pdf).is_absolute() else cfg.layout.root / pdf
        canon = cfg.layout.literature / "pdf" / f"{lid}.pdf"
        if not src.is_file():
            raise FileNotFoundError(f"PDF not found: {pdf}")
        if src.resolve() != canon.resolve():
            canon.parent.mkdir(parents=True, exist_ok=True)
            src.replace(canon)
        fields["pdf"] = f"literature/pdf/{lid}.pdf"
        fields.setdefault("access", "have_pdf")
        if fields["access"] is None:
            fields["access"] = "have_pdf"
    rec = {**rec, **{k: v for k, v in fields.items() if v is not None}, "updated": now_iso()}
    append_jsonl(cfg.layout.lit_index, rec)
    if rec.get("access") in ("have_pdf", "oa", "institutional"):
        from autolab.human import resolve_actions
        resolve_actions(cfg, f"download {lid} ", f"{lid} obtained ({rec['access']})")
    if pdf:
        try:
            pdf_text(cfg, lid)
        except Exception:           # best effort: `autolab lit text` reports the reason
            pass
    return rec


def _pypdf(cfg: Config):
    import sys
    lib = cfg.fw / "third_party" / "pylib"
    if lib.is_dir() and str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    try:
        import pypdf
    except ImportError as e:
        raise RuntimeError("pypdf not available: run scripts/fetch_vendor.sh (installs it into "
                           "third_party/pylib)") from e
    return pypdf


def pdf_text(cfg: Config, lid: str) -> dict:
    """Extract literature/pdf/<L-id>.pdf to literature/text/<L-id>.txt with page markers.
    Agents read the text file (the Read tool cannot render PDFs without poppler)."""
    pdf = cfg.layout.literature / "pdf" / f"{lid}.pdf"
    if not pdf.is_file():
        raise FileNotFoundError(f"{pdf} missing")
    pypdf = _pypdf(cfg)
    reader = pypdf.PdfReader(str(pdf))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception as e:          # damaged page: keep going, say so in the text
            txt = f"[page could not be extracted: {e}]"
        parts.append(f"\n\n===== page {i} =====\n\n{txt}")
    out = cfg.layout.literature / "text" / f"{lid}.txt"
    write_atomic(out, f"# {lid} — text extracted from {pdf.name} by pypdf {pypdf.__version__}. "
                      "Equations and tables may be garbled; check the PDF for exact values.\n" + "".join(parts))
    chars = sum(len(p) for p in parts)
    return {"id": lid, "text": str(out.relative_to(cfg.layout.root)), "pages": len(reader.pages),
            "chars": chars, "low_text": chars < 500 * len(reader.pages) * 0.2}


def receipt(cfg: Config, lid: str) -> dict | None:
    """Reading receipt = literature/notes/<L-id>.md with TOML frontmatter (read_level, read_by)."""
    p = cfg.layout.lit_notes / f"{lid}.md"
    if not p.is_file():
        return None
    meta, body = read_frontmatter(p)
    sec = sections(body)
    # accept "## Key points", "## Key Points (full text)", "## Key points — p. 1–7" (turn 15: a
    # heading variant made the gate report an empty section)
    meta["has_key_points"] = any(name.startswith("key points") and text.strip() for name, text in sec.items())
    return meta


def note_template(cfg: Config, lid: str) -> Path:
    rec = next(r for r in lit_records(cfg) if r["id"] == lid)
    p = cfg.layout.lit_notes / f"{lid}.md"
    if not p.exists():
        write_atomic(p, (
            "+++\n"
            f'id = "{lid}"\ndoi = "{rec.get("doi", "")}"\n'
            'read_level = "full"        # abstract | skim | full\n'
            'version_read = ""          # journal | preprint | tech-report | thesis | other (if not the indexed version, say so)\n'
            'source_url = ""\n'
            'read_by = ""               # agent or person\n'
            f'date = "{now_iso()[:10]}"\n'
            "+++\n\n"
            f"# {rec['title']}\n\n"
            "## Key points\n\n## Methods and setup\n\n## Results we may use\n\n"
            "## Limitations / what they did not test\n\n## Relevance to our GOAL\n"))
    return p


def intake_pdf_drop(cfg: Config) -> list[str]:
    """Move human-dropped PDFs named <L-id>.pdf into literature/pdf and mark them available."""
    moved = []
    pdf_dir = cfg.layout.literature / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    ids = {r["id"] for r in lit_records(cfg)}
    for p in sorted(cfg.layout.pdf_drop.glob("*.pdf")) if cfg.layout.pdf_drop.is_dir() else []:
        lid = p.stem.upper()
        if lid in ids:
            lit_update(cfg, lid, access="have_pdf", pdf=str(p))
            moved.append(lid)
    return moved


# ---------------------------------------------------------------- knowledge sources

def _wiki_failed_ideas(cfg: Config) -> list[dict]:
    out = []
    d = cfg.layout.wiki / "ideas"
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        meta, body = read_frontmatter(p)
        if str(meta.get("outcome", "")).lower() in ("negative", "mixed"):
            out.append({"id": f"wiki:{p.stem}", "kind": "failed_idea",
                        "title": meta.get("title", p.stem), "path": p, "text": body})
    return out


def _claim_dirs(base: Path, kind: str) -> list[dict]:
    out = []
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        f = d / "claim.md"
        if d.is_dir() and f.is_file():
            meta, body = read_frontmatter(f)
            out.append({"id": d.name, "kind": kind, "title": meta.get("statement", d.name),
                        "path": f, "text": body + " " + meta.get("statement", "")})
    return out


def _sources(cfg: Config) -> list[dict]:
    L = cfg.layout
    items = _wiki_failed_ideas(cfg)
    items += _claim_dirs(L.rejected, "rejected_claim")
    items += [i for i in _claim_dirs(L.findings, "finding") if i["id"].startswith("F")]
    for r in lit_records(cfg):
        items.append({"id": r["id"], "kind": "literature", "title": r["title"], "rec": r,
                      "text": " ".join([r["title"], " ".join(r.get("keywords", []))])})
    for u in uploads(cfg):
        items.append({"id": u["id"], "kind": "upload", "title": u.get("title", ""), "rec": u,
                      "text": " ".join(str(u.get(k, "")) for k in ("title", "description",
                                                                   "keywords", "conditions"))})
    for e in read_jsonl(L.experience / "error-cookbook.jsonl"):
        items.append({"id": e.get("id", "ERR"), "kind": "known_error", "title": e.get("symptom", ""),
                      "text": " ".join(str(v) for v in e.values())})
    from autolab.tasks import lesson_label, lessons
    for e in lessons(cfg):                       # scoped rules from experiments and retrospectives
        items.append({"id": e["id"], "kind": "lesson", "title": lesson_label(e),
                      "text": " ".join([e["lesson"], " ".join(e.get("keywords", [])),
                                        " ".join(str(v) for v in e.get("when", {}).values())])})
    return items


def _sources_hash(cfg: Config) -> str:
    L = cfg.layout
    parts = []
    for p in (L.wiki / "ideas", L.rejected, L.findings, L.lit_index, L.uploads_registry,
              L.experience / "error-cookbook.jsonl", L.experience / "lessons.jsonl"):
        parts.append(sha256_tree(p) if p.exists() else "-")
    return "|".join(parts)


# ---------------------------------------------------------------- context pack

MUST_ALWAYS = ("failed_idea", "rejected_claim")


def expand_synonyms(cfg: Config, query: set[str]) -> set[str]:
    """Add every term of a synonym group the query touches. Groups come from the domain pack
    (`[synonyms] groups = [[...], ...]` in domain.toml) and the project's
    `literature/synonyms.toml` (same shape). Keyword overlap alone missed renamed concepts."""
    import tomllib
    groups = list(cfg.domain.get("synonyms", {}).get("groups", []))
    f = cfg.layout.literature / "synonyms.toml"
    if f.is_file():
        try:
            groups += tomllib.loads(f.read_text(encoding="utf-8")).get("groups", [])
        except tomllib.TOMLDecodeError:
            pass
    out = set(query)
    qt = set().union(*(tokens(q) or {q.lower()} for q in query)) if query else set()
    for g in groups:
        gt = set().union(*(tokens(t) or {t.lower()} for t in g)) if g else set()
        if gt & qt:
            out.update(g)
    return out


def _select(cfg: Config, query: set[str]) -> list[dict]:
    """Knowledge items relevant to `query`, highest overlap first, with must-read flags."""
    limit = cfg["gates"]["context_pack_max_items"]
    must_tier = cfg["gates"]["require_reading_receipts_tier"]
    query = expand_synonyms(cfg, query)
    scored = []
    for it in _sources(cfg):
        s = overlap(query, it["text"] + " " + it["title"])
        always = it["kind"] == "upload" and it.get("rec", {}).get("use_as") == "constraint"
        if s > 0 or always:
            scored.append((s + (100 if always else 0), it))
    scored.sort(key=lambda x: -x[0])

    chosen, per_kind = [], {}
    for s, it in scored:
        k = it["kind"]
        if k not in MUST_ALWAYS and per_kind.get(k, 0) >= limit:
            continue
        per_kind[k] = per_kind.get(k, 0) + 1
        must = k in MUST_ALWAYS or (k == "upload" and it["rec"].get("use_as") == "constraint")
        if k == "literature" and TIERS.index(it["rec"].get("tier", "C")) <= TIERS.index(must_tier):
            must = True
        chosen.append({"id": it["id"], "kind": k, "title": it["title"], "score": s,
                       "must_read": must,
                       "path": str(it["path"].relative_to(cfg.layout.root)) if it.get("path") else None})
    return chosen


def build_context_pack(cfg: Config, topic: str, keywords: list[str],
                       include: list[str] | None = None) -> dict:
    """`include`: knowledge item IDs that must be in the pack even if keyword matching misses
    them (seen in a real run: keyword matching left three key papers out of a pack)."""
    # multi-word keywords ("path compression") also contribute their single words, since
    # matching works on word tokens
    query = set(k.lower() for k in keywords) | {w.lower() for w in re.findall(r"\w{3,}", topic)}
    query |= {w.lower() for k in keywords for w in re.findall(r"\w{3,}", k)}
    chosen = _select(cfg, query)
    have = {c["id"] for c in chosen}
    must_tier = cfg["gates"]["require_reading_receipts_tier"]
    for it in _sources(cfg):
        if include and it["id"] in include and it["id"] not in have:
            must = it["kind"] in MUST_ALWAYS or (it["kind"] == "literature" and
                   TIERS.index(it.get("rec", {}).get("tier", "C")) <= TIERS.index(must_tier))
            chosen.append({"id": it["id"], "kind": it["kind"], "title": it["title"], "score": 0,
                           "must_read": must, "included": True,
                           "path": str(it["path"].relative_to(cfg.layout.root)) if it.get("path") else None})
            have.add(it["id"])
    missing = sorted(set(include or []) - have)
    if missing:
        raise KeyError(f"--include IDs not found in the knowledge base: {missing}")
    cp = next_id(cfg.layout.ids, "context_pack")
    pack = {"id": cp, "topic": topic, "keywords": sorted(query), "created": now_iso(),
            "sources_hash": _sources_hash(cfg), "items": chosen, "include": sorted(include or [])}
    write_json(cfg.layout.context_packs / f"{cp}.json", pack)

    lines = [f"# Context pack {cp}: {topic}", "",
             "Answer every item in your plan under `## Prior knowledge consulted`:",
             "one line per ID saying how it changes the plan (or why it does not apply).",
             "Items marked MUST are mandatory reading; literature needs a reading receipt.", ""]
    for kind in ("failed_idea", "rejected_claim", "upload", "finding", "lesson", "literature", "known_error"):
        group = [c for c in chosen if c["kind"] == kind]
        if group:
            lines.append(f"## {kind.replace('_', ' ')}")
            for c in group:
                flag = "MUST " if c["must_read"] else ""
                where = f" — `{c['path']}`" if c["path"] else ""
                lines.append(f"- {flag}**{c['id']}** {c['title']}{where}")
            lines.append("")
    if not chosen:
        lines.append("_No prior knowledge matched. Say so explicitly in the plan._")
    write_atomic(cfg.layout.context_packs / f"{cp}.md", "\n".join(lines) + "\n")
    return pack


# ---------------------------------------------------------------- plan gate

PLAN_REQUIRED = ("id", "context_pack", "hypothesis", "prediction", "decision_rule", "budget_hours")


def check_plan(cfg: Config, eid: str) -> dict:
    """Plan gate: required fields, every context-pack item answered, receipts for must-read papers."""
    exp = cfg.layout.experiments / eid
    plan = exp / "plan.md"
    problems, warnings = [], []
    if not plan.is_file():
        return {"ok": False, "problems": [f"{plan} missing"], "warnings": []}
    meta, body = read_frontmatter(plan)
    for k in PLAN_REQUIRED:
        if not meta.get(k):
            problems.append(f"plan field '{k}' is empty")
    if any("<<FILL" in str(v) for v in meta.values()) or "<<FILL" in body:
        problems.append("plan still contains <<FILL placeholders")

    pack = read_json(cfg.layout.context_packs / f"{meta.get('context_pack', '')}.json")
    consulted = sections(body).get("prior knowledge consulted", "")
    if pack is None:
        problems.append(f"context pack '{meta.get('context_pack')}' not found; run `autolab preflight`")
    else:
        # Re-run the pack's own search: only NEW relevant items make it stale, and the plan can
        # answer just those. Edits to unrelated records (a tier fix, a moved PDF) do not count.
        current = {c["id"]: c for c in _select(cfg, set(pack.get("keywords", [])))}
        items = []
        for it in pack["items"]:
            now = current.get(it["id"])
            items.append({**it, "must_read": it["must_read"] or bool(now and now["must_read"])})
        known = {it["id"] for it in pack["items"]}
        new = [c for i, c in current.items() if i not in known]
        items += [{**c, "new": True} for c in new]
        lines = consulted.splitlines()
        # Once a job ran the plan is locked; an item that appeared afterwards (a new lesson, a new
        # paper) is answered in plan_amendments.md instead. Before 0.6.4 such a plan could never pass
        # its gate again (first-test E032, lesson LS051 added by E031 after E032 was locked).
        locked = (exp / "jobs.jsonl").is_file()
        amend = (exp / "plan_amendments.md").read_text(encoding="utf-8").splitlines() \
            if locked and (exp / "plan_amendments.md").is_file() else []
        for item in items:
            hit = [ln for ln in lines if item["id"] in ln]
            if not hit and item.get("new") and amend:
                hit = [ln for ln in amend if item["id"] in ln and not ln.lstrip().startswith("#")]
            if not hit:
                if item.get("new"):
                    problems.append(f"context pack {pack['id']} is stale: new relevant item {item['id']} "
                                    f"({item['kind']}{', MUST' if item['must_read'] else ''}) \"{item['title'][:60]}\" "
                                    "is not answered; add a line under Prior knowledge consulted or rebuild the pack"
                                    + (" (the plan is locked: answer it in plan_amendments.md, one line naming the ID)" if locked else ""))
                else:
                    problems.append(f"plan does not answer context item {item['id']} ({item['kind']})")
            elif item["must_read"] and len(hit[0].split(item["id"], 1)[1].strip(" :-—*")) < 20:
                problems.append(f"answer to MUST item {item['id']} is too short to show it was read")
            if item["kind"] == "literature" and item["must_read"]:
                rc = receipt(cfg, item["id"])
                rec = next((r for r in lit_records(cfg) if r["id"] == item["id"]), {})
                if rc is None:
                    problems.append(f"no reading receipt literature/notes/{item['id']}.md")
                elif not rc.get("has_key_points"):
                    problems.append(f"receipt {item['id']} has empty '## Key points'")
                elif rc.get("read_level") != "full":
                    if rec.get("access") in ("human_needed", "abstract_only"):
                        warnings.append(f"{item['id']} read at '{rc.get('read_level')}' only; full text not available")
                    else:
                        problems.append(f"{item['id']} has full text but receipt says '{rc.get('read_level')}'")

    _check_precision_and_map(cfg, plan, meta, body, problems, warnings)
    _check_candidates(cfg, eid, meta, body, problems, warnings)
    result ={"eid": eid, "ok": not problems, "problems": problems, "warnings": warnings,
              "checked": now_iso(), "plan_hash": sha256_tree(plan)}
    write_json(exp / "plan_gate.json", result)
    return result


def _check_precision_and_map(cfg: Config, plan: Path, meta: dict, body: str,
                             problems: list[str], warnings: list[str]) -> None:
    """Precision section (planned n, expected CI or detectable effect), hypothesis IDs that exist in
    state/HYPOTHESES.md, and the planning skills really invoked. In the first long run an
    under-powered design (3 seeds, CI from -1.3 to 15.9) cost two extra rounds of experiments."""
    import re as _re
    from autolab import skills
    prec = _re.sub(r"<!--.*?-->", "", sections(body).get("precision", ""), flags=_re.S).strip()
    if len(prec) < 60 or not _re.search(r"\d", prec):
        problems.append("section '## Precision' is missing or too thin: give the unit of replication, planned n, "
                        "the expected CI half-width or detectable effect, and where the variance estimate comes from "
                        "(use the `statistical-power` skill)")
    hyp = meta.get("hypotheses") or []
    hfile = cfg.layout.state / "HYPOTHESES.md"
    if not hyp:
        problems.append("plan field 'hypotheses' is empty: name the H-ids from state/HYPOTHESES.md this tests")
    elif not hfile.is_file():
        problems.append("state/HYPOTHESES.md is missing: create the hypothesis map first")
    else:
        known = set(_re.findall(r"\bH\d+[a-z]?\b", hfile.read_text(encoding="utf-8")))
        unknown = [h for h in hyp if h not in known]
        if unknown:
            problems.append(f"hypotheses {unknown} are not in state/HYPOTHESES.md")
    need = skills.required(cfg, "pi", "planning")
    if need and skills.have_logs(cfg):
        # the PI itself, or a student whose dispatch names this experiment
        seen = skills.invoked_by(cfg, plan.stat().st_mtime - 6 * 3600, {plan.parent.name}, include_pi=True)
        missing = [s for s in need if s not in seen]
        if missing:
            problems.append(f"planning skills {missing} were not invoked (Skill tool) while this plan was written")
    elif need:
        warnings.append(f"no turn logs: could not verify that {need} were invoked")


def _check_candidates(cfg: Config, eid: str, meta: dict, body: str,
                      problems: list[str], warnings: list[str]) -> None:
    """Each tested hypothesis needs a candidate card, and the plan must say what it discriminates
    and which decision each outcome leads to; finished experiments need an outcome first, so a
    failed run is classified (environment vs. direction) before the next plan builds on it."""
    from autolab import research
    if cfg["gates"].get("require_candidates", True):
        for h in meta.get("hypotheses") or []:
            problems += research.card_problems(cfg, h)
        problems += research.discrimination_problems(body)
    parent = str(meta.get("parent", "") or "")
    if parent and not (cfg.layout.experiments / parent / "plan.md").is_file():
        problems.append(f"parent experiment {parent} does not exist")
    if cfg["gates"].get("require_outcomes", True):
        open_ = [e for e in research.missing_outcomes(cfg) if e != eid]
        if open_:
            problems.append(f"finished experiments without an outcome: {open_}; record each with `autolab outcome "
                            "<E> --engineering valid|invalid --hypothesis ... --value ... --why ...` first")


def plan_gate_passed(cfg: Config, eid: str) -> bool:
    exp = cfg.layout.experiments / eid
    from autolab import guard
    guard.require_trusted_records(cfg, [exp / "plan_gate.json"])
    res = read_json(exp / "plan_gate.json")
    return bool(res and res.get("ok") and (exp / "plan.md").is_file()
                and res.get("plan_hash") == sha256_tree(exp / "plan.md"))


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
