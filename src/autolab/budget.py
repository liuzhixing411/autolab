"""API spend ledger.

The PI runner records the cost Claude Code reports for each turn (stream-json `result` event,
`total_cost_usd`, which includes subagents) and stops the loop before `budget.total_usd` is
reached. With a subscription login this figure is Claude Code's estimate, not an invoice.

A turn that ends without a result event (killed by the time limit, crashed) used to be booked as
0 USD. Now its token usage is recorded and the cost is booked as *unknown*: status() charges it
provisionally (`budget.unknown_turn_usd`, default: the most expensive known turn) and lists it for
reconciliation. Reviewer calls are recorded too (`reviewer.usd_per_call`; 0 on a subscription
login, where they are not billed per call). Before a turn the runner requires the remaining budget
to cover a reserve and caps the turn itself with `claude -p --max-budget-usd`.
"""

from __future__ import annotations

import json
from pathlib import Path

from autolab.config import Config
from autolab.util import FileLock, append_jsonl, now_iso, read_jsonl


def _ledger(cfg: Config) -> Path:
    return cfg.layout.state / "budget.jsonl"


def cost_from_log(log: Path) -> tuple[float | None, dict]:
    """(reported cost or None, token usage summed per model, deduplicated by message id)."""
    cost = None
    seen: set[str] = set()
    usage: dict[str, dict[str, int]] = {}
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result" and ev.get("total_cost_usd") is not None:
            cost = float(ev["total_cost_usd"])
        msg = ev.get("message") if ev.get("type") == "assistant" else None
        if isinstance(msg, dict) and msg.get("usage") and msg.get("id") not in seen:
            seen.add(msg.get("id"))
            u = usage.setdefault(msg.get("model", "?"), {})
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                u[k] = u.get(k, 0) + int(msg["usage"].get(k) or 0)
    return cost, usage


def record_cost(cfg: Config, source: str, usd: float | None, extra: dict | None = None,
                known: bool = True) -> dict:
    rec = {"at": now_iso(), "source": source, "usd": usd if usd is not None else 0.0,
           "cost_reported": usd is not None and known, **(extra or {})}
    if usd is None and not known:
        rec["unknown"] = True
    with FileLock(_ledger(cfg)):
        append_jsonl(_ledger(cfg), rec)
    return rec


def record_turn(cfg: Config, log: Path, source: str = "pi_turn") -> dict:
    cost, usage = cost_from_log(log)
    return record_cost(cfg, source, cost, {"log": str(log), "usage": usage}, known=cost is not None)


def status(cfg: Config, reserve: bool = False) -> dict:
    recs = read_jsonl(_ledger(cfg))
    known = [r for r in recs if not r.get("unknown")]
    unknown = [r for r in recs if r.get("unknown")]
    turns = [r for r in recs if r.get("source") == "pi_turn"]
    biggest = max((r.get("usd", 0.0) for r in known if r.get("source") == "pi_turn"), default=0.0)
    per_unknown = float(cfg["budget"].get("unknown_turn_usd") or 0) or max(biggest, 5.0)
    per_review = float(cfg["reviewer"].get("unknown_call_usd") or 1.0)
    provisional = sum(per_review if r.get("source") == "review" else per_unknown for r in unknown)
    spent = round(sum(r.get("usd", 0.0) for r in known) + provisional, 4)
    cap = float(cfg["budget"].get("total_usd") or 0.0)
    turn_reserve = float(cfg["budget"].get("turn_reserve_usd") or 0) or max(biggest, 5.0)
    out = {"spent_usd": spent, "cap_usd": cap, "turns": len(turns),
           "unknown_cost_turns": len(unknown), "unknown_turn_charge_usd": per_unknown,
           "turn_reserve_usd": turn_reserve, "remaining_usd": round(cap - spent, 4) if cap else None}
    need = turn_reserve if reserve else 0.0
    out["over"] = bool(cap) and spent + need >= cap
    if unknown:
        out["reconcile"] = [r.get("log", r.get("source")) for r in unknown]
    return out


def turn_cap(cfg: Config) -> float | None:
    """USD cap for the next PI turn (`claude -p --max-budget-usd`): what is left after the
    other turns' charges, never more than `budget.max_turn_usd` if set. None = no cap."""
    st = status(cfg)
    caps = [c for c in (st["remaining_usd"], float(cfg["budget"].get("max_turn_usd") or 0) or None) if c]
    return round(max(min(caps), 0.01), 2) if caps else None
