"""Task cards and the decision log.

`autolab task new` writes a task card whose `skills` are the required skills of its route
(core/skills.toml `required`), so a student cannot miss them: the handoff gate later checks that
each one was really invoked (Skill tool) in the turn logs. In the first long run the skill library
was never invoked at all: students had no Skill tool and only a menu of names.

`autolab decide` appends to state/DECISIONS.md, an append-only record of what was decided, why and
on which evidence. STATE.md is condensed every turn and used to lose that reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path

from autolab import skills
from autolab.config import Config
from autolab.util import now_iso, read_frontmatter, read_json, write_atomic, write_json

DIFFICULTY_MODEL = {"mechanical": "haiku", "standard": "sonnet", "hard": "opus"}
ROLES = ("literature", "experiment", "analysis", "figure", "writing")


def _next_task_id(cfg: Config) -> str:
    nums = [int(m.group(1)) for d in cfg.layout.tasks.glob("T*")
            if (m := re.match(r"T(\d+)", d.name))]
    counters = read_json(cfg.layout.ids, {}) or {}
    n = max([int(counters.get("task", 0))] + nums) + 1
    counters["task"] = n
    write_json(cfg.layout.ids, counters)
    return f"T{n:03d}"


def new(cfg: Config, title: str, student: str, task: str | None = None, stage: str | None = None,
        difficulty: str = "standard", model: str | None = None, extra_skills: list[str] | None = None,
        track: str = "") -> Path:
    if student not in ROLES:
        raise ValueError(f"student must be one of {ROLES}")
    if difficulty not in DIFFICULTY_MODEL:
        raise ValueError(f"difficulty must be one of {sorted(DIFFICULTY_MODEL)}")
    task = task or student
    req = skills.required(cfg, "student", stage, task)
    if task == "experiment":                  # writing experiment code: tests first
        req = req + [s for s in ("test-driven-development",) if s not in req]
    want = list(dict.fromkeys(req + (extra_skills or [])))
    avail = skills.skill_sources(cfg)
    missing = [s for s in want if s not in avail]
    if missing:
        raise ValueError(f"unknown skills {missing}")
    tid = _next_task_id(cfg)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    d = cfg.layout.tasks / f"{tid}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    model = model or DIFFICULTY_MODEL[difficulty]
    fm = "\n".join([
        "+++",
        f'id = "{tid}"',
        f'title = "{title}"',
        f'student = "student-{student}"',
        f'task = "{task}"' + (f'\nstage = "{stage}"' if stage else ""),
        f'difficulty = "{difficulty}"          # mechanical -> haiku, standard -> sonnet, hard -> opus',
        f'model = "{model}"                    # pass as the Agent `model` parameter',
        f'track = "{track}"',
        "skills = [" + ", ".join(f'"{s}"' for s in want) + "]   # invoke EACH with the Skill tool first",
        f'created = "{now_iso()}"',
        "+++",
    ])
    body = (f"\n# {tid}: {title}\n\n"
            "## Goal\n\n## Inputs (files, context pack)\n\n## Expected outputs\n\n## Budget and limits\n\n"
            "## How to report\nWrite `handoff.md` in this folder from `experiments/_TEMPLATE/handoff.md`; "
            "list every skill you invoked in `skills_used`.\n")
    write_atomic(d / "task.md", fm + "\n" + body)
    return d / "task.md"


def check_skills_used(cfg: Config, card: Path) -> tuple[list[str], list[str]]:
    """(problems, warnings) for a handoff card next to a task card: every skill the task card
    requires must be listed in `skills_used` and must appear as a Skill-tool call in a turn log
    written since the task card was created."""
    task_card = card.parent / "task.md"
    if not task_card.is_file():
        return [], []
    tmeta, _ = read_frontmatter(task_card)
    need = list(tmeta.get("skills", []))
    if not need:
        return [], []
    hmeta, _ = read_frontmatter(card)
    used = set(hmeta.get("skills_used", []))
    problems = [f"required skill '{s}' not in skills_used (invoke it with the Skill tool, then list it)"
                for s in need if s not in used]
    warnings: list[str] = []
    since = task_card.stat().st_mtime - 60
    if skills.have_logs(cfg):
        tid = str(tmeta.get("id", card.parent.name.split("-")[0]))
        seen = skills.invoked_by(cfg, since, {tid})       # only this task's student counts
        problems += [f"skill '{s}' is listed in skills_used but this task's student never invoked it (no Skill "
                     f"call under an Agent dispatch that names {tid}; put the task ID in the Agent description)"
                     for s in need if s in used and s not in seen]
    else:
        warnings.append("no turn logs: skill invocations could not be verified")
    problems += skills.missing_artifacts(cfg, [s for s in need if s in used], since)
    return problems, warnings


def decide(cfg: Config, text: str, why: str, evidence: str = "", track: str = "", by: str = "PI",
           amends: str = "") -> str:
    """`amends`: the D-id this record corrects or extends; near-duplicate records used to pile up
    when a decision was refined (RETRO-133)."""
    from autolab.util import next_id
    p = cfg.layout.state / "DECISIONS.md"
    amends = amends.strip()
    if amends:
        if not re.fullmatch(r"D\d+", amends):
            raise ValueError("--amends takes a decision id such as D172")
        if not (p.is_file() and re.search(rf"(?m)^## {amends} ", p.read_text(encoding="utf-8"))):
            raise KeyError(f"no decision {amends} in state/DECISIONS.md")
        text = f"[amends {amends}] {text}"
    did = next_id(cfg.layout.ids, "decision")
    head = "" if p.is_file() else ("# Decisions (append-only; written by `autolab decide`)\n\n"
                                   "Newest last. Each entry: what was decided, why, on which evidence.\n\n")
    entry = (f"## {did} — {now_iso()}{f' — track {track}' if track else ''} — {by}\n"
             f"- **Decision:** {text.strip()}\n- **Why:** {why.strip()}\n"
             + (f"- **Evidence:** {evidence.strip()}\n" if evidence.strip() else "") + "\n")
    with open(p, "a", encoding="utf-8") as f:
        f.write(head + entry)
    return did


LESSON_SCOPE = ("domain", "data", "resolution", "resources", "method", "regime")
CONFIDENCE = ("low", "medium", "high")


def add_lesson(cfg: Config, lesson: str, origin: str, keywords: list[str], when: dict[str, str] | None = None,
               evidence: list[str] | None = None, confidence: str = "low", recheck: str = "") -> str:
    """A rule learned from an experiment or a retrospective, with the conditions under which it was
    observed. Context packs surface it before related work together with its scope and confidence,
    so "X does not help" learned at 256^3 on synthetic data is not applied to a real 3D case.
    Unscoped lessons used to read as universal rules."""
    from autolab.util import append_jsonl, next_id, sha256_tree
    when = {k.strip(): v.strip() for k, v in (when or {}).items() if v.strip()}
    if not when:
        raise ValueError(f"give the conditions it was learned under: --when key=value (keys {', '.join(LESSON_SCOPE)})")
    bad = [k for k in when if k not in LESSON_SCOPE]
    if bad:
        raise ValueError(f"unknown condition keys {bad}; use {LESSON_SCOPE}")
    if confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
    if not re.match(r"(E\d+|RETRO-\d+|C\d+|F\d+)", origin.strip()):
        raise ValueError("origin must name the source: an experiment (E012), claim/finding or RETRO-<tick>")
    if not recheck.strip():
        raise ValueError("say when to recheck it (--recheck): which change of conditions would make it doubtful")
    fps = {}
    for e in evidence or []:
        p = cfg.layout.root / e
        if not p.exists():
            raise FileNotFoundError(f"evidence {e} not found")
        fps[e] = sha256_tree(p)
    lid = next_id(cfg.layout.ids, "lesson")
    append_jsonl(cfg.layout.experience / "lessons.jsonl",
                 {"id": lid, "lesson": lesson.strip(), "origin": origin.strip(), "when": when,
                  "evidence": fps, "confidence": confidence, "recheck": recheck.strip(),
                  "keywords": [k.strip() for k in keywords if k.strip()], "at": now_iso()})
    return lid


def lesson_event(cfg: Config, lid: str, kind: str, source: str, why: str, confidence: str | None = None) -> None:
    """Record a supporting or contradicting example (or retire the lesson). Events are appended;
    `lessons()` folds them, so the history stays visible."""
    from autolab.util import append_jsonl
    if kind not in ("support", "counter", "retire"):
        raise ValueError("kind must be support, counter or retire")
    if lid not in {l["id"] for l in lessons(cfg)}:
        raise KeyError(f"no lesson {lid}")
    if confidence and confidence not in CONFIDENCE:
        raise ValueError(f"confidence must be one of {CONFIDENCE}")
    if not why.strip():
        raise ValueError("give the reason")
    append_jsonl(cfg.layout.experience / "lessons.jsonl",
                 {"event": kind, "lesson_id": lid, "source": source.strip(), "why": why.strip(),
                  "confidence": confidence, "at": now_iso()})


def lessons(cfg: Config, include_retired: bool = False) -> list[dict]:
    from autolab.util import read_jsonl
    out: dict[str, dict] = {}
    for r in read_jsonl(cfg.layout.experience / "lessons.jsonl"):
        if "event" not in r:
            out[r["id"]] = {**r, "support": [], "counter": [], "retired": False,
                            "when": r.get("when", {}), "confidence": r.get("confidence", "unscoped")}
            continue
        l = out.get(r.get("lesson_id", ""))
        if not l:
            continue
        if r["event"] == "retire":
            l["retired"] = True
        else:
            l[r["event"]].append(f"{r['source']}: {r['why']}")
        if r.get("confidence"):
            l["confidence"] = r["confidence"]
    return [l for l in out.values() if include_retired or not l["retired"]]


def lesson_label(l: dict) -> str:
    scope = ", ".join(f"{k}={v}" for k, v in l.get("when", {}).items()) or "UNSCOPED (conditions unknown)"
    tail = f" [confidence {l.get('confidence')}; {len(l['support'])} support, {len(l['counter'])} counter"
    tail += f"; recheck if {l['recheck']}]" if l.get("recheck") else "]"
    return f"{l['lesson'][:110]} — only under: {scope}{tail}"


def retro_due(cfg: Config, tick: int) -> int | None:
    """Tick of the last retrospective if one is due now (every `gates.retro_every` ticks), else None."""
    every = int(cfg["gates"].get("retro_every", 20) or 0)
    if not every:
        return None
    done = [int(m.group(1)) for p in cfg.layout.experience.glob("RETRO-*.md")
            if (m := re.match(r"RETRO-(\d+)", p.stem))]
    last = max(done, default=0)
    return last if tick - last >= every else None
