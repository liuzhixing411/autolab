"""Command line: every interface the agents and the human use goes through here."""

from __future__ import annotations

import argparse
import json
import subprocess
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


def cmd_weixin(a):
    from autolab import weixin
    weixin.run(_cfg(a), a.action)


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
    elif a.action == "notify":
        human.notify(cfg, a.text)


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
    cfg = _cfg(a)
    if a.action == "add":
        when = {}
        for w in a.when or []:
            k, sep, v = w.partition("=")
            if not sep:
                raise ValueError(f"--when needs key=value, got '{w}'")
            when[k] = v
        print(tasks.add_lesson(cfg, a.text, a.origin or "", (a.keywords or "").split(","), when,
                               a.evidence or [], a.confidence, a.recheck or ""))
    elif a.action == "list":
        for l in tasks.lessons(cfg, include_retired=a.all):
            print(f"{l['id']}{' (retired)' if l['retired'] else ''}: {tasks.lesson_label(l)}")
    else:                                        # support | counter | retire
        tasks.lesson_event(cfg, a.text, a.action, a.origin or "", a.why or "", a.set_confidence)
        print(f"{a.text}: {a.action} recorded")


def cmd_candidate(a):
    from autolab import research
    cfg = _cfg(a)
    if a.action == "new":
        fields = {k: getattr(a, k) or "" for k in research.CARD_FIELDS if k != "statement"}
        print(research.new_candidate(cfg, a.id, a.statement or "", **fields))
    elif a.action == "check":
        probs = research.card_problems(cfg, a.id)
        _out({"ok": not probs, "problems": probs})
        return 0 if not probs else 1
    elif a.action == "set":
        research.set_candidate(cfg, a.id, a.status, a.why or "")
        print(f"{a.id} -> {a.status}")
    else:
        for c in research.list_candidates(cfg):
            print(f"{c['id']} [{c['status']}{', priority ' + c['priority'] if c['priority'] else ''}] "
                  f"{c['statement'][:90]} | test: {c['discriminating_experiment'][:70]} | cost: {c['cost_estimate']}")


def cmd_outcome(a):
    from autolab import research
    rec = research.record_outcome(_cfg(a), a.eid, a.engineering, a.hypothesis, a.value, a.why,
                                  [e for e in (a.evidence or "").split(",") if e.strip()], a.usd)
    _out(rec)


def cmd_tree(a):
    from autolab import research
    print(research.tree(_cfg(a)), end="")


def cmd_report(a):
    from autolab import report
    m = report.write(_cfg(a))
    print(report.render(m) if not a.json else "", end="")
    if a.json:
        _out(m)


def cmd_guard(a):
    from autolab import guard
    cfg = _cfg(a)
    if a.action == "snapshot":
        _out(guard.snapshot(cfg))
        return 0
    from autolab.util import now_iso
    res = guard.check(cfg, Path(a.log) if a.log else None)
    if not res.get("checked"):
        print("guard: no valid snapshot; refusing to continue", file=sys.stderr)
        return 5
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

    s = sub.add_parser("weixin", help="human-only Weixin login/listener; apply is runner-only")
    s.add_argument("action", choices=["login", "listen", "apply"])
    s.set_defaults(fn=cmd_weixin)

    s = sub.add_parser("skills")
    s.add_argument("action", choices=["resolve", "mount", "check", "list"])
    s.add_argument("--role", default="pi")
    s.add_argument("--stage")
    s.add_argument("--task")
    s.set_defaults(fn=cmd_skills)

    s = sub.add_parser("human")
    s.add_argument("action", choices=["check", "ack", "request", "uploads", "notify"])
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

    s = sub.add_parser("lesson", help="scoped lessons: add | list | support/counter/retire <LS-id> (surface in context packs)")
    s.add_argument("action", choices=["add", "list", "support", "counter", "retire"])
    s.add_argument("text", nargs="?", default="", help="add: the lesson; support/counter/retire: the LS-id")
    s.add_argument("--origin", help="add: source (E012, C004, F003 or RETRO-40); support/counter: the experiment")
    s.add_argument("--when", action="append", help="add: condition key=value (domain, data, resolution, resources, "
                                                    "method, regime); repeat for several")
    s.add_argument("--evidence", action="append", help="add: file or folder it rests on (fingerprinted)")
    s.add_argument("--confidence", default="low", choices=["low", "medium", "high"])
    s.add_argument("--set-confidence", choices=["low", "medium", "high"], help="support/counter: new confidence")
    s.add_argument("--recheck", help="add: which change of conditions would make it doubtful")
    s.add_argument("--why", help="support/counter/retire: reason")
    s.add_argument("--keywords", default="")
    s.add_argument("--all", action="store_true", help="list: include retired lessons")
    s.set_defaults(fn=cmd_lesson)

    from autolab.research import CARD_FIELDS, ENGINEERING, HYPOTHESIS, STATUS, VALUE
    s = sub.add_parser("candidate", help="candidate hypotheses: new | list | check | set (state/candidates/<H>.md)")
    s.add_argument("action", choices=["new", "list", "check", "set"])
    s.add_argument("id", nargs="?", help="hypothesis id, e.g. H14")
    for k in CARD_FIELDS:
        s.add_argument(f"--{k.replace('_', '-')}", dest=k)
    s.add_argument("--status", choices=list(STATUS))
    s.add_argument("--why")
    s.set_defaults(fn=cmd_candidate)

    s = sub.add_parser("outcome", help="close an experiment: engineering / hypothesis / value (pivot, stop -> human)")
    s.add_argument("eid")
    s.add_argument("--engineering", required=True, choices=list(ENGINEERING))
    s.add_argument("--hypothesis", required=True, choices=list(HYPOTHESIS))
    s.add_argument("--value", required=True, choices=list(VALUE))
    s.add_argument("--why", required=True)
    s.add_argument("--evidence", help="comma-separated files")
    s.add_argument("--usd", type=float, help="API cost attributable to this experiment, if known")
    s.set_defaults(fn=cmd_outcome)

    sub.add_parser("tree", help="write state/TREE.md: experiments as a search tree with outcomes").set_defaults(fn=cmd_tree)

    s = sub.add_parser("report", help="metrics: send-back rate, valid tests, repeated failures, memory use, "
                                      "human corrections, cost per credible result (state/METRICS.md)")
    s.add_argument("what", choices=["metrics"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_report)

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
        from autolab import guard
        track = args.cmd not in ("init", "hook", "guard", "weixin")
        cfg = _cfg(args) if track else None
        before = guard.protected(cfg) if cfg else {}
        try:
            rc = args.fn(args)
        finally:
            if cfg:
                guard.authorize_changes(cfg, before)
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError, KeyError) as e:
        print(f"autolab: {e}", file=sys.stderr)
        return 1
    return int(rc or 0)
