"""What the lab must do now: situations computed from its records, with no model call.

Skills offered as options stayed unused: in first-test 51 of 67 were never invoked, and the six most
used were all written into the task card. The ideation skills in particular had no trigger: "when a
direction fails, brainstorm" was a sentence in a protocol, and one call in 171 turns was the result.
A rule here detects the situation, names the action and closes by itself when the action's product
exists. Items marked `blocks` stop new plans (`knowledge.check_plan`). `autolab due waive` records a
reasoned exception, so a rule can never deadlock the lab.
"""

from __future__ import annotations

import re
from datetime import datetime

from autolab.config import Config
from autolab.util import now_iso, read_json, write_json

IDEATION_SKILLS = ("brainstorming-research-ideas", "hypothesis-generation")


def _t(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso or "").timestamp()
    except ValueError:
        return 0.0


def _file(cfg: Config):
    return cfg.layout.state / "due.json"


def _reviews_of(cfg: Config, task: str) -> list[float]:
    return sorted(_t(r.get("created")) for r in (read_json(p) or {} for p in cfg.layout.reviews.glob("R*.json"))
                  if r.get("task") == task)


def _last_round(cfg: Config) -> float:
    """When the last complete ideation round ended: a written record with at least three candidates,
    an `idea_review` of it, and the ideation skills invoked around it."""
    from autolab import skills
    reviews = _reviews_of(cfg, "idea_review")
    logs = skills.have_logs(cfg)
    done = 0.0
    for f in (cfg.layout.state / "ideation").glob("*.md"):
        t = f.stat().st_mtime
        if len(set(re.findall(r"\bH\d+[a-z]?\b", f.read_text(encoding="utf-8", errors="ignore")))) >= 3 \
                and any(r + 1 >= t for r in reviews) \
                and (not logs or all(s in skills.invoked_since(cfg, t - 6 * 3600) for s in IDEATION_SKILLS)):
            done = max(done, t)
    return done


def _ideation(cfg: Config) -> list[dict]:
    from autolab import research
    outs = []
    for e in cfg.layout.experiments.glob("E*"):
        o = read_json(e / "outcome.json")
        if o and o.get("engineering") == "valid":
            outs.append(o)
    outs.sort(key=lambda o: _t(o.get("at")))
    last = _last_round(cfg)
    streak = []
    for o in outs:
        if _t(o.get("at")) <= last:
            continue
        stalled = o.get("value") in ("refine", "stop", "pivot") or o.get("hypothesis") == "refutes"
        streak = streak + [o] if stalled else []
    do = (f"Invoke {' and '.join(f'`{s}`' for s in IDEATION_SKILLS)}. Write `state/ideation/<date>-<topic>.md` with at "
          "least three mechanistically different candidates (`autolab candidate new H.. --origin pi ...`; keep the "
          "rejected ones with the reason). Then `autolab review run idea_review state/ideation/<file>` and add the "
          "reviewer's own proposals with `--origin reviewer`.")
    if len(streak) >= int(cfg["triggers"]["refine_streak"]):
        names = ", ".join(o["experiment"] for o in streak)
        return [{"key": f"ideation:{streak[0]['experiment']}", "blocks": True, "skills": list(IDEATION_SKILLS), "do": do,
                 "title": f"{len(streak)} results in a row did not move a line forward ({names}): stop refining, look for another mechanism"}]
    if not outs and not last and len(research.list_candidates(cfg)) < 3:
        return [{"key": "ideation:start", "blocks": True, "skills": list(IDEATION_SKILLS), "do": do,
                 "title": "fewer than three candidates and no ideation round yet: a first plan should choose between alternatives"}]
    return []


def _value(cfg: Config) -> list[dict]:
    """Are the accepted findings adding up to something the field would use?"""
    reviews = _reviews_of(cfg, "value_grade")
    last = reviews[-1] if reviews else 0.0
    new = sorted((rec.get("accepted", ""), d.name) for d in cfg.layout.findings.glob("F*")
                 for rec in [read_json(d / "finding.json") or {}] if _t(rec.get("accepted")) > last)
    if len(new) < int(cfg["triggers"]["value_every"]):
        return []
    return [{"key": f"value:{new[0][1]}", "blocks": True, "skills": [],
             "title": f"{len(new)} findings since the last value review ({', '.join(n for _a, n in new)})",
             "do": "`autolab review run value_grade` (reads GOAL, TASTE, the findings and state/CONTRIBUTIONS.md). "
                   "Act on its answer: what is the one new thing, and which venue would take it."}]


def _literature(cfg: Config, tick: int) -> list[dict]:
    from autolab.knowledge import lit_records
    recs = lit_records(cfg)
    if not recs:
        return []
    items = []
    last = int((read_json(cfg.layout.literature / "watch.json", {}) or {}).get("tick", 0))
    if tick - last >= int(cfg["triggers"]["lit_watch_every"]):
        items.append({"key": f"lit_watch:{last}", "blocks": False, "skills": [],
                      "title": f"no literature watch for {tick - last} ticks",
                      "do": "`autolab lit watch`: new papers citing the tier A records. Register what matters."})
    todo = [r["id"] for r in recs if r.get("access") in ("abstract_only", "oa", "human_needed") and not r.get("fetch_tried")]
    if todo:
        items.append({"key": "lit_fetch", "blocks": False, "skills": [],
                      "title": f"{len(todo)} papers without full text were never tried",
                      "do": "`autolab lit fetch --id all`: downloads legal open copies and extracts their text."})
    return items


def _reviewer(cfg: Config) -> list[dict]:
    """Reviews paused for lack of quota, and reviews still waiting for a second vendor."""
    from autolab import reviewer
    paused = reviewer.limit_status(cfg)
    if paused:
        when = datetime.fromtimestamp(paused["until"]).strftime("%m-%d %H:%M")
        return [{"key": "reviewer_limit", "blocks": False, "skills": [],
                 "title": f"the reviewer is out of quota until {when}",
                 "do": "Do not run reviews until then; claims and audits wait for theirs. The human has been told."}]
    if cfg["reviewer"]["independence"] == "same_vendor":
        return []
    recs = sorted((r for r in (read_json(p) or {} for p in cfg.layout.reviews.glob("R*.json")) if r.get("subject")),
                  key=lambda r: r["id"])
    latest = {(r["task"], r["subject"].get("audit") or r["subject"].get("claim")): r for r in recs}
    items = []
    for r in latest.values():
        if r.get("independence") == "same_vendor":
            s = r["subject"]
            target = f"--audit {s['audit']}" if s.get("audit") else f"--claim {s.get('claim')}"
            items.append({"key": f"cross_review:{r['id']}", "blocks": False, "skills": [],
                          "title": f"{r['id']} ({r['task']}, {s.get('audit') or s.get('claim')}) had no reviewer from another vendor",
                          "do": f"`autolab review run {r['task']} {target}`"})
    return items


def _defects(cfg: Config) -> list[dict]:
    """Findings a known defect touches and nobody has answered for."""
    from autolab import defects
    return [{"key": f"defect:{r['id']}", "blocks": True, "skills": [],
             "title": f"{r['id']} ({r['what'][:90]}) still touches {', '.join(r['open'])}",
             "do": f"For each finding: `autolab defect resolve {r['id']} <F-id> --how remeasured|rescoped|not_affected "
                   f"--why ...`. A plan that re-measures declares `fixes = [\"{r['id']}\"]` and passes."}
            for r in defects.status(cfg) if r["open"]]


def compute(cfg: Config, tick: int | None = None) -> list[dict]:
    if not cfg["triggers"].get("enabled", True):
        return []
    if tick is None:
        tick = int((read_json(cfg.layout.state / "tick.json", {}) or {}).get("n", 0))
    waived = (read_json(_file(cfg), {}) or {}).get("waived", {})
    items = _reviewer(cfg) + _defects(cfg) + _ideation(cfg) + _value(cfg) + _literature(cfg, tick)
    return [i for i in items if i["key"] not in waived]


def plan_blockers(cfg: Config, exempt: list[str] | None = None) -> list[str]:
    """`exempt`: items this plan exists to settle: re-measuring a defect takes a plan of its own."""
    return [f"due item {i['key']} comes first: {i['title']}. {i['do']} (a reasoned exception: `autolab due waive "
            f"{i['key']} --why ...`)" for i in compute(cfg) if i["blocks"] and i["key"] not in (exempt or [])]


def waive(cfg: Config, key: str, why: str) -> dict:
    if not why.strip():
        raise ValueError("give the reason")
    if key not in {i["key"] for i in compute(cfg)}:
        raise KeyError(f"no open due item {key}")
    from autolab.tasks import decide
    data = read_json(_file(cfg), {}) or {}
    rec = {"why": why.strip(), "at": now_iso(), "decision": decide(cfg, f"waived due item {key}", why)}
    data.setdefault("waived", {})[key] = rec
    write_json(_file(cfg), data)
    return rec


def digest(cfg: Config, tick: int) -> list[str]:
    items = compute(cfg, tick)
    if not items:
        return []
    lines = ["## Must do now", "These situations hold in the lab's records. `blocks` items stop new plans until "
             "done or waived with a reason (`autolab due waive <key> --why ...`)."]
    for i in items:
        lines.append(f"- **{i['key']}**{' [blocks]' if i['blocks'] else ''} {i['title']}. {i['do']}")
    return lines + [""]


