"""Research steering: candidate registry, experiment outcomes, the search tree.

Candidates (`state/candidates/<H-id>.md`) make the choice of the next experiment explicit: each
candidate hypothesis records its closest prior work, the mechanism difference, evidence for and
against, the key uncertainty, the smallest experiment that would discriminate it from its rivals,
the cost, and when to continue, pause or kill it. A plan names the H-ids it tests; the plan gate
requires their cards and a `## Discrimination` section (which explanations the experiment separates
and which decision each outcome leads to). Rankings between candidates are advice, not evidence.

Outcomes (`autolab outcome`) close an experiment in three layers, so that an environment failure is
never booked as a failed research direction:
  engineering  valid | invalid           (invalid: rerun or fix; no research conclusion)
  hypothesis   supports | refutes | inconclusive | n/a
  value        continue | refine | pivot | stop   (pivot/stop go to the human, as RULES require)
The analysis script named in `analysis_command` is fingerprinted at the first submission; changing
it afterwards must be explained in plan_amendments.md before an outcome is accepted.
`autolab tree` draws experiments as a graph (plan `parent`), with outcome, cost and reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autolab.config import Config
from autolab.util import (FileLock, append_jsonl, now_iso, read_frontmatter, read_json, read_jsonl,
                          sha256_file, write_atomic, write_json)

CARD_FIELDS = ("statement", "closest_work", "mechanism_difference", "evidence_for", "evidence_against",
               "key_uncertainty", "discriminating_experiment", "cost_estimate", "continue_if", "pause_if", "kill_if")
CARD_REQUIRED = ("statement", "closest_work", "mechanism_difference", "key_uncertainty",
                 "discriminating_experiment", "continue_if", "kill_if")
STATUS = ("open", "selected", "paused", "killed", "supported")


def _cdir(cfg: Config) -> Path:
    return cfg.layout.state / "candidates"


def new_candidate(cfg: Config, hid: str, statement: str, **fields) -> Path:
    if not re.fullmatch(r"H\d+[a-z]?", hid):
        raise ValueError("candidate id must look like H14 (the hypothesis id)")
    p = _cdir(cfg) / f"{hid}.md"
    if p.exists():
        raise FileExistsError(f"{p} exists; edit it instead")
    vals = {"statement": statement, **{k: fields.get(k, "") for k in CARD_FIELDS if k != "statement"}}
    fm = ["+++", f'id = "{hid}"', 'status = "open"', f'created = "{now_iso()}"',
          'priority = ""            # advisory only (e.g. from a comparison); never evidence of novelty']
    fm += [f"{k} = {json.dumps(str(v), ensure_ascii=False)}" for k, v in vals.items()]
    fm += ["+++", "", f"# {hid}", "", "## Notes", ""]
    write_atomic(p, "\n".join(fm) + "\n")
    hyp = cfg.layout.state / "HYPOTHESES.md"                 # keep the map in step
    text = hyp.read_text(encoding="utf-8") if hyp.is_file() else "# Hypotheses\n\n"
    if not re.search(rf"\b{re.escape(hid)}\b", text):
        write_atomic(hyp, text.rstrip("\n") + f"\n| {hid} | — | {statement} | open | — | — | see state/candidates/{hid}.md |\n")
    return p


def candidate(cfg: Config, hid: str) -> dict | None:
    p = _cdir(cfg) / f"{hid}.md"
    return read_frontmatter(p)[0] if p.is_file() else None


def card_problems(cfg: Config, hid: str) -> list[str]:
    c = candidate(cfg, hid)
    if c is None:
        return [f"no candidate card state/candidates/{hid}.md (`autolab candidate new {hid} \"...\"`)"]
    empty = [k for k in CARD_REQUIRED if not str(c.get(k, "")).strip() or "<<FILL" in str(c.get(k, ""))]
    out = [f"candidate {hid}: fill {', '.join(empty)}"] if empty else []
    if c.get("status") == "killed":
        out.append(f"candidate {hid} is killed; reopen it with a recorded decision before testing it again")
    return out


def list_candidates(cfg: Config) -> list[dict]:
    out = []
    for p in sorted(_cdir(cfg).glob("H*.md")):
        c = read_frontmatter(p)[0]
        out.append({k: c.get(k, "") for k in ("id", "status", "priority", "statement", "cost_estimate",
                                               "discriminating_experiment")})
    return out


def set_candidate(cfg: Config, hid: str, status: str, reason: str) -> None:
    if status not in STATUS:
        raise ValueError(f"status must be one of {STATUS}")
    p = _cdir(cfg) / f"{hid}.md"
    if not p.is_file():
        raise FileNotFoundError(p)
    text = p.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^status = "[^"]*"', f'status = "{status}"', text, count=1)
    write_atomic(p, text.rstrip("\n") + f"\n- {now_iso()}: status -> {status}: {reason}\n")
    from autolab.tasks import decide
    decide(cfg, f"candidate {hid} -> {status}", reason)


def discrimination_problems(body: str) -> list[str]:
    from autolab.util import sections
    sec = re.sub(r"<!--.*?-->", "", sections(body).get("discrimination", ""), flags=re.S)
    if len(sec.strip()) < 40:
        return ["section '## Discrimination' is missing or thin: name the competing explanations this experiment "
                "separates and the decision each outcome leads to"]
    probs = []
    if not re.search(r"(?i)\bif\b.*\b(support|succe|positive|confirm|pass)", sec):
        probs.append("'## Discrimination' must say what you decide if the result supports the hypothesis")
    if not re.search(r"(?i)\bif\b.*\b(refut\w*|fail\w*|negative|reject\w*|not|no)\b", sec):
        probs.append("'## Discrimination' must say what you decide if the result refutes it")
    return probs


# ---------------------------------------------------------------- frozen evaluator

_SCRIPT = re.compile(r"(?<![\w$*/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|sh|R|jl))(?![\w/])")


def freeze_evaluator(cfg: Config, eid: str) -> dict | None:
    """Fingerprint the scripts of the plan's analysis_command (first submission only)."""
    exp = cfg.layout.experiments / eid
    f = exp / "evaluator.json"
    if f.is_file():
        return read_json(f)
    meta, _ = read_frontmatter(exp / "plan.md")
    cmd = str(meta.get("analysis_command", ""))
    files = {}
    for s in _SCRIPT.findall(cmd):
        p = exp / s if (exp / s).is_file() else cfg.layout.root / s
        if p.is_file():
            files[s] = sha256_file(p)
    if not files:
        return None
    rec = {"frozen": now_iso(), "analysis_command": cmd, "files": files}
    write_json(f, rec)
    return rec


def evaluator_problems(cfg: Config, eid: str) -> tuple[list[str], list[str]]:
    exp = cfg.layout.experiments / eid
    rec = read_json(exp / "evaluator.json")
    if not rec:
        return [], ["no frozen evaluator (plan has no analysis_command): the analysis was not fixed before the data"]
    amend = (exp / "plan_amendments.md").read_text(encoding="utf-8") if (exp / "plan_amendments.md").is_file() else ""
    probs = []
    for s, h in rec["files"].items():
        p = exp / s if (exp / s).is_file() else cfg.layout.root / s
        if not p.is_file() or sha256_file(p) != h:
            if s not in amend:
                probs.append(f"evaluator {s} changed after submission and plan_amendments.md does not explain it")
    return probs, []


# ---------------------------------------------------------------- outcomes

ENGINEERING = ("valid", "invalid")
HYPOTHESIS = ("supports", "refutes", "inconclusive", "n/a")
VALUE = ("continue", "refine", "pivot", "stop")


def record_outcome(cfg: Config, eid: str, engineering: str, hypothesis: str, value: str, reason: str,
                   evidence: list[str] | None = None, usd: float | None = None) -> dict:
    exp = cfg.layout.experiments / eid
    if not (exp / "plan.md").is_file():
        raise FileNotFoundError(f"{eid}: no plan.md")
    if engineering not in ENGINEERING or hypothesis not in HYPOTHESIS or value not in VALUE:
        raise ValueError(f"engineering {ENGINEERING}, hypothesis {HYPOTHESIS}, value {VALUE}")
    if engineering == "invalid" and hypothesis != "n/a":
        raise ValueError("an invalid run (environment, bug, timeout) supports no research conclusion: hypothesis = n/a")
    if engineering == "invalid" and value in ("pivot", "stop"):
        raise ValueError("an invalid run is not a reason to pivot or stop a direction: rerun or fix it (continue/refine)")
    if not reason.strip():
        raise ValueError("give the reason")
    probs, warns = evaluator_problems(cfg, eid)
    if probs:
        raise ValueError("; ".join(probs))
    meta, _ = read_frontmatter(exp / "plan.md")
    rec = {"at": now_iso(), "experiment": eid, "engineering": engineering, "hypothesis": hypothesis,
           "value": value, "reason": reason.strip(), "evidence": evidence or [],
           "hypotheses": meta.get("hypotheses", []), "parent": meta.get("parent", ""),
           "core_hours": core_hours(exp),
           "usd": usd, "warnings": warns,
           "jobs_seen": sum(1 for r in read_jsonl(exp / "jobs.jsonl") if "submitted" in r)}
    with FileLock(exp / "outcome"):
        append_jsonl(exp / "outcomes.jsonl", rec)
        write_json(exp / "outcome.json", rec)
    from autolab.tasks import decide
    did = decide(cfg, f"{eid} outcome: engineering {engineering}, hypothesis {hypothesis}, value {value}",
                 reason, ", ".join(evidence or []))
    rec["decision"] = did
    if value in ("pivot", "stop"):
        from autolab import human
        deleg = human.delegation(cfg)
        text = (f"{eid}: the PI proposes to {value.upper()} this line (hypothesis {hypothesis}). Reason: {reason.strip()} "
                f"Evidence: {', '.join(evidence or []) or 'see outcome.json'}. Decision {did}.")
        if deleg:
            human.request_action(cfg, "decision", f"[PI decided under delegation until {deleg['until']}; for human "
                                 f"review] {text}", blocking=False)
        else:
            human.request_action(cfg, "decision", text + " RULES: a stop or major pivot needs your approval.",
                                 blocking=True, dedupe=f"{eid}: the PI proposes to {value.upper()}")
            rec["needs_human"] = True
    return rec


def core_hours(exp: Path) -> float:
    """Core-hours of an experiment's finished jobs (allocated CPUs x elapsed)."""
    tot = 0.0
    for u in read_jsonl(exp / "jobs_usage.jsonl"):
        ch = u.get("core_hours")
        if ch is None:                           # records written before 0.5
            res = u.get("requested", {})
            ch = float(u.get("elapsed_h", 0)) * float(res.get("total_cpus", res.get("cpus", 1)) or 1)
        tot += float(ch)
    return round(tot, 2)


def missing_outcomes(cfg: Config) -> list[str]:
    """Experiments whose jobs have all finished (usage recorded) but that have no outcome newer
    than their last submission (a rerun after an invalid run needs a new outcome)."""
    out = []
    for exp in sorted(cfg.layout.experiments.glob("E*")):
        jobs = [r for r in read_jsonl(exp / "jobs.jsonl") if "submitted" in r]
        if not jobs:
            continue
        o = read_json(exp / "outcome.json") or {}
        if o and o.get("jobs_seen", len(jobs)) >= len(jobs):
            continue
        done = {u["job"] for u in read_jsonl(exp / "jobs_usage.jsonl")}
        if all(j["job"] in done for j in jobs):
            out.append(exp.name)
    return out


# ---------------------------------------------------------------- search tree

def tree(cfg: Config) -> str:
    nodes = {}
    for exp in sorted(cfg.layout.experiments.glob("E*")):
        if not (exp / "plan.md").is_file():
            continue
        meta, _ = read_frontmatter(exp / "plan.md")
        o = read_json(exp / "outcome.json") or {}
        ch = round(core_hours(exp), 1)
        nodes[exp.name] = {"parent": str(meta.get("parent", "") or ""), "hyp": meta.get("hypotheses", []),
                           "o": o, "ch": ch, "title": str(meta.get("hypothesis", ""))[:70]}
    kids: dict[str, list[str]] = {}
    for n, d in nodes.items():
        kids.setdefault(d["parent"] if d["parent"] in nodes else "", []).append(n)
    lines = ["# Search tree (generated by `autolab tree`)", "",
             "experiment [hypotheses] engineering/hypothesis/value, core-hours: reason", ""]

    def walk(n: str, depth: int) -> None:
        d = nodes[n]
        o = d["o"]
        state = "/".join(o.get(k, "?") for k in ("engineering", "hypothesis", "value")) if o else "open"
        lines.append(f"{'  ' * depth}- {n} [{', '.join(d['hyp'])}] {state}, {d['ch']} ch: "
                     f"{(o.get('reason') or d['title'])[:110]}")
        for k in sorted(kids.get(n, [])):
            walk(k, depth + 1)

    for root in sorted(kids.get("", [])):
        walk(root, 0)
    text = "\n".join(lines) + "\n"
    write_atomic(cfg.layout.state / "TREE.md", text)
    return text
