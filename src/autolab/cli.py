"""Command line: every interface the agents and the human use goes through here."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autolab.paths import find_root


def _cfg(args):
    from autolab.config import Config
    return Config(find_root(Path(args.project) if args.project else None))


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_init(a):
    from autolab import project
    p = project.init(Path(a.path), a.name or Path(a.path).resolve().name, a.domain, a.site, a.force)
    print(f"initialized {p}\nnext: set [slurm] account etc. in autolab.toml, run `autolab doctor`, then follow "
          "00_human/START_HERE.md")


def cmd_doctor(a):
    from autolab import project
    rows = project.doctor(_cfg(a))
    for name, ok, detail in rows:
        print(f"[{'ok' if ok else '--'}] {name}{('  ' + detail) if detail else ''}")
    return 0 if all(ok for name, ok, _d in rows if not name_optional(name)) else 1


def name_optional(name: str) -> bool:
    return name.startswith(("slurm", "reviewer CLI", "claude CLI", "human templates", "human READY"))


def cmd_tick(a):
    from autolab import tick
    _out(tick.run(_cfg(a)))


def cmd_skills(a):
    from autolab import project, skills
    cfg = _cfg(a)
    if a.action == "resolve":
        _out(skills.resolve(cfg, a.role, a.stage, a.task))
    elif a.action == "mount":
        m = project.remount(cfg)
        print(f"mounted {len(m['skills'])} skills for domain '{m['domain']}'")
    elif a.action == "check":
        probs = skills.check(cfg)
        _out(probs)
        return 1 if probs else 0
    elif a.action == "list":
        _out(sorted(skills.skill_sources(cfg)))


def cmd_human(a):
    from autolab import human
    cfg = _cfg(a)
    if a.action == "check":
        _out({**human.check_changes(cfg), "pending_ack": human.pending_ack(cfg)})
    elif a.action == "ack":
        _out({"acknowledged": human.ack(cfg, a.text)})
    elif a.action == "request":
        # decisions are blocking unless explicitly informational (--fyi): turn 6 filed a
        # RULES-required stop/modify decision as optional
        blocking = a.blocking or (a.kind == "decision" and not a.fyi)
        text = f"**{a.title}** — {a.text}" if a.title else a.text
        deleg = human.delegation(cfg)
        if deleg and a.kind == "decision":
            # the human handed final authority to the PI: log for review, never block
            blocking = False
            text = f"[PI decided under delegation until {deleg['until']}; for human review] {text}"
        print(human.request_action(cfg, a.kind, text, blocking, dedupe=a.dedupe) or "")
    elif a.action == "uploads":
        _out(human.scan_uploads(cfg))


def cmd_lit(a):
    from autolab import knowledge
    cfg = _cfg(a)
    if a.action == "add":
        kw = [k.strip() for k in (a.keywords or "").split(",") if k.strip()]
        _out(knowledge.lit_add(cfg, a.title, a.doi or "", a.year, a.tier, a.role, a.access, kw))
    elif a.action == "update":
        kw = [k.strip() for k in a.keywords.split(",")] if a.keywords else None
        _out(knowledge.lit_update(cfg, a.id, access=a.access_update, pdf=a.pdf, tier=a.tier_update,
                                  role=a.role_update, keywords=kw, url=a.url, title=a.title,
                                  doi=a.doi, year=a.year))
    elif a.action == "text":
        ids = ([r["id"] for r in knowledge.lit_records(cfg)
                if (cfg.layout.literature / "pdf" / f"{r['id']}.pdf").is_file()] if a.id in (None, "all") else [a.id])
        _out([knowledge.pdf_text(cfg, i) for i in ids])
    elif a.action == "note":
        print(knowledge.note_template(cfg, a.id))
    elif a.action == "list":
        _out(knowledge.lit_records(cfg))
    elif a.action == "intake":
        _out(knowledge.intake_pdf_drop(cfg))


def cmd_preflight(a):
    from autolab import knowledge
    inc = [i.strip() for i in (a.include or "").split(",") if i.strip()]
    kw = " ".join(a.keywords) if isinstance(a.keywords, list) else a.keywords
    pack = knowledge.build_context_pack(_cfg(a), a.topic, [k.strip() for k in kw.split(",") if k.strip()], inc)
    print(f"{pack['id']}: {len(pack['items'])} items "
          f"({sum(i['must_read'] for i in pack['items'])} must-read) → state/context_packs/{pack['id']}.md")


def cmd_gate(a):
    cfg = _cfg(a)
    if a.kind == "plan":
        from autolab import knowledge
        res = knowledge.check_plan(cfg, a.id)
    elif a.kind == "claim":
        from autolab import gates
        res = gates.claim_gate(cfg, a.id)
    else:
        from autolab import handoff
        res = handoff.validate_in(cfg, Path(a.id))
        res.pop("meta", None)
    _out(res)
    return 0 if res.get("ok", res.get("decision") == "accepted") else 1


def cmd_review(a):
    from autolab import reviewer
    from autolab.util import read_frontmatter, read_json, write_json
    cfg = _cfg(a)
    if a.action == "route":
        _out(reviewer.route(cfg, a.task, a.central, a.disagreement, a.text or ""))
        return
    files = [Path(f) for f in a.files]
    subject = None
    if a.claim:
        from autolab import gates
        from autolab.util import FileLock
        files = [cfg.layout.claims / a.claim / "claim.md"] + files
        meta, _ = read_frontmatter(cfg.layout.claims / a.claim / "claim.md")
        for e in list(meta.get("evidence", [])) + list(meta.get("validation", {}).values()):
            if (cfg.layout.root / e).is_file() and cfg.layout.root / e not in files:
                files.append(cfg.layout.root / e)            # the reviewer reads the evidence itself
        fp = gates.fingerprint(cfg, a.claim)              # the review counts for this version only
        subject = {"claim": a.claim, **fp}
    rec = reviewer.run(cfg, a.task, files, a.instructions or "", a.central, a.disagreement, subject=subject)
    if a.claim:
        p = cfg.layout.claims / a.claim / "reviews.json"
        with FileLock(p):
            write_json(p, (read_json(p, []) or []) + [rec["id"]])
    if a.loop:
        from autolab import ledger
        ledger.record_round(cfg, a.loop, rec)
    _out({k: rec[k] for k in ("id", "task", "route", "verdict", "grade") if k in rec})


def cmd_ledger(a):
    from autolab import ledger
    cfg = _cfg(a)
    if a.action == "check":
        _out(ledger.check(cfg, a.loop))
    elif a.action == "set":
        _out(ledger.set_status(cfg, a.loop, a.issue, a.status, a.note or ""))
    elif a.action == "show":
        _out(ledger.load(cfg, a.loop))


def cmd_slurm(a):
    from autolab import slurm
    cfg = _cfg(a)
    if a.action == "render":
        path, r = slurm.render(cfg, a.eid, a.part)
        print(path)
    elif a.action == "validate":
        res = slurm.validate(cfg, a.eid, a.part)
        _out(res)
        return 0 if res["ok"] else 1
    elif a.action == "submit":
        _out(slurm.submit(cfg, a.eid, a.part))
    elif a.action == "status":
        _out(slurm.status(cfg, a.eid))
    elif a.action == "cancel":
        _out({"cancelled": slurm.cancel(cfg, a.eid, a.job)})
    elif a.action == "usage":
        c = slurm.committed_core_hours(cfg)
        _out({"core_hours_used": slurm.core_hours_used(cfg), "finished_jobs": c["used"],
              "reserved_for_unfinished": c["reserved"], "committed": c["committed"],
              "cap": cfg["slurm"].get("total_core_hours") or None})


def cmd_budget(a):
    from autolab import budget
    cfg = _cfg(a)
    if a.action == "record":
        _out(budget.record_turn(cfg, Path(a.log)))
        return
    if a.action == "turn-cap":
        cap = budget.turn_cap(cfg)
        print("" if cap is None else f"{cap:.2f}")
        return 0
    st = budget.status(cfg, reserve=(a.action == "check"))
    _out(st)
    return 4 if st["over"] else 0


def cmd_task(a):
    from autolab import tasks
    p = tasks.new(_cfg(a), a.title, a.student, a.task, a.stage, a.difficulty, a.model,
                  [s for s in (a.skills or "").split(",") if s.strip()], a.track)
    print(p)


def cmd_decide(a):
    from autolab import tasks
    print(tasks.decide(_cfg(a), a.text, a.why, a.evidence, a.track, a.by))


def cmd_lesson(a):
    from autolab import tasks
    print(tasks.add_lesson(_cfg(a), a.text, a.origin, (a.keywords or "").split(",")))


def cmd_guard(a):
    from autolab import guard
    cfg = _cfg(a)
    if a.action == "snapshot":
        _out(guard.snapshot(cfg))
        return 0
    from autolab.util import now_iso
    res = guard.check(cfg, Path(a.log) if a.log else None)
    if res.get("violations"):
        print(f"{now_iso()} guard: {len(res['violations'])} violation(s): "
              + "; ".join(f"{v['file']}: {v['kind']}" for v in res["violations"]))
        return 5
    return 0


def cmd_wake(a):
    from autolab import wake
    cfg = _cfg(a)
    if a.action == "mark":
        wake.mark(cfg)
        return 0
    try:
        go, reasons = wake.check(cfg, a.max_idle)
    except Exception as e:                     # never let the check stall the lab
        print(f"wake: check failed ({e}); waking")
        return 0
    print(("WAKE: " if go else "IDLE: nothing new since the last turn") + "; ".join(reasons))
    return 0 if go else 3


def cmd_hook(a):
    from autolab import hooks
    return hooks.main()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autolab")
    p.add_argument("--project", help="project folder (default: search upward for autolab.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a project from the template")
    s.add_argument("path")
    s.add_argument("--name")
    s.add_argument("--domain", default="generic")
    s.add_argument("--site", default="local")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    sub.add_parser("doctor", help="check every interface").set_defaults(fn=cmd_doctor)
    sub.add_parser("tick", help="housekeeping + write the PI digest").set_defaults(fn=cmd_tick)

    s = sub.add_parser("skills")
    s.add_argument("action", choices=["resolve", "mount", "check", "list"])
    s.add_argument("--role", default="pi")
    s.add_argument("--stage")
    s.add_argument("--task")
    s.set_defaults(fn=cmd_skills)

    s = sub.add_parser("human")
    s.add_argument("action", choices=["check", "ack", "request", "uploads"])
    s.add_argument("text", nargs="?", default="")
    s.add_argument("--kind", default="other", choices=["download", "decision", "data", "approval", "other"])
    s.add_argument("--blocking", action="store_true")
    s.add_argument("--fyi", action="store_true", help="decision requests are blocking unless marked informational")
    s.add_argument("--title", help="short title shown in bold before the text")
    s.add_argument("--dedupe", help="add nothing while an open request contains this text")
    s.set_defaults(fn=cmd_human)

    s = sub.add_parser("lit", help="literature index: add | update | text (PDF→text) | note (reading receipt) | list | intake (pdf_drop)")
    s.add_argument("action", choices=["add", "update", "text", "note", "list", "intake"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--doi")
    s.add_argument("--year", type=int)
    s.add_argument("--tier", default="B", help="add only; update uses --set-tier")
    s.add_argument("--role", default="background", help="add only; update uses --set-role")
    s.add_argument("--access", default="abstract_only",
                   help="add: initial access; update: use --set-access")
    s.add_argument("--keywords")
    s.add_argument("--set-access", dest="access_update", help="update: oa|institutional|human_needed|abstract_only|have_pdf")
    s.add_argument("--set-tier", dest="tier_update")
    s.add_argument("--set-role", dest="role_update")
    s.add_argument("--pdf", help="update: path of the downloaded PDF inside the project")
    s.add_argument("--url", help="update: where the full text was obtained")
    s.set_defaults(fn=cmd_lit)

    s = sub.add_parser("preflight", help="build the context pack required before a new plan")
    s.add_argument("topic")
    s.add_argument("--keywords", nargs="*", default=[], help="comma-separated; spaces inside a keyword are fine even unquoted")
    s.add_argument("--include", help="comma-separated IDs (L0033,wiki:...,C009) that must be in the pack")
    s.set_defaults(fn=cmd_preflight)

    s = sub.add_parser("gate")
    s.add_argument("kind", choices=["plan", "claim", "handoff"])
    s.add_argument("id", help="E-id, C-id, or path to handoff.md")
    s.set_defaults(fn=cmd_gate)

    s = sub.add_parser("review")
    s.add_argument("action", choices=["route", "run"])
    s.add_argument("task")
    s.add_argument("files", nargs="*")
    s.add_argument("--claim")
    s.add_argument("--loop")
    s.add_argument("--central", action="store_true")
    s.add_argument("--disagreement", action="store_true")
    s.add_argument("--text")
    s.add_argument("--instructions")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("ledger")
    s.add_argument("action", choices=["check", "set", "show"])
    s.add_argument("loop")
    s.add_argument("--issue")
    s.add_argument("--status")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("slurm")
    s.add_argument("action", choices=["render", "validate", "submit", "status", "cancel", "usage"])
    s.add_argument("--job", help="cancel: one recorded job id (default: all unfinished jobs of the experiment)")
    s.add_argument("eid", nargs="?")
    s.add_argument("--part", help="a [parts.<name>] block of plan.md (runs that write separate outputs)")
    s.set_defaults(fn=cmd_slurm)

    s = sub.add_parser("budget", help="record a PI turn's cost; status; check (cap minus a turn reserve, exit 4 = stop); turn-cap")
    s.add_argument("action", choices=["record", "status", "check", "turn-cap"])
    s.add_argument("log", nargs="?")
    s.set_defaults(fn=cmd_budget)

    s = sub.add_parser("task", help="new task card with the required skills of its route")
    s.add_argument("action", choices=["new"])
    s.add_argument("title")
    s.add_argument("--student", required=True, choices=["literature", "experiment", "analysis", "figure", "writing"])
    s.add_argument("--task", help="route task (literature, novelty, experiment, analysis, theory, figure, "
                                  "section, compile, debug, review, rigor); default = the student role")
    s.add_argument("--stage", help="route stage, e.g. writing or polish")
    s.add_argument("--difficulty", default="standard", choices=["mechanical", "standard", "hard"])
    s.add_argument("--model", choices=["haiku", "sonnet", "opus"], help="default: from difficulty")
    s.add_argument("--skills", help="extra skills, comma-separated")
    s.add_argument("--track", default="")
    s.set_defaults(fn=cmd_task)

    s = sub.add_parser("decide", help="append a decision to state/DECISIONS.md (append-only)")
    s.add_argument("text")
    s.add_argument("--why", required=True)
    s.add_argument("--evidence", default="")
    s.add_argument("--track", default="")
    s.add_argument("--by", default="PI")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("lesson", help="record a rule learned in a retrospective (surfaces in context packs)")
    s.add_argument("action", choices=["add"])
    s.add_argument("text")
    s.add_argument("--origin", required=True, help="what happened that taught this")
    s.add_argument("--keywords", default="")
    s.set_defaults(fn=cmd_lesson)

    s = sub.add_parser("guard", help="runner support: snapshot protected files before a turn, check them after")
    s.add_argument("action", choices=["snapshot", "check"])
    s.add_argument("log", nargs="?")
    s.set_defaults(fn=cmd_guard)

    s = sub.add_parser("wake", help="runner support: is a PI turn worth starting? (exit 0 wake, 3 idle)")
    s.add_argument("action", choices=["check", "mark"])
    s.add_argument("--max-idle", type=float, default=45, help="minutes without a turn before forcing one")
    s.set_defaults(fn=cmd_wake)

    s = sub.add_parser("hook")
    s.add_argument("event", choices=["pre-tool-use"])
    s.set_defaults(fn=cmd_hook)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rc = args.fn(args)
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError, KeyError) as e:
        print(f"autolab: {e}", file=sys.stderr)
        return 1
    return int(rc or 0)
