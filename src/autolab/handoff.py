"""Student → PI handoff cards.

A card is experiments/<E>/handoff.md (or state/tasks/<T>/handoff.md) with TOML
frontmatter. Numbers quoted in the result must appear in results/metrics.json, so a
summary cannot drift from the data. Cards are for the PI only; the reviewer always
reads primary files (ARIS reviewer-independence).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from autolab.util import read_frontmatter, sections

REQUIRED = ("task_id", "status", "plan_followed", "confidence", "needs_pi_decision")
STATUS = {"done", "partial", "blocked"}
SECTIONS = ("result", "deviations from plan", "anomalies", "suggested next steps")
VALUE_WORDS = ("promising", "remarkable", "groundbreaking", "breakthrough", "impressive")
_NUM = re.compile(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _metric_numbers(obj) -> list[float]:
    out = []
    if isinstance(obj, dict):
        for v in obj.values():
            out += _metric_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _metric_numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    return out


def _matches(x: float, pool: list[float]) -> bool:
    for y in pool:
        for scale in (1.0, 100.0, 0.01):        # allow 0.874 ↔ 87.4 %
            v = y * scale
            if v == 0 and x == 0:
                return True
            if v != 0 and abs(x - v) <= max(abs(v) * 5e-3, 10 ** -_decimals(x) / 2 + 1e-12):
                return True
    return False


def _decimals(x: float) -> int:
    s = repr(x)
    return len(s.split(".")[1]) if "." in s and "e" not in s else 0


def validate(card: Path, metrics: Path | None = None) -> dict:
    problems, warnings = [], []
    if not card.is_file():
        return {"ok": False, "problems": [f"{card} missing"], "warnings": []}
    meta, body = read_frontmatter(card)
    for k in REQUIRED:
        if k not in meta:
            problems.append(f"missing field '{k}'")
    if meta.get("status") and meta["status"] not in STATUS:
        problems.append(f"status must be one of {sorted(STATUS)}")
    sec = sections(body)
    for s in SECTIONS:
        if not sec.get(s, "").strip():
            problems.append(f"section '## {s.title()}' is empty or missing")
    result = sec.get("result", "")
    if len(result.split()) > 120:
        warnings.append("result section is long; keep it to the key numbers and condition")
    for w in VALUE_WORDS:
        if re.search(rf"\b{w}\b", body, re.I):
            problems.append(f"value word '{w}': value is judged by PI/reviewer, not the student")

    # Numbers must trace to metrics.json for experiment cards. For other tasks (literature,
    # planning) the rule only warns: counts there are not measurements.
    strict = "experiments" in card.resolve().parts
    sink = problems if strict else warnings
    metrics = metrics or card.parent / "results" / "metrics.json"
    numbers = [float(n) for n in _NUM.findall(result)]
    if numbers:
        if not metrics.is_file():
            sink.append(f"result quotes numbers but {metrics} does not exist")
        else:
            pool = _metric_numbers(json.loads(metrics.read_text(encoding="utf-8")))
            # configuration values (grid size, thresholds, seeds) may be quoted too: they come
            # from the experiment's config and plan files, not from measurements
            exp = card.parent
            for f in [*exp.glob("config*.*"), *exp.glob("configs/*"), exp / "plan.md",
                      *exp.glob("results/*.json"), *exp.glob("results/*/*.json"),
                      *exp.glob("results/*/*/*.json"), exp / "jobs.jsonl"]:
                if f.is_file() and f.stat().st_size < 1_000_000:
                    pool += [float(x) for x in _NUM.findall(f.read_text(encoding="utf-8", errors="ignore"))]
            # job ids and similar identifiers (7+ digit integers) are not measurements
            missing = [n for n in numbers if not _matches(n, pool) and not (n.is_integer() and abs(n) >= 1e6)]
            if missing:
                sink.append(f"numbers not found in metrics.json: {missing}")
    if meta.get("status") == "blocked" and not meta.get("escalation"):
        problems.append("status=blocked needs `escalation` = path of the escalation report")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "meta": meta}
