"""`autolab init` and `autolab doctor`."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from autolab import human, skills
from autolab.config import Config, framework_root, load_toml
from autolab.paths import Layout
from autolab.util import write_atomic, write_json

STUDENT_ROLES = ("literature", "experiment", "analysis", "figure", "writing")
ROLE_STAGE_TASKS = {
    "literature": [("ideation", "literature"), (None, "literature"), (None, "novelty")],
    "experiment": [(None, "experiment")],
    "analysis": [(None, "analysis"), (None, "theory")],
    "figure": [(None, "figure")],
    "writing": [("writing", "section"), ("polish", None), (None, "compile")],
}


def init(target: Path, name: str, domain: str, site: str, force: bool = False) -> Path:
    fw = framework_root()
    target = target.resolve()
    if (target / "autolab.toml").exists() and not force:
        raise FileExistsError(f"{target} already has autolab.toml")
    tpl = fw / "templates" / "project"
    if not (fw / "sites" / f"{site}.toml").is_file():
        raise FileNotFoundError(f"no sites/{site}.toml; copy sites/_template.toml and fill it in")
    shutil.copytree(tpl, target, dirs_exist_ok=True)
    layout = Layout(target)
    for d in layout.dirs():
        d.mkdir(parents=True, exist_ok=True)
    cfg_text = (tpl / "autolab.toml").read_text(encoding="utf-8")
    cfg_text = (cfg_text.replace("{{NAME}}", name).replace("{{DOMAIN}}", domain)
                .replace("{{SITE}}", site).replace("{{FRAMEWORK_ROOT}}", fw.as_posix()))
    write_atomic(target / "autolab.toml", cfg_text)
    cfg = Config(target)
    _write_settings(cfg)
    skills.mount(cfg)
    write_agents(cfg)
    _write_claude_md(cfg)
    human.check_changes(cfg, now=float("inf"))     # baseline snapshot
    # the baseline is not a human change
    from autolab.util import read_json
    snap = read_json(layout.human_snapshot, {}) or {}
    snap["_pending_ack"] = []
    write_json(layout.human_snapshot, snap)
    return target


# Unattended runs (claude -p) cannot ask for approval, so the allowed commands are listed.
# The PreToolUse hook still enforces the zone rules on top of this list.
ALLOW = [
    "Read", "Glob", "Grep", "Edit", "Write", "WebSearch", "WebFetch", "Skill", "TaskStop",
    "Bash(autolab:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git add:*)",
    "Bash(git commit:*)", "Bash(git rev-parse:*)", "Bash(git init:*)", "Bash(git rm --cached:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(grep:*)",
    "Bash(find:*)", "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(touch:*)", "Bash(echo:*)",
    "Bash(date:*)", "Bash(sed:*)", "Bash(awk:*)", "Bash(sort:*)", "Bash(diff:*)", "Bash(tar:*)",
    "Bash(python:*)", "Bash(python3:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(env:*)",
    "Bash(cd:*)", "Bash(mktemp:*)", "Bash(pdftotext:*)", "Bash(pdfinfo:*)", "Bash(file:*)",
    "Bash(which:*)", "Bash(du:*)", "Bash(stat:*)", "Bash(basename:*)", "Bash(dirname:*)",
    "Bash(realpath:*)", "Bash(xargs:*)", "Bash(uniq:*)", "Bash(cut:*)", "Bash(tr:*)",
    "Bash(gzip:*)", "Bash(gunzip:*)", "Bash(unzip:*)", "Bash(md5sum:*)", "Bash(sha256sum:*)",
    "Bash(squeue:*)", "Bash(sacct:*)", "Bash(seff:*)", "Bash(sinfo:*)", "Bash(module:*)",
]
DENY = [
    "Bash(sbatch:*)", "Bash(scancel:*)", "Bash(ssh:*)", "Bash(scp:*)", "Bash(git push:*)",
    "Bash(rm -rf:*)", "Bash(sudo:*)",
    "Read(~/.ssh/**)", "Read(~/.codex/**)", "Read(~/.claude/.credentials.json)",
    # one unattended turn must end; these keep the session alive (turn 5 hung on ScheduleWakeup)
    "ScheduleWakeup", "CronCreate", "CronDelete", "RemoteTrigger",
]


# human-owned or gate-owned paths: Write/Edit tools denied; with the sandbox, writes from any Bash
# program denied too (`autolab` commands, which run unsandboxed, are the sanctioned writers)
PROTECTED_WRITE = ["autolab.toml", "CLAUDE.md", "findings/**", "state/DECISIONS.md", "00_human/**"]
# network for sandboxed Bash (literature, packages, code hosting); extend in [agents] sandbox_domains
SANDBOX_DOMAINS = ["arxiv.org", "export.arxiv.org", "api.crossref.org", "doi.org", "api.openalex.org",
                   "api.semanticscholar.org", "pypi.org", "files.pythonhosted.org", "github.com",
                   "raw.githubusercontent.com", "codeload.github.com"]


def _write_roots(cfg: Config) -> list[str]:
    """Places outside the project where agents may write files (scratch run output, /tmp)."""
    import os
    roots = ["/tmp"]
    scratch = cfg.site.get("site", {}).get("scratch_root")
    if scratch:
        roots.append(os.path.expandvars(scratch))
    return roots


def _venv_rules(cfg: Config) -> list[str]:
    root = cfg.layout.root.resolve().as_posix()
    rules = []
    for exe in ("python", "python3", "pip", "pip3"):
        rules += [f"Bash(.venv/bin/{exe}:*)", f"Bash(./.venv/bin/{exe}:*)", f"Bash({root}/.venv/bin/{exe}:*)"]
    return rules


def _extra_rules(cfg: Config) -> list[str]:
    """Commands the human allowed in autolab.toml [agents] allow_bash (e.g. compilers for an
    approved external code). An entry with a '/' is a project-relative program path."""
    root = cfg.layout.root.resolve().as_posix()
    rules = []
    for cmd in cfg.data.get("agents", {}).get("allow_bash", []):
        rules.append(f"Bash({cmd}:*)")
        if "/" in cmd and not cmd.startswith("/"):
            rules += [f"Bash(./{cmd}:*)", f"Bash({root}/{cmd}:*)"]
    return rules


def _write_settings(cfg: Config) -> None:
    py = Path(sys.executable).as_posix()
    hook = f'"{py}" -m autolab hook pre-tool-use'
    settings = {
        "env": {"AUTOLAB_HOME": cfg.fw.as_posix(),
                "AUTOLAB_PYTHON": py,
                "ARIS_REPO": (cfg.fw / "third_party" / "aris").as_posix(),
                "PYTHONPATH": (cfg.fw / "src").as_posix(),
                "AUTOLAB_WRITE_ROOTS": ":".join(_write_roots(cfg))},
        # the framework (skills' tools, ARIS scripts) must be readable from the project;
        # the hook still refuses writes outside the project and AUTOLAB_WRITE_ROOTS
        # the project venv's interpreter is called by path, which `Bash(python:*)` does not
        # match (turn 9: diagnosis blocked because `.venv/bin/python` needed approval)
        "permissions": {"allow": ALLOW + _venv_rules(cfg) + _extra_rules(cfg), "deny": DENY,
                        "additionalDirectories": [cfg.fw.as_posix()]},
        "hooks": {"PreToolUse": [{"matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
                                  "hooks": [{"type": "command", "command": hook}]}]},
    }
    # extra variables the human set for the agents ({root} = project root), e.g. BASILISK for qcc
    root = cfg.layout.root.resolve().as_posix()
    agents_cfg = cfg.data.get("agents", {})
    for k, v in agents_cfg.get("env", {}).items():
        settings["env"][k] = str(v).replace("{root}", root)
    # Write/Edit tools: human-owned files are denied outright (deny beats acceptEdits)
    settings["permissions"]["deny"] = DENY + [f"Edit(/{root}/{p})" for p in PROTECTED_WRITE]
    if agents_cfg.get("sandbox"):
        # OS-level boundary for Bash and every program it starts (bubblewrap on Linux): the hook and
        # the allow list only filter command text, a script can still write anywhere the user can
        settings["sandbox"] = {
            "enabled": True,
            "failIfUnavailable": True,             # refuse to run unsandboxed
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": True,
            # `autolab` is the sanctioned writer of protected files; scheduler queries need the network
            "excludedCommands": ["Bash(autolab:*)", "Bash(squeue:*)", "Bash(sacct:*)", "Bash(seff:*)",
                                 "Bash(sinfo:*)"],
            "filesystem": {"allowWrite": _write_roots(cfg),
                           "denyWrite": [f"{root}/{p}" for p in PROTECTED_WRITE]},
            "network": {"allowedDomains": list(agents_cfg.get("sandbox_domains", SANDBOX_DOMAINS))},
        }
    write_json(cfg.layout.claude / "settings.json", settings)


def write_agents(cfg: Config) -> list[Path]:
    """Render .claude/agents/student-*.md with the model and resolved skills for each role."""
    tpl_dir = cfg.fw / "core" / "agents"
    out = []
    for role in STUDENT_ROLES:
        tpl = tpl_dir / f"student-{role}.md"
        if not tpl.is_file():
            continue
        names: list[str] = []
        for stage, task in ROLE_STAGE_TASKS[role]:
            for s in skills.resolve(cfg, "student", stage, task):
                if s not in names:
                    names.append(s)
        model = cfg["models"].get(f"student_{role}", cfg["models"]["student_experiment"])
        text = (tpl.read_text(encoding="utf-8")
                .replace("{{MODEL}}", model)
                .replace("{{SKILLS}}", "\n".join(f"- `{s}`" for s in names))
                .replace("{{DOMAIN}}", cfg.domain_name)) + _site_notes(cfg)
        p = cfg.layout.agents / f"student-{role}.md"
        write_atomic(p, text)
        out.append(p)
    return out


def _write_claude_md(cfg: Config) -> None:
    tpl = (cfg.fw / "templates" / "CLAUDE.md.tmpl").read_text(encoding="utf-8")
    pi_skills = {st: skills.resolve(cfg, "pi", st) for st in
                 ("init", "ideation", "planning", "claim_review", "writing", "rebuttal", "retrospective")}
    table = "\n".join(f"| {st} | {', '.join(f'`{s}`' for s in names)} |" for st, names in pi_skills.items())
    # copy the field notes INTO the project: agents should not need paths outside it
    notes = cfg.domain_dir / "field-notes.md"
    if notes.is_file():
        write_atomic(cfg.layout.root / "DOMAIN_NOTES.md",
                     f"<!-- generated from domains/{cfg.domain_name}/field-notes.md by `autolab skills mount`; "
                     "edit the framework copy -->\n" + notes.read_text(encoding="utf-8"))
    text = (tpl.replace("{{PI_SKILL_TABLE}}", table)
            .replace("{{DOMAIN}}", cfg.domain_name)
            .replace("{{DOMAIN_TITLE}}", cfg.domain.get("domain", {}).get("title", cfg.domain_name))
            .replace("{{FIELD_NOTES}}", "DOMAIN_NOTES.md"))
    write_atomic(cfg.layout.root / "CLAUDE.md", text + _site_notes(cfg))


def _site_notes(cfg: Config) -> str:
    """Cluster facts from sites/<site>.toml `notes`, appended to CLAUDE.md and the student agents,
    so core templates stay free of any one cluster's details."""
    site = cfg.site.get("site", {})
    notes = list(cfg.site.get("notes") or site.get("notes") or [])
    per_core = site.get("mem_gb_per_core")
    if per_core:
        notes.append(f"memory is billed as cores here: every ~{per_core} GB counts as one core; request only the "
                     f"memory you need and state why in `[resources] mem_reason` if it exceeds 2 x cpus x {per_core} GB")
    if not notes:
        return ""
    return (f"\n## Cluster notes (site `{site.get('name', cfg['project']['site'])}`, from sites/*.toml)\n"
            + "".join(f"- {n}\n" for n in notes))


def remount(cfg: Config) -> dict:
    """Refresh everything generated from the framework: skills, agents, CLAUDE.md, settings."""
    m = skills.mount(cfg)
    write_agents(cfg)
    _write_claude_md(cfg)
    _write_settings(cfg)
    return m


def doctor(cfg: Config) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    add = lambda name, ok, detail="": checks.append((name, bool(ok), detail))
    add("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    add("framework root", (cfg.fw / "core" / "skills.toml").is_file(), str(cfg.fw))
    add(f"domain pack '{cfg.domain_name}'", cfg.domain_dir.is_dir(), str(cfg.domain_dir))
    probs = skills.check(cfg)
    add("skill routes resolve", not probs, "; ".join(probs[:5]))
    mount = cfg.layout.skills_mount / "MOUNT.json"
    add("skills mounted", mount.is_file(), "run `autolab skills mount`" if not mount.is_file() else "")
    if mount.is_file():
        m = json.loads(mount.read_text(encoding="utf-8"))
        add("mounted domain matches config", m.get("domain") == cfg.domain_name,
            f"mounted={m.get('domain')} config={cfg.domain_name}")
    add("ARIS tools present", (cfg.fw / "third_party" / "aris" / "tools" / "research_wiki.py").is_file())
    try:
        from autolab.knowledge import _pypdf
        add("PDF text extraction (pypdf)", True, _pypdf(cfg).__version__)
    except RuntimeError as e:
        add("PDF text extraction (pypdf)", False, str(e))
    add("hook configured", (cfg.layout.claude / "settings.json").is_file())
    add("agents rendered", any(cfg.layout.agents.glob("student-*.md")))
    unfilled = human.unfilled_templates(cfg)
    add("human templates filled", not unfilled, ", ".join(unfilled))
    add("STOP absent", not human.stopped(cfg))
    add("human READY", human.is_ready(cfg), "create 00_human/READY after filling START_HERE steps")
    rv = cfg["reviewer"]
    if rv["backend"] in ("codex", "command"):
        exe = shutil.which(rv["command"][0])
        detail = ""
        if exe:
            v = subprocess.run([exe, "--version"], capture_output=True, text=True)
            detail = (v.stdout or v.stderr).strip()
        add(f"reviewer CLI ({rv['command'][0]})", exe, detail or "not on PATH")
    else:
        add(f"reviewer backend = {rv['backend']}", True, "mock backend: reviews are not real")
    for tool in ("sbatch", "sacct", "squeue"):
        add(f"slurm: {tool}", shutil.which(tool), "")
    add("slurm.account set", bool(cfg["slurm"]["account"]), "fill from `slist`")
    add("claude CLI", shutil.which("claude"), "")
    trusted = claude_trusted(cfg.layout.root)
    add("claude trusts this project", trusted,
        "" if trusted else "run `claude` once in this folder and accept the trust prompt "
                           "(otherwise permissions and hooks are not loaded)")
    return checks


def claude_trusted(root: Path) -> bool:
    """Read-only check of ~/.claude.json: has the human accepted the trust prompt here?"""
    cfgf = Path.home() / ".claude.json"
    try:
        projects = json.loads(cfgf.read_text(encoding="utf-8")).get("projects", {})
    except (OSError, json.JSONDecodeError):
        return False
    here = root.resolve()
    for p in (here, *here.parents):          # a trusted parent folder covers its subfolders
        entry = projects.get(p.as_posix()) or projects.get(str(p)) or {}
        if entry.get("hasTrustDialogAccepted"):
            return True
    return False
