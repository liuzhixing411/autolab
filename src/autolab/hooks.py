"""Claude Code PreToolUse hook: enforce the zone rules mechanically.

Wired in <project>/.claude/settings.json. Reads the hook JSON on stdin; exit code 2
blocks the tool call and the stderr message is shown to the agent.

Rules
- 00_human/STOP present  → only read-only tools allowed
- 00_human/              → humans only; agents may write ACTION_NEEDED.md and ACK.md
- findings/              → only `autolab gate claim`
- .claude/skills/        → mounted copies; change overlays and re-mount instead
- experiments/*/plan.md  → locked once a job has been submitted (use plan_amendments.md)
- raw sbatch / scancel   → use `autolab slurm ...`
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "TodoWrite"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
HUMAN_WRITABLE = {"ACTION_NEEDED.md", "ACK.md"}
# budget/caps, the agents' own permissions and the generated protocol: written by the human or
# `autolab skills mount`, never by agents (an agent could otherwise raise its own budget or grant
# itself tools; found while widening the allow list for compilers, 2026-10-16)
HUMAN_CONFIG = ("autolab.toml", ".claude/settings", ".claude/agents/", "CLAUDE.md")
# written only through `autolab decide`, so past decisions cannot be rewritten after the fact
APPEND_ONLY = ("state/DECISIONS.md",)
# records only autolab commands write: the frozen evaluator, outcomes, lessons, spend and call logs
CLI_OWNED = re.compile(r"^(experiments/[^/]+/(evaluator\.json|outcomes?\.jsonl?|outcome\.json|submission_pending\.json|plan_gate\.json|jobs(?:_usage)?\.jsonl)|"
                       r"claims/[^/]+/(reviews|gate)\.json|state/reviews/.*|"
                       r"experience/lessons\.jsonl|state/(calls|budget)\.jsonl)$")
_CLI_OWNED_TEXT = ("evaluator.json", "outcome.json", "outcomes.jsonl", "lessons.jsonl", "calls.jsonl", "budget.jsonl",
                   "plan_gate.json", "jobs.jsonl", "jobs_usage.jsonl", "submission_pending.json", "reviews.json", "state/reviews/")
_AUTOLAB_CMD = re.compile(r"^\s*(autolab|python3?\s+-m\s+autolab|\S*python\S*\s+-m\s+autolab)\b")
_RAW_SLURM = re.compile(r"(^|[;&|`(]\s*|\s)(sbatch|scancel)\b")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")
_REDIRECT = re.compile(r"(?<![<>&])(?:\d|&)?>>?\s*([^\s;|&<>]+)")
_WRITERS = {"rm", "mv", "tee", "truncate", "rmdir", "touch", "chmod", "unlink"}
_DEST_ONLY = {"cp", "install", "rsync", "ln"}
_HARMLESS_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr"}


def _writes_into(cmd: str, zone: str, allowed: tuple[str, ...] = ()) -> bool:
    """True if some part of a shell command writes into `zone` (best effort, per segment).

    Reads that merely mention the zone (`ls 00_human/`, `cat 00_human/GOAL.md 2>/dev/null`)
    are fine; redirections into it and modifying commands whose arguments touch it are not.
    """
    def hits(text: str) -> bool:
        return zone in text and not any(a in text for a in allowed)

    for seg in _SEGMENT_SPLIT.split(cmd):
        for target in _REDIRECT.findall(seg):
            if target not in _HARMLESS_TARGETS and not target.startswith("&") and hits(target):
                return True
        words = seg.strip().split()
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):   # VAR=x cmd
            words = words[1:]
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        args = [w for w in words[1:] if not w.startswith("-")]
        if prog in _DEST_ONLY:                       # copying OUT of a zone is a read
            if args and hits(args[-1]):
                return True
        elif prog == "dd":
            if any(w.startswith("of=") and hits(w) for w in words[1:]):
                return True
        elif prog in _WRITERS or (prog == "sed" and any(w.startswith("-i") for w in words[1:])):
            if any(hits(w) for w in words[1:]):
                return True
    return False


_INSTALL = re.compile(r"(?:^|\s)(?:\S*/)?(?:pip3?|python3?(?:\.\d+)?\s+-m\s+pip|conda|mamba|micromamba|uv\s+pip)"
                      r"\s+(?:[^|;&]*\s)?(install|download)\b")


def _package_install(seg: str, root: Path) -> str | None:
    """Package installs must stay inside the project (a project .venv or --target in the
    project). Installing into the user's site-packages changes their other work.
    Seen in the first real run: after `pip install` was refused, an agent used
    `python -m pip download` + `python -m pip install --user`."""
    if not _INSTALL.search(seg):
        return None
    if re.search(r"(?:^|\s)(?:\./)?\.venv/bin/(?:python3?|pip3?)\b", seg) or \
            str(root / ".venv") in seg:
        return None
    m = re.search(r"--target[ =](\S+)", seg)
    if m and not m.group(1).startswith(("/", "..", "~")) and "--user" not in seg:
        return None
    return ("Package installs outside the project are not allowed in unattended runs. Use a project "
            "virtualenv: `python -m venv --system-site-packages .venv` then `.venv/bin/python -m pip install <pkg>` "
            "and run jobs with `.venv/bin/python`; or ask the human with `autolab human request --kind approval`. "
            "Do not work around this.")


def _rel(path: str, root: Path) -> str | None:
    try:
        p = Path(path)
        p = p if p.is_absolute() else root / p
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


_INTERP = re.compile(r"(^|[\s/;&|(])(python3?|perl|ruby|node|Rscript)\b")
_WRITE_OPS = re.compile(r"write_text|write_bytes|\.write\(|open\([^)]*,\s*(mode\s*=\s*)?['\"][wax+]|shutil\.|os\.(replace|rename|remove|"
                        r"unlink|truncate)|\.unlink\(|\.rename\(|\.replace\(|>>?\s*\S")


def _interpreter_writes(cmd: str, root: Path) -> str:
    """Protected path mentioned by an interpreter command that also performs a file write
    (seen in review: `python -c "Path('autolab.toml').write_text(...)"` passed the shell checks)."""
    plain = re.sub(r"\d?>&\d|\d?>\s*/dev/null", " ", cmd)           # stream merges are not writes
    if not _INTERP.search(plain) or not _WRITE_OPS.search(plain):
        return ""
    paths = list(HUMAN_CONFIG) + list(APPEND_ONLY) + list(_CLI_OWNED_TEXT) + ["findings/", "00_human/"]
    paths += [p.parent.relative_to(root).as_posix() + "/plan.md"
              for p in (root / "experiments").glob("*/jobs.jsonl")] if (root / "experiments").is_dir() else []
    for p in paths:
        if p in cmd and not any(ok in cmd for ok in ("00_human/ACTION_NEEDED.md", "00_human/ACK.md")
                                if p == "00_human/"):
            return p
    return ""


def decide(event: dict, root: Path) -> tuple[bool, str]:
    tool = event.get("tool_name", "")
    inp = event.get("tool_input", {}) or {}
    if (root / "00_human" / "STOP").exists() and tool not in READ_ONLY_TOOLS:
        return False, "00_human/STOP exists: the human paused the lab. Only reading is allowed."

    if tool in WRITE_TOOLS:
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        rel = _rel(target, root)
        if rel is None:
            import os
            roots = [r for r in os.environ.get("AUTOLAB_WRITE_ROOTS", "/tmp").split(os.pathsep) if r]
            try:
                p = Path(target).expanduser().resolve()
            except OSError:
                return False, f"cannot resolve {target}"
            from autolab.config import framework_root
            if p.is_relative_to(framework_root().resolve()):
                return False, "The framework is read-only, including when installed under a scratch root."
            if any(p.is_relative_to(Path(r).expanduser().resolve()) for r in roots):
                return True, ""
            return False, (f"{target} is outside the project. Write inside the project, or to "
                           f"{', '.join(roots)} for scratch output. The framework is read-only.")
        if rel.startswith("00_human/") and Path(rel).name not in HUMAN_WRITABLE:
            return False, ("00_human/ belongs to the human. Write requests to 00_human/ACTION_NEEDED.md "
                           "(`autolab human request`) and acknowledgements via `autolab human ack`.")
        if rel.startswith("findings/"):
            return False, "findings/ is written only by `autolab gate claim <C-id>`."
        if any(p.endswith(".lock") for p in Path(rel).parts) or rel.startswith("state/pi_logs/"):
            return False, "Lock ownership and runner logs are not agent-writable."
        if rel.startswith(".claude/skills/"):
            return False, "Mounted skills are generated. Edit core/ or domain overlays, then `autolab skills mount`."
        if any(rel == p or rel.startswith(p) for p in HUMAN_CONFIG):
            return False, (f"{rel} is human-owned configuration (budget, caps, permissions, protocol). "
                           "Ask for a change with `autolab human request --kind approval`.")
        if rel in APPEND_ONLY:
            return False, f"{rel} is append-only: add entries with `autolab decide \"...\" --why \"...\"`."
        if CLI_OWNED.match(rel):
            return False, (f"{rel} is written only by autolab commands (`autolab outcome`, `autolab lesson`, "
                           "`autolab slurm submit`, the runner). A changed analysis script is explained in "
                           "plan_amendments.md, not by editing the frozen fingerprint.")
        m = re.match(r"experiments/([^/]+)/plan\.md$", rel)
        if m and (root / "experiments" / m.group(1) / "jobs.jsonl").exists():
            return False, (f"plan.md of {m.group(1)} is locked (a job was submitted). "
                           "Record changes in plan_amendments.md with a timestamp and reason.")
        return True, ""

    if tool == "Bash":
        cmd = inp.get("command", "")
        if _AUTOLAB_CMD.match(cmd):
            # A prefix is not an authorization for the rest of a shell program.
            plain = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", " _Q_ ", cmd)
            if re.search(r"[;&|<>\n\r]", plain) or "$(" in cmd or "`" in cmd:
                return False, "Run autolab as one command without chaining, redirection or shell substitution."
            import shlex
            try:
                args = shlex.split(cmd)
            except ValueError:
                return False, "Invalid command quoting."
            command = args[1:] if args[0] == "autolab" else args[args.index("-m") + 2:]
            # Runner/operator controls must not be invoked by the agent being checked.
            if (command and command[0] in ("init", "guard", "weixin")) or "--project" in command or any(a.startswith("--project=") for a in command):
                return False, "Project initialization, project switching and integrity controls belong to the operator."
            if command[:2] == ["budget", "record"]:
                return False, "Only the runner records API costs."
            return True, ""
        # blank out quoted text first: a `;` or "sbatch" inside a commit message is not a command
        unquoted = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", " _Q_ ", cmd)
        for seg in _SEGMENT_SPLIT.split(unquoted):
            words = seg.strip().split()
            if not words:
                continue
            prog = words[0].rsplit("/", 1)[-1]
            # a commit message or echo that merely mentions sbatch is not a call
            if prog not in ("git", "echo", "printf", "autolab") and _RAW_SLURM.search(seg):
                return False, "Do not call sbatch/scancel directly; use `autolab slurm validate|submit|status`."
            why = _package_install(seg, root)
            if why:
                return False, why
        human_ok = tuple(f"00_human/{n}" for n in HUMAN_WRITABLE)
        if _writes_into(cmd, "00_human/", human_ok):
            return False, ("Shell command would modify 00_human/; that zone belongs to the human. "
                           "Reading it is fine.")
        if _writes_into(cmd, "findings/"):
            return False, "Shell command would modify findings/; only `autolab gate claim` writes there."
        if _writes_into(cmd, ".claude/skills"):
            return False, "Mounted skills are generated; re-mount instead of editing."
        if any(_writes_into(cmd, p) for p in HUMAN_CONFIG):
            return False, ("Shell command would modify human-owned configuration "
                           f"({', '.join(HUMAN_CONFIG)}); ask with `autolab human request --kind approval`.")
        if any(_writes_into(cmd, p) for p in APPEND_ONLY):
            return False, "state/DECISIONS.md is append-only: use `autolab decide`."
        if any(_writes_into(cmd, p) for p in _CLI_OWNED_TEXT):
            return False, ("Outcome, evaluator, lesson, budget and call records are written only by autolab "
                           "commands (`autolab outcome`, `autolab lesson ...`).")
        hit = _interpreter_writes(cmd, root)
        if hit:
            return False, (f"A script that writes files mentions the protected path '{hit}'. Protected files change "
                           "only through `autolab` commands or the human. (This check is best effort; the "
                           "post-turn guard and the OS sandbox are the real boundary.)")
    return True, ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    root = Path(event.get("cwd") or ".")
    import os
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", root))
    ok, why = decide(event, root)
    if ok:
        return 0
    print(why, file=sys.stderr)
    return 2
