"""Known defects of a shared baseline, tool or analysis, and the findings they touch.

first-test found in T175 that its speedups were measured against a weaker cc3d call than the library
offers. New experiments changed; F002-F010 kept the old baseline and nothing asked about them again.
A defect lists the findings it touches, and each stays open until it is re-measured, re-scoped or
shown not to be affected. Until then the defect is a due item that stops new plans, except a plan
that fixes it (`fixes = ["DF001"]`), and findings/INDEX.md and state/CONTRIBUTIONS.md carry a note.
With `--grep`, a later finding whose code matches is touched too, so the same mistake is caught again.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from autolab.config import Config
from autolab.util import FileLock, next_id, now_iso, read_json, write_json

HOW = ("remeasured", "rescoped", "not_affected")
SCAN_LIMIT_BYTES = 2_000_000


def _file(cfg: Config, did: str) -> Path:
    return cfg.layout.defects / f"{did}.json"


def load(cfg: Config, did: str) -> dict:
    rec = read_json(_file(cfg, did))
    if not rec:
        raise KeyError(f"no defect {did}")
    return rec


def all_defects(cfg: Config) -> list[dict]:
    return [read_json(p) for p in sorted(cfg.layout.defects.glob("DF*.json"))]


def _live(cfg: Config) -> dict[str, dict]:
    """Current findings; a superseded one no longer needs an answer."""
    out = {}
    for d in sorted(cfg.layout.findings.glob("F*")):
        rec = read_json(d / "finding.json")
        if rec and not rec.get("superseded_by"):
            out[d.name] = rec
    return out


def _code(cfg: Config, eid: str) -> Iterator[Path]:
    from autolab.slurm import _CODE_EXT, _OUTPUT_DIRS
    exp = cfg.layout.experiments / eid
    for f in sorted(exp.rglob("*")) if exp.is_dir() else []:
        if f.suffix.lower() in _CODE_EXT and f.is_file() and f.stat().st_size <= SCAN_LIMIT_BYTES \
                and not set(f.relative_to(exp).parts[:-1]) & _OUTPUT_DIRS:
            yield f


def _matches(cfg: Config, finding: dict, rx: re.Pattern) -> bool:
    claim = cfg.layout.findings / finding["id"] / "claim.md"
    files = [f for e in finding.get("experiments", []) for f in _code(cfg, e)] + [claim]
    return any(rx.search(f.read_text(encoding="utf-8", errors="ignore")) for f in files if f.is_file())


def touched(cfg: Config, rec: dict) -> list[str]:
    rx = re.compile(rec["grep"]) if rec.get("grep") else None
    return sorted(f for f, v in _live(cfg).items() if f in rec["affects"] or (rx and _matches(cfg, v, rx)))


def open_findings(cfg: Config, rec: dict) -> list[str]:
    return [f for f in touched(cfg, rec) if f not in rec["resolved"]]


def new(cfg: Config, what: str, found_by: str, affects: list[str], grep: str = "", correction: str = "") -> dict:
    """`affects`: F-ids, or E-ids standing for every finding built on that experiment."""
    if not what.strip() or not found_by.strip():
        raise ValueError("say what is wrong and where it was found (--found-by <T-id, R-id or LS-id>)")
    try:
        re.compile(grep)
    except re.error as e:
        raise ValueError(f"--grep: {e}") from e
    live, findings = _live(cfg), set()
    for a in affects:
        hit = [f for f, v in live.items() if a == f or a in v.get("experiments", [])]
        if not hit:
            raise KeyError(f"{a} is neither a current finding nor an experiment behind one")
        findings.update(hit)
    rec = {"what": what.strip(), "found_by": found_by.strip(), "correction": correction.strip(), "grep": grep,
           "affects": sorted(findings), "resolved": {}, "at": now_iso()}
    hit = touched(cfg, rec)
    if not hit:
        raise ValueError("this touches no current finding: name them with --affects, or give a --grep that matches their code")
    rec = {"id": next_id(cfg.layout.ids, "defect", width=3), **rec}
    write_json(_file(cfg, rec["id"]), rec)
    from autolab.tasks import decide
    decide(cfg, f"known defect {rec['id']}: {rec['what']}", f"found by {rec['found_by']}; touches {', '.join(hit)}")
    _refresh(cfg)
    return {**rec, "touches": hit}


def _check_evidence(cfg: Config, rec: dict, evidence: str) -> None:
    """A re-measurement has to be newer than the defect, or it is the old number again."""
    if evidence.startswith("F"):
        at = (read_json(cfg.layout.findings / evidence / "finding.json") or {}).get("accepted", "")
    else:
        o = read_json(cfg.layout.experiments / evidence / "outcome.json") or {} if evidence else {}
        at = o.get("at", "") if o.get("engineering") == "valid" else ""
    if at < rec["at"]:
        raise ValueError(f"--evidence: a valid experiment outcome (E-id) or accepted finding (F-id) newer than {rec['id']}")


def resolve(cfg: Config, did: str, fid: str, how: str, why: str, evidence: str = "") -> dict:
    if how not in HOW:
        raise ValueError(f"--how must be one of {HOW}")
    if not why.strip():
        raise ValueError("--why: for rescoped, the sentence that holds now; otherwise the reason")
    with FileLock(_file(cfg, did)):
        rec = load(cfg, did)
        if fid not in touched(cfg, rec):
            raise KeyError(f"{did} does not touch {fid}")
        if how == "remeasured":
            _check_evidence(cfg, rec, evidence)
        rec["resolved"][fid] = {"how": how, "why": why.strip(), "evidence": evidence, "at": now_iso()}
        write_json(_file(cfg, did), rec)
    from autolab.tasks import decide
    decide(cfg, f"defect {did}, {fid}: {how}", why.strip(), evidence)
    _refresh(cfg)
    return {"id": did, "finding": fid, "how": how, "open": open_findings(cfg, rec)}


def status(cfg: Config) -> list[dict]:
    return [{**r, "touches": touched(cfg, r), "open": open_findings(cfg, r)} for r in all_defects(cfg)]


def pending(cfg: Config) -> dict[str, list[str]]:
    """finding -> the defects it still has to answer."""
    out: dict[str, list[str]] = {}
    for r in status(cfg):
        for f in r["open"]:
            out.setdefault(f, []).append(r["id"])
    return out


def notes(cfg: Config) -> list[str]:
    """What a reader of the findings must know."""
    lines = []
    for r in status(cfg):
        size = f" Known size: {r['correction']}" if r["correction"] else ""
        lines.append(f"- **{r['id']}** {r['what']} (found by {r['found_by']}).{size}")
        if r["open"]:
            lines.append(f"  - not yet answered: {', '.join(r['open'])}")
        lines += [f"  - {f} {v['how']}{' in ' + v['evidence'] if v['evidence'] else ''}: {v['why']}"
                  for f, v in sorted(r["resolved"].items())]
    return lines


def _refresh(cfg: Config) -> None:
    from autolab import gates, novelty
    gates._rebuild_index(cfg)
    novelty.write_ledger(cfg)
