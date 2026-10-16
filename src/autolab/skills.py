"""Skill registry: which role loads which skill at which stage, and mounting into a project.

core/skills.toml          general-science routes (field-neutral)
domains/<d>/domain.toml   [[route]] additions + local domain skills in domains/<d>/skills/
core/overlays/<s>.md      field-neutral adaptation appended to a vendor SKILL.md
domains/<d>/overlays/<s>.md   field-specific adaptation appended after the core overlay

Vendor skill folders are never edited; overlays are appended when mounting.
"""

from __future__ import annotations

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


def resolve(cfg: Config, role: str, stage: str | None = None, task: str | None = None) -> list[str]:
    """Skills for a role at a stage/task: role `always` + matching routes (core then domain)."""
    reg = registry(cfg)
    names: list[str] = list(reg.get("always", {}).get(role, []))
    names += cfg.domain.get("always", {}).get(role, [])
    for r in _routes(cfg):
        if r.get("role") != role:
            continue
        if "stage" in r and r["stage"] != stage:
            continue
        if "task" in r and r["task"] != task:
            continue
        names += r.get("skills", [])
    seen, out = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def all_routed(cfg: Config) -> set[str]:
    reg = registry(cfg)
    names = set()
    for lst in list(reg.get("always", {}).values()) + list(cfg.domain.get("always", {}).values()):
        names.update(lst)
    for r in _routes(cfg):
        names.update(r.get("skills", []))
    return names


def check(cfg: Config) -> list[str]:
    """Problems: routed skills that do not exist, overlays for skills that do not exist."""
    problems = []
    avail = skill_sources(cfg)
    for n in sorted(all_routed(cfg)):
        if n not in avail:
            problems.append(f"routed skill '{n}' is not installed (core/skills or domain skills)")
    for odir in (cfg.fw / "core" / "overlays", cfg.domain_dir / "overlays"):
        if odir.is_dir():
            for f in odir.glob("*.md"):
                if f.stem not in avail:
                    problems.append(f"overlay {f} targets unknown skill '{f.stem}'")
    return problems


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
        manifest["skills"][name] = {"source": str(src), "overlays": applied,
                                    "hash": sha256_tree(dst)}

    # ARIS skills reference ../shared-references/...
    for name in SHARED_DIRS:
        src = cfg.fw / "core" / "skills" / name
        if src.is_dir():
            shutil.copytree(src, target / name)

    write_json(target / "MOUNT.json", manifest)
    return manifest
