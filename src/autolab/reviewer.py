"""Reviewer: route each review task to a tier (model + effort) by difficulty, then run it.

Tiers follow ARIS reviewer-routing: verdict-bearing reviews never run below `xhigh`;
the `light` tier is for non-verdict checks only. A task's base tier comes from
[reviewer.tasks]; it is bumped one tier when the claim is central, the PI and reviewer
disagreed before, or the topic matches the domain pack's hard_topics.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from autolab.config import Config
from autolab.util import next_id, now_iso, read_frontmatter, write_atomic, write_json

ORDER = ("light", "regular", "deep")
VERDICT_TASKS = {"claim_support", "experiment_audit", "novelty", "kill_argument",
                 "paper_claim_audit", "final_cold_read", "value_grade", "review_loop_round",
                 "section_review", "citation_audit", "idea_review"}


def hard_topic_hit(cfg: Config, text: str) -> list[str]:
    """Topics match at a word start ("compressib" hits "compressible", not "incompressible").
    All-caps acronyms (LES, DNS) must match as whole words, case-sensitively."""
    hits = []
    for t in cfg.domain.get("reviewer", {}).get("hard_topics", []):
        if t.isupper():
            found = re.search(rf"\b{re.escape(t)}\b", text)
        else:
            found = re.search(rf"\b{re.escape(t)}", text, re.I)
        if found:
            hits.append(t)
    return hits


def route(cfg: Config, task: str, central: bool = False, disagreement: bool = False,
          text: str = "") -> dict:
    rv = cfg["reviewer"]
    if task not in rv["tasks"]:
        raise KeyError(f"unknown review task '{task}'; known: {sorted(rv['tasks'])}")
    tier = rv["tasks"][task]
    reasons = [f"base tier for {task}"]
    esc = rv["escalate"]
    hits = hard_topic_hit(cfg, text) if esc.get("hard_topic") else []
    bump = (central and esc.get("central_claim")) or (disagreement and esc.get("prior_disagreement")) or hits
    if bump and tier != ORDER[-1]:
        tier = ORDER[ORDER.index(tier) + 1]
        reasons.append("bumped: " + ", ".join(
            r for r, on in (("central claim", central), ("prior disagreement", disagreement),
                            (f"hard topic {hits}", bool(hits))) if on))
    spec = dict(rv["tiers"][tier])
    if task in VERDICT_TASKS and not spec.get("verdict_allowed"):
        tier = "regular"
        spec = dict(rv["tiers"][tier])
        reasons.append("verdict task cannot run on a non-verdict tier")
    return {"task": task, "tier": tier, "model": spec["model"], "effort": spec["effort"],
            "fallback_models": rv.get("fallback_models", []), "reasons": reasons}


def build_prompt(task: str, files: list[Path], instructions: str, root: Path,
                 verdict_allowed: bool = True) -> str:
    """Reviewer gets a role, an objective and file paths only — never the executor's summary."""
    def show(p: Path) -> str:
        try:
            return p.relative_to(root).as_posix()
        except ValueError:
            return str(p)
    rel = "\n".join(f"- {show(p)}" for p in files)
    if verdict_allowed:
        schema = ('  "verdict": "yes" | "partial" | "no" | "ready" | "almost" | "not ready" | "n/a",\n'
                  '  "grade": "A" | "B" | "C" | "D" | null,\n')
    else:
        schema = ('  "verdict": "n/a",   (this is a non-verdict check: list issues only)\n'
                  '  "grade": null,\n')
    return (
        f"You are an independent reviewer. Task: {task}.\n"
        f"{instructions.strip()}\n\n"
        f"Read these files yourself (do not rely on any summary):\n{rel}\n\n"
        "End your answer with one JSON object in a ```json fence with keys:\n" + schema +
        '  "issues": [{"id": str, "severity": "critical"|"major"|"minor", "text": str}],\n'
        '  "confidence": "low" | "medium" | "high"\n')


_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_verdict(text: str) -> dict:
    blocks = _JSON_BLOCK.findall(text)
    if not blocks:
        return {"verdict": "unparsed", "grade": None, "issues": [], "confidence": "low"}
    data = json.loads(blocks[-1])
    data.setdefault("issues", [])
    return data


def run(cfg: Config, task: str, files: list[Path], instructions: str = "",
        central: bool = False, disagreement: bool = False, out_dir: Path | None = None) -> dict:
    root = cfg.layout.root
    files = [p if p.is_absolute() else root / p for p in files]
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"review files not found: {missing}")
    text = " ".join(p.read_text(encoding="utf-8", errors="ignore")[:20000] for p in files)
    r = route(cfg, task, central, disagreement, text)
    verdict_allowed = bool(cfg["reviewer"]["tiers"][r["tier"]].get("verdict_allowed"))
    prompt = build_prompt(task, files, instructions, root, verdict_allowed)
    rid = next_id(cfg.layout.ids, "review", width=4)
    out_dir = out_dir or cfg.layout.reviews
    backend = cfg["reviewer"]["backend"]

    if backend == "mock":
        raw = _mock_review(files, root)
    elif backend in ("codex", "command"):
        # "command" = any CLI given in reviewer.command (prompt on stdin, answer in {out_file} or
        # stdout); "codex" is the same with the default Codex command line
        exe = cfg["reviewer"]["command"][0]
        if shutil.which(exe) is None:
            raise RuntimeError(f"reviewer CLI '{exe}' not on PATH; install it or set [reviewer] command/backend")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{rid}.last.md"
        cmd = [c.format(model=r["model"], effort=r["effort"], workdir=str(root), out_file=str(out_file))
               for c in cfg["reviewer"]["command"]]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=cfg["reviewer"]["timeout_s"], cwd=root)
        write_atomic(out_dir / f"{rid}.stderr.log", proc.stderr[-20000:])
        if proc.returncode != 0:
            raise RuntimeError(f"reviewer failed ({proc.returncode}): {proc.stderr[-2000:]}")
        raw = out_file.read_text(encoding="utf-8") if out_file.is_file() else proc.stdout
    else:
        raise ValueError(f"unknown reviewer backend '{backend}'")

    verdict = parse_verdict(raw)
    if not verdict_allowed:                       # a non-verdict tier can only report issues
        verdict["verdict"], verdict["grade"] = "n/a", None
    record = {"id": rid, "task": task, "route": r, "files": [str(f) for f in files],
              "backend": backend, "created": now_iso(), **verdict}
    write_atomic(out_dir / f"{rid}.prompt.md", prompt)
    write_atomic(out_dir / f"{rid}.raw.md", raw)
    write_json(out_dir / f"{rid}.json", record)
    return record


def _mock_review(files: list[Path], root: Path) -> str:
    """Deterministic stand-in for tests: returns mock_review.json found next to a reviewed file."""
    for f in files:
        p = (f if f.is_absolute() else root / f).parent / "mock_review.json"
        if p.is_file():
            return "mock review\n```json\n" + p.read_text(encoding="utf-8") + "\n```\n"
    return 'mock review\n```json\n{"verdict": "n/a", "grade": null, "issues": [], "confidence": "low"}\n```\n'
