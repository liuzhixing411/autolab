"""`autolab tick`: deterministic housekeeping + the one digest the PI reads each turn.

Runs without any model call: detects human edits, registers uploads, files dropped PDFs,
validates new handoff cards, polls jobs, and summarizes open gates. The PI then reads
state/digests/TICK-<n>.md (L0) instead of walking the tree.
"""

from __future__ import annotations

from autolab import handoff, human, knowledge, ledger
from autolab.config import Config
from autolab.util import now_iso, read_json, write_atomic, write_json


def _new_handoffs(cfg: Config) -> list[dict]:
    seen_file = cfg.layout.state / "handoffs_seen.json"
    seen = read_json(seen_file, {}) or {}
    out = []
    cards = list(cfg.layout.experiments.glob("*/handoff.md")) + list(cfg.layout.tasks.glob("*/handoff.md"))
    for card in sorted(cards):
        if card.parent.name.startswith("_"):          # templates
            continue
        key = card.relative_to(cfg.layout.root).as_posix()
        mtime = card.stat().st_mtime
        if seen.get(key) == mtime:
            continue
        res = handoff.validate_in(cfg, card)
        out.append({"card": key, "ok": res["ok"], "problems": res["problems"],
                    "status": res.get("meta", {}).get("status"),
                    "needs_pi_decision": res.get("meta", {}).get("needs_pi_decision")})
        seen[key] = mtime
    write_json(seen_file, seen)
    return out


def _jobs(cfg: Config) -> list[dict]:
    try:
        from autolab import slurm
        return slurm.status(cfg)
    except RuntimeError as e:       # not on a cluster
        return [{"note": str(e)}]


def run(cfg: Config) -> dict:
    tick_file = cfg.layout.state / "tick.json"
    prev_t = tick_file.stat().st_mtime if tick_file.is_file() else 0.0
    counter = (read_json(cfg.layout.state / "tick.json", {}) or {}).get("n", 0) + 1
    write_json(cfg.layout.state / "tick.json", {"n": counter, "at": now_iso()})

    changes = human.check_changes(cfg)
    answered = human.resolve_from_inbox(cfg) if not changes["settling"] else []
    ups = human.scan_uploads(cfg)
    pdfs = knowledge.intake_pdf_drop(cfg)
    cards = _new_handoffs(cfg)
    jobs = _jobs(cfg)
    try:
        from autolab import slurm
        used = slurm.record_usage(cfg)
    except (RuntimeError, OSError):         # no scheduler here
        used = []
    escalations = sorted(p.name for p in cfg.layout.escalations.glob("*.md"))
    claims = []
    for d in sorted(cfg.layout.claims.glob("C*")):
        g = read_json(d / "gate.json", {}) or {}
        claims.append(f"{d.name}: {g.get('decision', 'not gated')}")
    loops = {p.stem: ledger.check(cfg, p.stem) for p in cfg.layout.ledger.glob("*.json")}
    from autolab import skills as _skills, tasks as _tasks
    used_skills = _skills.invoked_since(cfg, prev_t) if prev_t else {}
    retro_last = _tasks.retro_due(cfg, counter)

    L = [f"# Tick {counter} — {now_iso()}", ""]
    if human.stopped(cfg):
        L += ["**STOP present: do nothing but read. The human paused the lab.**", ""]
    deleg = human.delegation(cfg)
    if deleg:
        L += [f"**Human away; final authority delegated to the PI until {deleg['until']}.** Decide the "
              "RULES 'ask me first' items yourself, log each with `autolab human request --kind decision "
              "\"PI decided: ...; reason: ...; evidence: ...\"` (recorded for review, not blocking) and in "
              "STATE.md. Hard limits still apply.", ""]
    if not human.is_ready(cfg):
        L += ["**Human input not ready (no 00_human/READY).** Do not plan, dispatch or submit. "
              "If something in the templates is unclear, add one `autolab human request --kind decision` "
              "(only once), then end the turn.", ""]
    pending = human.pending_ack(cfg)
    if pending:
        L += [f"## Human changed: {', '.join(pending)}",
              "Read the changed files, re-plan if needed, then `autolab human ack \"what you did\"`.", ""]
    if changes["settling"]:
        L += [f"_Human is still editing: {', '.join(changes['settling'])} (not applied yet)._", ""]
    if answered:
        L += ["## Requests answered by the human (inbox)"] + [f"- {a}: read `{f}` and act on it" for a, f in answered] + [""]
    unfilled = human.unfilled_templates(cfg)
    if unfilled:
        L += [f"## Templates not filled by the human: {', '.join(unfilled)}", ""]
    if ups["added"] or ups["invalid"]:
        L += ["## Human uploads"] + [f"- new {u['id']}: {u['title']} ({u['kind']}, use as {u['use_as']})"
                                     for u in ups["added"]]
        L += [f"- invalid {u['path']}: {'; '.join(u['problems'])}" for u in ups["invalid"]] + [""]
    if pdfs:
        L += [f"## PDFs filed from pdf_drop: {', '.join(pdfs)} — write reading receipts", ""]
    if cards:
        L += ["## New handoff cards"]
        for c in cards:
            flag = "OK" if c["ok"] else "RETURN TO STUDENT: " + "; ".join(c["problems"])
            L.append(f"- `{c['card']}` status={c['status']} decision_needed={c['needs_pi_decision']} — {flag}")
        L.append("")
    if escalations:
        L += ["## Escalations waiting"] + [f"- state/escalations/{e}" for e in escalations] + [""]
    if jobs:
        L += ["## Jobs"] + [f"- {j}" for j in jobs] + [""]
    if used:
        L += ["## Finished jobs: what they really used (size the next request from this)"]
        L += [f"- {u}" for u in used] + [""]
    if claims:
        L += ["## Claims"] + [f"- {c}" for c in claims] + [""]
    if loops:
        L += ["## Review loops"] + [f"- {k}: {v['state']} {v['reasons']}" for k, v in loops.items()] + [""]
    from autolab import budget
    b = budget.status(cfg)
    if b["cap_usd"]:
        L += [f"## Budget: ${b['spent_usd']:.2f} of ${b['cap_usd']:.2f} spent over {b['turns']} turns", ""]
    if used_skills:
        L += ["## Skills invoked since the last tick: " + ", ".join(f"{k} x{v}" for k, v in sorted(used_skills.items())), ""]
    elif prev_t and _skills.have_logs(cfg):
        L += ["## Skills invoked since the last tick: none. Task cards list required skills; students must invoke them.", ""]
    if retro_last is not None:
        L += [f"## Retrospective due (last at tick {retro_last or 'never'})",
              f"Dispatch one student (analysis, sonnet) to write `experience/RETRO-{counter}.md` from state/DECISIONS.md, "
              "rejected claims, escalations, blocked tasks and handoff anomalies since then: what went wrong or slow, "
              "the root cause, and a rule that would have prevented it. Add each rule with "
              "`autolab lesson add \"<rule>\" --origin \"<what happened>\" --keywords k1,k2`; context packs then "
              "surface it before related work. Framework problems go to the human in ACTION_NEEDED.", ""]
    actions = human.open_actions(cfg)
    if actions:
        L += [f"## Open requests to the human: {len(actions)} (see 00_human/ACTION_NEEDED.md)", ""]
    path = cfg.layout.digests / f"TICK-{counter:04d}.md"
    write_atomic(path, "\n".join(L) + "\n")
    return {"tick": counter, "digest": str(path), "ready": human.is_ready(cfg),
            "stopped": human.stopped(cfg), "human_changed": pending,
            "new_cards": len(cards), "uploads": len(ups["added"])}
