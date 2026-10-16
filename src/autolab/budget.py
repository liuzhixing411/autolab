"""API spend ledger. The PI runner records the cost Claude Code reports for each turn
(stream-json `result` event, `total_cost_usd`, which includes subagents) and stops the
loop once `budget.total_usd` is reached. With a subscription login this figure is
Claude Code's estimate, not an invoice; treat it as a usage proxy.

Reviewer (Codex) calls on a ChatGPT login are not billed per call and are not counted.
"""

from __future__ import annotations

import json
from pathlib import Path

from autolab.config import Config
from autolab.util import append_jsonl, now_iso, read_jsonl


def _ledger(cfg: Config) -> Path:
    return cfg.layout.state / "budget.jsonl"


def cost_from_log(log: Path) -> float | None:
    cost = None
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
            cost = float(ev["total_cost_usd"])
    return cost


def record_turn(cfg: Config, log: Path, source: str = "pi_turn") -> dict:
    cost = cost_from_log(log)
    rec = {"at": now_iso(), "source": source, "usd": cost or 0.0, "log": str(log),
           "cost_reported": cost is not None}
    append_jsonl(_ledger(cfg), rec)
    return rec


def status(cfg: Config) -> dict:
    spent = round(sum(r.get("usd", 0.0) for r in read_jsonl(_ledger(cfg))), 4)
    cap = float(cfg["budget"].get("total_usd") or 0.0)
    return {"spent_usd": spent, "cap_usd": cap, "over": bool(cap) and spent >= cap,
            "turns": len(read_jsonl(_ledger(cfg)))}
