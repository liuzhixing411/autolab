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


# Release of the frozen-evaluator step (0.5.0). An experiment whose first job was submitted before
# this could not have been frozen; without this exemption such experiments could never be closed and
# blocked every new plan (first-test A031, 2026-10-17: E012-E016 stuck for 37 hours).
EVALUATOR_SINCE = "2026-10-17T01:54:22-04:00"


def legacy_unfrozen(cfg: Config, eid: str) -> str | None:
    """Submission time of the first job if it predates frozen evaluators, else None."""
    import datetime as dt
    jobs = [r for r in read_jsonl(cfg.layout.experiments / eid / "jobs.jsonl") if "submitted" in r]
    if not jobs:
        return None
    try:
        first = min(dt.datetime.fromisoformat(j["submitted"]) for j in jobs)
        since = dt.datetime.fromisoformat(EVALUATOR_SINCE)
        if first.tzinfo is None:
            first = first.astimezone()
        return first.isoformat() if first < since else None
    except (TypeError, ValueError):
        return None


def evaluator_problems(cfg: Config, eid: str) -> tuple[list[str], list[str]]:
    exp = cfg.layout.experiments / eid
    rec = read_json(exp / "evaluator.json")
    if not rec:
        if not (exp / "plan.md").is_file():
            return ["experiment plan is missing"], []
        meta, _ = read_frontmatter(exp / "plan.md")
        if meta.get("analysis_command"):
            first = legacy_unfrozen(cfg, eid)
            if first:
                return [], [f"legacy: first job submitted {first}, before frozen evaluators existed "
                            f"({EVALUATOR_SINCE}); the analysis script was not fingerprinted by the framework"]
            return ["analysis_command has no frozen evaluator; register its scripts before running"], []
        return [], ["no frozen evaluator (plan has no analysis_command): the analysis was not fixed before the data"]
    amend = (exp / "plan_amendments.md").read_text(encoding="utf-8") if (exp / "plan_amendments.md").is_file() else ""
    probs = []
    for s, h in rec["files"].items():
        p = exp / s if (exp / s).is_file() else cfg.layout.root / s
        if not p.is_file() or sha256_file(p) != h:
            current = sha256_file(p) if p.is_file() else "missing"
            # An amendment is valid for one exact transition, never all future edits.
            if not any(s in line and h in line and current in line and "reason:" in line
                       and line.split("reason:", 1)[1].strip() for line in amend.splitlines()):
                probs.append(f"evaluator {s} changed: add a dated amendment naming {s}, old hash {h}, "
                             f"new hash {current}, and reason: ...")
    return probs, []


# ---------------------------------------------------------------- outcomes

ENGINEERING = ("valid", "invalid")
HYPOTHESIS = ("supports", "refutes", "inconclusive", "n/a")
VALUE = ("continue", "refine", "pivot", "stop")


def code_manifest(cfg: Config, eid: str) -> dict[str, str]:
    """Code and config files the run depends on (clean paths, outputs excluded): path -> sha256."""
    from autolab.slurm import _CODE_EXT, _OUTPUT_DIRS, clean_paths
    root = cfg.layout.root
    files = {}
    for rel in clean_paths(cfg, eid):
        p = root / rel
        if p.is_file():
            files[rel] = sha256_file(p)
        elif p.is_dir():
            for f in sorted(x for x in p.rglob("*") if x.is_file()):
                r = f.relative_to(p)
                if set(r.parts) & _OUTPUT_DIRS:
                    continue
                if f.suffix.lower() in _CODE_EXT or f.name.lower() in ("makefile", "plan.md"):
                    files[f.relative_to(root).as_posix()] = sha256_file(f)
    return files


def experiment_version(cfg: Config, eid: str, manifest: list[str] | None = None) -> str:
    """Bind an outcome to the run ledger, the code it ran, and the results. With `manifest` (the
    code files recorded with the outcome) only those files are hashed: a diagnosis script added to
    the experiment folder afterwards no longer makes the outcome stale (first-test RETRO-133);
    changing or removing a recorded file still does."""
    import hashlib
    import json
    from autolab.util import sha256_tree
    exp = cfg.layout.experiments / eid
    root = cfg.layout.root
    code = code_manifest(cfg, eid)
    if manifest is not None:
        # inside the experiment folder only the recorded files count (later diagnosis files do not);
        # shared code elsewhere (src/ ...) counts in full: a new shared file may change the run
        prefix = exp.relative_to(root).as_posix() + "/"
        code = {r: h for r, h in code.items() if not r.startswith(prefix)}
        code.update({r: sha256_file(root / r) if (root / r).is_file() else "missing"
                     for r in manifest if r.startswith(prefix)})
    parts = {"code": code}
    for name in ("plan.md", "jobs.jsonl", "results"):
        p = exp / name
        parts[name] = sha256_tree(p) if p.exists() else "missing"
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _legacy_version(cfg: Config, eid: str) -> str:
    """Version as computed before 0.6.2 (whole clean paths), for outcomes recorded then."""
    import hashlib
    import json
    from autolab.gates import _code_hash
    from autolab.slurm import clean_paths
    from autolab.util import sha256_tree
    exp = cfg.layout.experiments / eid
    parts = {}
    for rel in clean_paths(cfg, eid):
        p = cfg.layout.root / rel
        parts[rel] = _code_hash(p) if p.is_dir() else sha256_file(p) if p.is_file() else "missing"
    for name in ("plan.md", "jobs.jsonl", "results"):
        p = exp / name
        parts[name] = sha256_tree(p) if p.exists() else "missing"
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def outcome_current(cfg: Config, eid: str, outcome: dict) -> bool:
    if "code_manifest" in outcome:
        return outcome.get("version") == experiment_version(cfg, eid, list(outcome["code_manifest"]))
    return outcome.get("version") == _legacy_version(cfg, eid)


def _check_completed(cfg: Config, exp: Path, latest: dict, failed_ok: dict[str, str]) -> list[str]:
    """Problems that stop a valid outcome. A failed array task (or a whole secondary job) may be
    excused with a reason; at least one task of the run must have completed. One OOM task of an
    array used to void the whole experiment (first-test E020: full-connectivity task killed, the
    primary face task complete)."""
    from autolab import slurm
    finished = {r["job"]: r.get("state") for r in read_jsonl(exp / "jobs_usage.jsonl")}
    probs, completed = [], 0
    for j in latest.values():
        job = j["job"]
        st = finished.get(job)
        if st is None:
            probs.append(f"job {job} has no recorded usage yet (run `autolab tick`)")
            continue
        if st == "COMPLETED":
            completed += 1
            continue
        if job in failed_ok:
            continue
        tasks = slurm.task_states(job)
        bad = [t for t, s in tasks.items() if s != "COMPLETED" and t not in failed_ok]
        completed += sum(1 for t, s in tasks.items() if s == "COMPLETED" and t not in failed_ok)
        if bad or not tasks:
            probs.append(f"job {job} ended {st}" + (f" (tasks {', '.join(bad)})" if bad else "")
                         + "; excuse a secondary task with --failed-ok <job>_<task>=<reason>, or record invalid")
    if latest and not probs and not completed:
        probs.append("no task of this run completed")
    return probs


def record_outcome(cfg: Config, eid: str, engineering: str, hypothesis: str, value: str, reason: str,
                   evidence: list[str] | None = None, usd: float | None = None, changes_goal: bool = False,
                   failed_ok: dict[str, str] | None = None) -> dict:
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
    jobs = [r for r in read_jsonl(exp / "jobs.jsonl") if "submitted" in r]
    latest = {j.get("part"): j for j in jobs}
    failed_ok = {k.strip(): v.strip() for k, v in (failed_ok or {}).items()}
    if any(not v for v in failed_ok.values()):
        raise ValueError("every excused failure needs a reason (--failed-ok <job>_<task>=<reason>)")
    if engineering == "valid":
        probs = _check_completed(cfg, exp, latest, failed_ok)
        if probs:
            raise ValueError("valid outcome requires completed jobs: " + "; ".join(probs))
    from autolab.util import sha256_tree
    evidence_hashes = {}
    for e in evidence or []:
        p = cfg.layout.root / e
        if not p.exists():
            raise ValueError(f"missing outcome evidence: {e}")
        evidence_hashes[e] = sha256_tree(p)
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
    if any(w.startswith("legacy:") for w in warns):
        rec["legacy"] = True
        rec["evaluator"] = "legacy-unfrozen"
    manifest = code_manifest(cfg, eid)
    rec["code_manifest"] = manifest
    rec["version"] = experiment_version(cfg, eid, list(manifest))
    if failed_ok:
        rec["excused_failures"] = failed_ok
    rec["evidence_hashes"] = evidence_hashes
    rec["run_jobs"] = [j["job"] for j in latest.values()]
    with FileLock(exp / "outcome"):
        append_jsonl(exp / "outcomes.jsonl", rec)
        write_json(exp / "outcome.json", rec)
    from autolab.tasks import decide
    did = decide(cfg, f"{eid} outcome: engineering {engineering}, hypothesis {hypothesis}, value {value}"
                 + (f" (excused failures: " + "; ".join(f"{k}: {v}" for k, v in failed_ok.items()) + ")" if failed_ok else ""),
                 reason, ", ".join(evidence or []))
    rec["decision"] = did
    if value in ("pivot", "stop"):
        # stopping or redirecting a sub-direction is the PI's call (human, 2026-10-18); only a change of
        # the research goal or question waits for the human
        from autolab import human
        verb = {"stop": "停止", "pivot": "转向"}[value]
        if changes_goal:
            text = (f"{eid}: the PI proposes to {value.upper()} and change the research goal or question "
                    f"(hypothesis {hypothesis}). Reason: {reason.strip()} Evidence: "
                    f"{', '.join(evidence or []) or 'see outcome.json'}. Decision {did}.")
            human.request_action(cfg, "decision", text, blocking=True, rule="research_goal",
                                 dedupe=f"{eid}: the PI proposes to {value.upper()}")
            rec["needs_human"] = True
        else:
            human.notify(cfg, f"PI 已决定{verb} {eid} 这条子方向（研究目标不变），不同意请直接回复。"
                              f"背景：{reason.strip()[:300]}")
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
