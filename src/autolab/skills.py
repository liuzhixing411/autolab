"""Skill registry: which role loads which skill at which stage, and mounting into a project.

core/skills.toml          general-science routes (field-neutral)
domains/<d>/domain.toml   [[route]] additions + local domain skills in domains/<d>/skills/
core/overlays/<s>.md      field-neutral adaptation appended to a vendor SKILL.md
domains/<d>/overlays/<s>.md   field-specific adaptation appended after the core overlay

Vendor skill folders are never edited; overlays are appended when mounting.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from autolab.config import Config, load_toml
from autolab.util import now_iso, sha256_tree, write_json

OVERLAY_MARK = "\n\n<!-- autolab overlay: {origin} -->\n"
SHARED_DIRS = ("shared-references",)


def registry(cfg: Config) -> dict:
    return load_toml(cfg.fw / "core" / "skills.toml")


def skill_sources(cfg: Config) -> dict[str, Path]:
    """name -> folder, for every skill available to this project (domain wins on clash)."""
    out: dict[str, Path] = {}
    for base in (cfg.fw / "core" / "skills", cfg.domain_dir / "skills"):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d / "SKILL.md").is_file():
                    out[d.name] = d
    return out


def _routes(cfg: Config) -> list[dict]:
    return list(registry(cfg).get("route", [])) + list(cfg.domain.get("route", []))


def _matches(r: dict, role: str, stage: str | None, task: str | None) -> bool:
    """A route with a task is found by that task; its stage only narrows the match. (A `section`
    task without `--stage writing` used to miss its route and got no skills at all.)"""
    if r.get("role") != role:
        return False
    if "task" in r:
        return r["task"] == task and ("stage" not in r or stage is None or r["stage"] == stage)
    return "stage" not in r or r["stage"] == stage


def resolve(cfg: Config, role: str, stage: str | None = None, task: str | None = None) -> list[str]:
    """Skills for a role at a stage/task: role `always` + matching routes (core then domain)."""
    reg = registry(cfg)
    names: list[str] = list(reg.get("always", {}).get(role, []))
    names += cfg.domain.get("always", {}).get(role, [])
    for r in _routes(cfg):
        if _matches(r, role, stage, task):
            names += r.get("skills", [])
    return list(dict.fromkeys(names))


def required(cfg: Config, role: str, stage: str | None = None, task: str | None = None) -> list[str]:
    """Skills that MUST be invoked (Skill tool) for this role/stage/task: `[always_required]`
    plus each matching route's `required` list. `resolve` stays the wider menu."""
    reg = registry(cfg)
    names = list(reg.get("always_required", {}).get(role, []))
    names += cfg.domain.get("always_required", {}).get(role, [])
    for r in _routes(cfg):
        if _matches(r, role, stage, task):
            names += r.get("required", [])
    return list(dict.fromkeys(names))


def situations(cfg: Config, role: str, stage: str | None = None, task: str | None = None) -> dict[str, str]:
    """skill -> the situation that calls for it, from the routes' `when` tables."""
    out: dict[str, str] = {}
    for r in _routes(cfg):
        if _matches(r, role, stage, task):
            for name, cond in r.get("when", {}).items():
                out.setdefault(name, cond)
    return out


def student_tasks(cfg: Config) -> list[str]:
    """Route tasks a student card may name. An unknown task used to pass silently and the card got
    only the base skill (first-test: 28 cards named `writing`, `plan`, `analyze`, `draft`, ...)."""
    return sorted({r["task"] for r in _routes(cfg) if r.get("role") == "student" and "task" in r})


def activation(cfg: Config) -> dict[str, str]:
    """How each available skill comes to be used. `gate`: a card, gate or due rule forces it.
    `situation`: named with its condition. `reviewer`: handed to the reviewer. `human`: for the
    human. `library`: reachable by name only, a dependency of other skills or spare."""
    from autolab.due import IDEATION_SKILLS
    reg = registry(cfg)
    gated = set(IDEATION_SKILLS)
    for lst in list(reg.get("always_required", {}).values()) + list(cfg.domain.get("always_required", {}).values()):
        gated.update(lst)
    named, reviewer, human = set(), set(), set()
    for r in _routes(cfg):
        gated.update(r.get("required", []))
        named.update(r.get("when", {}))
        if r.get("role") == "reviewer":
            reviewer.update(r.get("skills", []))
        elif r.get("role") == "human":
            human.update(r.get("skills", []))
    for lst in list(reg.get("always", {}).values()) + list(cfg.domain.get("always", {}).values()):
        named.update(lst)
    out = {}
    for n in skill_sources(cfg):
        out[n] = ("gate" if n in gated else "situation" if n in named else "reviewer" if n in reviewer
                  else "human" if n in human else "library")
    return out


def listing(cfg: Config) -> dict[str, str]:
    """`skillOverrides` for .claude/settings.json. Claude Code keeps every skill name but drops
    descriptions over a budget of 1 % of the context window, least-invoked skills first, so an
    unused skill loses the text that could have got it used. Skills the agents have no direct use
    for stay callable by name (skills depend on each other; the reviewer reads its files) and leave
    the budget to the ones with a job."""
    return {n: "name-only" for n, how in activation(cfg).items() if how in ("library", "human", "reviewer")}


def invoked_since(cfg: Config, since: float = 0.0) -> dict[str, int]:
    """Skill-tool invocations (PI and students) in the turn logs written after `since` (epoch s).
    Empty when there are no logs (interactive use), so callers can tell 'unknown' from 'not used'."""
    import json
    out: dict[str, int] = {}
    logs = cfg.layout.state / "pi_logs"
    for log in sorted(logs.glob("*.jsonl")) if logs.is_dir() else []:
        if log.stat().st_mtime < since:
            continue
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            if '"Skill"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Skill":
                    name = str((block.get("input") or {}).get("skill", "")).split(":")[-1]
                    if name:
                        out[name] = out.get(name, 0) + 1
    return out


def invoked_by(cfg: Config, since: float, ids: set[str], include_pi: bool = False) -> dict[str, int]:
    """Skill calls attributed to the students whose dispatch mentions one of `ids` (task or
    experiment IDs in the Agent call's description or prompt), plus the PI's own calls if
    `include_pi`. Subagent events carry `parent_tool_use_id` = the dispatching Agent call, so a
    skill one student invoked no longer counts for another student's task."""
    import json
    pat = re.compile(r"\b(" + "|".join(re.escape(i) for i in sorted(ids)) + r")\b") if ids else None
    agents: dict[str, bool] = {}
    calls: list[tuple[str | None, str]] = []
    logs = cfg.layout.state / "pi_logs"
    for log in sorted(logs.glob("*.jsonl")) if logs.is_dir() else []:
        if log.stat().st_mtime < since:
            continue
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            if '"Skill"' not in line and '"Agent"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            for block in (ev.get("message") or {}).get("content") or []:
                if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                    continue
                inp = block.get("input") or {}
                if block.get("name") == "Agent":
                    text = f"{inp.get('description', '')} {str(inp.get('prompt', ''))[:4000]}"
                    agents[block.get("id")] = bool(pat and pat.search(text))
                elif block.get("name") == "Skill":
                    name = str(inp.get("skill", "")).split(":")[-1]
                    if name:
                        calls.append((ev.get("parent_tool_use_id"), name))
    out: dict[str, int] = {}
    for parent, name in calls:
        if (parent is None and include_pi) or (parent is not None and agents.get(parent)):
            out[name] = out.get(name, 0) + 1
    return out


# files a skill must leave behind when a task card requires it (checked by the handoff gate)
SKILL_ARTIFACTS = {"ara-rigor-reviewer": ("claims/*/rigor_review.json", "paper/rigor_review.json"),
                   "literature-review": ("literature/search_log.md",)}
# search-log kinds a skill must leave behind: invoking `research-lit` and then searching the web by
# hand is what left first-test without Semantic Scholar and without citation links
SKILL_SEARCHES = {"research-lit": ("search", "snowball"), "novelty-audit": ("search", "snowball")}


def missing_artifacts(cfg: Config, names: list[str], since: float) -> list[str]:
    out = []
    for n in names:
        pats = SKILL_ARTIFACTS.get(n)
        if pats:
            hits = [p for pat in pats for p in cfg.layout.root.glob(pat) if p.stat().st_mtime >= since]
            if not hits:
                out.append(f"skill '{n}' should leave {' or '.join(pats)} (written after the task card); none found")
        kinds = SKILL_SEARCHES.get(n)
        if kinds:
            from datetime import datetime
            from autolab.scholar import search_log
            done = {r["kind"] for r in search_log(cfg) if datetime.fromisoformat(r["at"]).timestamp() >= since}
            lost = [k for k in kinds if k not in done]
            if lost:
                out.append(f"skill '{n}' needs `autolab lit {' and `autolab lit '.join(lost)}` since the task card was written")
    return out


def have_logs(cfg: Config) -> bool:
    logs = cfg.layout.state / "pi_logs"
    return logs.is_dir() and any(logs.glob("*.jsonl"))


def all_routed(cfg: Config) -> set[str]:
    reg = registry(cfg)
    names = set()
    for key in ("always", "always_required"):
        for lst in list(reg.get(key, {}).values()) + list(cfg.domain.get(key, {}).values()):
            names.update(lst)
    for r in _routes(cfg):
        names.update(r.get("skills", []))
        names.update(r.get("required", []))
    return names


def check(cfg: Config) -> list[str]:
    """Problems: routed skills that do not exist, overlays for skills that do not exist, a `when`
    or `required` name missing from its route's `skills`."""
    problems = []
    avail = skill_sources(cfg)
    for n in sorted(all_routed(cfg)):
        if n not in avail:
            problems.append(f"routed skill '{n}' is not installed (core/skills or domain skills)")
    for r in _routes(cfg):
        for n in list(r.get("when", {})) + r.get("required", []):
            if n not in r.get("skills", []):
                problems.append(f"route {r.get('role')}/{r.get('stage', '')}/{r.get('task', '')}: '{n}' is not in its `skills`")
    for odir in (cfg.fw / "core" / "overlays", cfg.domain_dir / "overlays"):
        if odir.is_dir():
            for f in odir.glob("*.md"):
                if not f.stem.startswith("_") and f.stem not in avail:
                    problems.append(f"overlay {f} targets unknown skill '{f.stem}'")
    return problems


_MCP = re.compile(r"mcp__[A-Za-z0-9-]+(?:__[A-Za-z0-9_-]+)?")
_SCRIPT = re.compile(r"\b[a-z_]+(?:_fetch|_search)\.py\b|\bverify_papers\.py\b|\bsave_trace\.sh\b|\bresearch_wiki\.py\b")
_ML_ONLY = re.compile(r"\b(?:ICLR|NeurIPS|ICML|CVPR|wandb|W&B|training run)\b")


def doctor(cfg: Config) -> list[dict]:
    """What keeps a skill from working in this lab, for every skill. Half of the library calls a
    Codex tool that does not exist here; `novelty-check` searched ML venues for a CFD project."""
    tools = cfg.fw / "third_party" / "aris" / "tools"
    how = activation(cfg)
    rows = []
    for name, src in sorted(skill_sources(cfg).items()):
        text = (src / "SKILL.md").read_text(encoding="utf-8", errors="ignore")
        mcp = sorted(set(_MCP.findall(text)))
        rows.append({"skill": name, "use": how.get(name, "library"),
                     "codex_step": any(m.startswith("mcp__codex__") for m in mcp),
                     "other_mcp": [m for m in mcp if not m.startswith("mcp__codex__")],
                     "missing_scripts": sorted(s for s in set(_SCRIPT.findall(text)) if not (tools / s).is_file()),
                     "ml_terms": len(_ML_ONLY.findall(text))})
    return rows


def report(cfg: Config) -> list[dict]:
    """Use of each skill in this project's turn logs, next to what it is for. A `gate` skill that is
    never invoked means its trigger is broken; a `situation` skill that is never invoked is a
    question for the retrospective."""
    seen = invoked_since(cfg)
    return [{"skill": n, "use": how, "calls": seen.get(n, 0)} for n, how in sorted(activation(cfg).items())]


def mount(cfg: Config, target: Path | None = None) -> dict:
    """Copy every available skill into <project>/.claude/skills with overlays appended.

    All installed skills are mounted, not just routed ones, because ARIS pipeline
    skills invoke their dependencies (e.g. training-check) by name.
    """
    target = target or cfg.layout.skills_mount
    target.mkdir(parents=True, exist_ok=True)
    avail = skill_sources(cfg)
    wanted = sorted(avail)
    manifest = {"mounted_at": now_iso(), "domain": cfg.domain_name, "skills": {}}

    # clear previous autolab-managed mounts only
    old = target / "MOUNT.json"
    if old.is_file():
        import json
        for name in json.loads(old.read_text(encoding="utf-8")).get("skills", {}):
            p = target / name
            if p.is_dir():
                shutil.rmtree(p)
        for name in SHARED_DIRS:
            if (target / name).is_dir():
                shutil.rmtree(target / name)

    for name in wanted:
        src = avail[name]
        dst = target / name
        shutil.copytree(src, dst)
        applied = []
        for origin, odir in (("core", cfg.fw / "core" / "overlays"),
                             (f"domain:{cfg.domain_name}", cfg.domain_dir / "overlays")):
            ov = odir / f"{name}.md"
            if ov.is_file():
                with (dst / "SKILL.md").open("a", encoding="utf-8", newline="\n") as f:
                    f.write(OVERLAY_MARK.format(origin=origin))
                    f.write(ov.read_text(encoding="utf-8"))
                applied.append(origin)
        codex = cfg.fw / "core" / "overlays" / "_codex-mcp.md"
        if codex.is_file() and "mcp__codex__" in (dst / "SKILL.md").read_text(encoding="utf-8", errors="ignore"):
            with (dst / "SKILL.md").open("a", encoding="utf-8", newline="\n") as f:
                f.write(OVERLAY_MARK.format(origin="core:codex"))
                f.write(codex.read_text(encoding="utf-8"))
            applied.append("core:codex")
        manifest["skills"][name] = {"source": str(src), "overlays": applied,
                                    "hash": sha256_tree(dst)}

    # ARIS skills reference ../shared-references/...
    for name in SHARED_DIRS:
        src = cfg.fw / "core" / "skills" / name
        if src.is_dir():
            shutil.copytree(src, target / name)

    write_json(target / "MOUNT.json", manifest)
    return manifest
