"""Decide whether a PI turn is worth starting.

Overnight run (turns 15-16): most turns only polled running jobs, at $1-3 each. The runner
now asks `autolab wake check` first and skips the Claude turn when nothing changed since the
last turn. It always wakes on: no snapshot yet, human changes, new or updated handoff cards,
escalations, job state changes, or after `max_idle` minutes without a turn. Any error wakes.
"""

from __future__ import annotations

import time

from autolab import human
from autolab.config import Config
from autolab.util import now_iso, read_json, write_json


def _path(cfg: Config):
    return cfg.layout.state / "wake.json"


def _cards(cfg: Config) -> dict[str, float]:
    L = cfg.layout
    out = {}
    for card in list(L.experiments.glob("*/handoff.md")) + list(L.tasks.glob("*/handoff.md")):
        if not card.parent.name.startswith("_"):
            out[card.relative_to(L.root).as_posix()] = card.stat().st_mtime
    return out


def _escalations(cfg: Config) -> dict[str, float]:
    return {p.name: p.stat().st_mtime for p in cfg.layout.escalations.glob("*.md")}


def _jobs(cfg: Config) -> dict[str, str]:
    from autolab import slurm
    try:
        return {s["job"]: s["state"] for s in slurm.status(cfg)}
    except RuntimeError:            # no scheduler here
        return {}


def _decision_count(cfg: Config) -> int:
    p = cfg.layout.state / "DECISIONS.md"
    if not p.is_file():
        return 0
    import re
    return len(re.findall(r"(?m)^## D\d+ ", p.read_text(encoding="utf-8")))


def check(cfg: Config, max_idle_min: float = 45, now: float | None = None) -> tuple[bool, list[str]]:
    now = now or time.time()
    snap = read_json(_path(cfg))
    if not snap:
        return True, ["no previous snapshot"]
    reasons = []
    from autolab import autonomy
    late = autonomy.overdue(cfg)
    if late:
        reasons.append(f"human requests past their deadline: {late}")
    jobs = _jobs(cfg)
    from autolab.slurm import TERMINAL
    # Space forced turns out only when the lab is really just waiting: requests open, no jobs
    # running, AND the last turn decided nothing. After a productive turn the normal interval
    # stays (the PI may have other tracks to move; first-test 2026-10-20: work sat for 1.5 h).
    idle_last_turn = "decisions" in snap and snap.get("decisions") == snap.get("decisions_before")
    if idle_last_turn and autonomy.blocked(cfg) and not any(st not in TERMINAL for st in jobs.values()):
        max_idle_min = max(max_idle_min, autonomy.settings(cfg)["blocked_max_idle_min"])
    if now - snap.get("t", 0) > max_idle_min * 60:
        reasons.append(f"no PI turn for more than {max_idle_min:.0f} min")
    hc = human.check_changes(cfg, now=now, commit=False)
    if hc["changed"] or human.pending_ack(cfg):
        reasons.append(f"human changes: {hc['changed'] or human.pending_ack(cfg)}")
    for k, m in _cards(cfg).items():
        if snap.get("cards", {}).get(k) != m:
            reasons.append(f"handoff card new or updated: {k}")
    for k, m in _escalations(cfg).items():
        if snap.get("escalations", {}).get(k) != m:
            reasons.append(f"escalation: {k}")
    for j, st in jobs.items():
        if snap.get("jobs", {}).get(j) != st:
            reasons.append(f"job {j}: {snap.get('jobs', {}).get(j)} -> {st}")
    return bool(reasons), reasons


def mark(cfg: Config) -> dict:
    """After a turn: `decisions_before` is the count at the previous mark, so check() can tell
    whether the turn just finished recorded any decision."""
    prev = read_json(_path(cfg)) or {}
    snap = {"t": time.time(), "at": now_iso(), "cards": _cards(cfg),
            "escalations": _escalations(cfg), "jobs": _jobs(cfg),
            "decisions": _decision_count(cfg), "decisions_before": prev.get("decisions")}
    write_json(_path(cfg), snap)
    return snap
