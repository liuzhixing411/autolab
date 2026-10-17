"""Post-turn integrity check of protected files, run by the runner (outside the agents).

The PreToolUse hook and the permission lists are best-effort filters: a program an agent runs
(e.g. `python -c "Path('autolab.toml').write_text(...)"`) writes files without passing through
them. The real boundary is OS-level (a separate account, a container, or Claude Code's sandbox:
`[agents] sandbox = true`). This module adds detection: before a turn the runner snapshots the
protected files (hashes, and copies of the configuration) outside the project; after the turn it
lists protected files that changed and checks whether a tool call in the turn log touched them.

- configuration (autolab.toml, .claude/settings*, .claude/agents/, CLAUDE.md) changed by a tool
  call: restored from the snapshot, recorded, and the human is told;
- findings/ changed without a matching `autolab gate claim`, state/DECISIONS.md rewritten instead of
  appended, a locked plan or the human zone changed by a tool call: recorded, 00_human/STOP is
  created and the human is asked to check (nothing is reverted automatically).
Human edits do not appear in the turn log, so they are not flagged. Residual risks remain (a program
that builds the path at run time, SLURM jobs running on compute nodes); see README "Safety model".
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from autolab.config import Config
from autolab.util import append_jsonl, now_iso, read_json, sha256_file, write_json

CONFIG = ("autolab.toml", ".claude/settings.json", ".claude/settings.local.json", "CLAUDE.md")
HUMAN_WRITABLE = {"00_human/ACTION_NEEDED.md", "00_human/ACK.md"}


def _dir(cfg: Config) -> Path:
    key = hashlib.sha1(str(cfg.layout.root.resolve()).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "autolab" / "guard" / key


def protected(cfg: Config) -> dict[str, str]:
    root = cfg.layout.root
    files: list[Path] = [root / c for c in CONFIG]
    files += list((root / ".claude" / "agents").glob("*.md"))
    files += [p for p in (root / "00_human").rglob("*") if p.is_file()]
    files += [p for p in cfg.layout.findings.rglob("*") if p.is_file()]
    files.append(cfg.layout.state / "DECISIONS.md")
    files += [p.parent / "plan.md" for p in cfg.layout.experiments.glob("*/jobs.jsonl")]
    out = {}
    for p in files:
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel not in HUMAN_WRITABLE and ".lock" not in rel:
                out[rel] = sha256_file(p)
    return out


def snapshot(cfg: Config) -> dict:
    d = _dir(cfg)
    shutil.rmtree(d / "copies", ignore_errors=True)
    hashes = protected(cfg)
    for rel in hashes:
        if rel in CONFIG or rel.startswith(".claude/agents/"):
            dst = d / "copies" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg.layout.root / rel, dst)
    dec = cfg.layout.state / "DECISIONS.md"
    snap = {"at": now_iso(), "hashes": hashes,
            "decisions": dec.read_text(encoding="utf-8") if dec.is_file() else ""}
    write_json(d / "snapshot.json", snap)
    return {"files": len(hashes)}


def _tool_calls(log: Path) -> list[tuple[str, str]]:
    """(tool, target text) for every write-capable tool call in a turn log."""
    out = []
    if not log or not log.is_file():
        return out
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        for b in (ev.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if b.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    out.append((b["name"], str(inp.get("file_path") or inp.get("notebook_path") or "")))
                elif b.get("name") == "Bash":
                    out.append(("Bash", str(inp.get("command", ""))))
    return out


def _touched(rel: str, calls: list[tuple[str, str]]) -> bool:
    for tool, target in calls:
        if tool == "Bash":
            cmd = target.strip()
            if cmd.startswith("autolab ") or cmd.startswith("autolab\t"):
                continue                       # the sanctioned interface
            if rel in cmd or (rel.startswith("00_human/") and "00_human" in cmd and Path(rel).name in cmd):
                return True
        elif target.endswith(rel):
            return True
    return False


def _gate_ran(calls: list[tuple[str, str]]) -> bool:
    return any(t == "Bash" and "autolab gate claim" in c for t, c in calls)


def check(cfg: Config, log: Path | None) -> dict:
    d = _dir(cfg)
    snap = read_json(d / "snapshot.json")
    if not snap:
        return {"checked": False, "why": "no snapshot"}
    before, after = snap["hashes"], protected(cfg)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    calls = _tool_calls(log) if log else []
    violations, restored = [], []
    for rel in changed:
        if rel == "state/DECISIONS.md":
            now = (cfg.layout.root / rel).read_text(encoding="utf-8") if (cfg.layout.root / rel).is_file() else ""
            if not now.startswith(snap.get("decisions", "")):
                violations.append({"file": rel, "kind": "decision log rewritten (append-only)"})
            continue
        if rel.startswith("findings/"):
            if not _gate_ran(calls) or _touched(rel, calls):
                violations.append({"file": rel, "kind": "findings changed outside `autolab gate claim`"})
            continue
        if not _touched(rel, calls):
            continue                           # e.g. the human edited it during the turn
        if rel in CONFIG or rel.startswith(".claude/agents/"):
            src = d / "copies" / rel
            if src.is_file():
                shutil.copy2(src, cfg.layout.root / rel)
                restored.append(rel)
            violations.append({"file": rel, "kind": "configuration changed by a tool call (restored)"})
        else:
            violations.append({"file": rel, "kind": "protected file changed by a tool call"})
    res = {"checked": True, "changed": changed, "violations": violations, "restored": restored}
    if violations:
        from autolab.human import request_action
        append_jsonl(cfg.layout.state / "guard_violations.jsonl", {"at": now_iso(), "log": str(log), **res})
        serious = [v for v in violations if "restored" not in v["kind"]]
        if serious:
            (cfg.layout.human / "STOP").touch()
        request_action(cfg, "approval",
                       "Integrity guard: " + "; ".join(f"{v['file']}: {v['kind']}" for v in violations[:6])
                       + (". The lab is paused (00_human/STOP); check with `git diff` / `git log`, then delete STOP."
                          if serious else ". Configuration was restored from the pre-turn snapshot."),
                       blocking=bool(serious))
    return res
