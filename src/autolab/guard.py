"""Integrity snapshots and exact trusted-CLI mutation receipts, stored outside the project.

Unexplained protected-file changes fail closed, even when their origin could be a human.
The OS sandbox prevents agent subprocesses from accessing the receipt store; hooks are
only a second layer. Cluster jobs still require deployment-level filesystem isolation.
"""

from __future__ import annotations

import hashlib
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
    for name in ("evaluator.json", "outcome.json", "outcomes.jsonl"):   # written by autolab commands only
        files += list(cfg.layout.experiments.glob(f"*/{name}"))
    files += [cfg.layout.experience / "lessons.jsonl", cfg.layout.state / "calls.jsonl",
              cfg.layout.state / "budget.jsonl", cfg.layout.state / "requests.json"]
    files += list(cfg.layout.reviews.rglob("*"))
    for pattern in ("*/plan_gate.json", "*/jobs.jsonl", "*/jobs_usage.jsonl", "*/submission_pending.json"):
        files += list(cfg.layout.experiments.glob(pattern))
    for pattern in ("*/reviews.json", "*/gate.json"):
        files += list(cfg.layout.claims.glob(pattern))
    out = {}
    for p in files:
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel not in HUMAN_WRITABLE and ".lock" not in rel and not p.name.startswith(".tmp-"):
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
    write_json(d / "authorized.json", {})
    return {"files": len(hashes)}


def authorize_changes(cfg: Config, before: dict[str, str]) -> None:
    """Record exact mutations of a completed trusted CLI invocation, outside the sandbox.

    Never bless files unchanged by that invocation (they may have been altered earlier).
    A later edit invalidates the receipt even if the log mentions a sanctioned command.
    """
    from autolab.util import FileLock
    d = _dir(cfg)
    if not (d / "snapshot.json").is_file():
        return
    after = protected(cfg)
    with FileLock(d / "authorized"):
        approved = read_json(d / "authorized.json", {}) or {}
        for rel in set(before) | set(after):
            if before.get(rel) != after.get(rel):
                approved[rel] = after.get(rel)
        write_json(d / "authorized.json", approved)


def require_trusted_records(cfg: Config, paths: list[Path] | None = None) -> None:
    """During an unattended turn, do not consume unreceipted approvals or ledgers."""
    snap = read_json(_dir(cfg) / "snapshot.json")
    if not snap:
        return
    from autolab.hooks import CLI_OWNED
    approved = read_json(_dir(cfg) / "authorized.json", {}) or {}
    expected = {**snap["hashes"], **approved}
    current = protected(cfg)
    selected = {p.relative_to(cfg.layout.root).as_posix() for p in paths} if paths is not None else None
    bad = [p for p in set(expected) | set(current) if CLI_OWNED.match(p)
           and (selected is None or p in selected)
           and expected.get(p) != current.get(p)]
    if bad:
        raise RuntimeError("untrusted control records changed: " + ", ".join(sorted(bad)))


HUMAN_EDIT = "human-zone file changed without a receipt; no agent write in the turn log, accepted as a human edit"
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def _tool_calls(log: Path | None) -> list[tuple[str, dict]]:
    """(tool name, input) of every tool call in a stream-json turn log, subagents included."""
    import json
    out = []
    if not log or not Path(log).is_file():
        return out
    for line in Path(log).read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") if isinstance(ev, dict) else None
        for c in (msg or {}).get("content", []) if isinstance(msg, dict) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                out.append((str(c.get("name", "")), c.get("input") or {}))
    return out


def _agent_wrote_human_zone(calls: list[tuple[str, dict]]) -> bool:
    """Any sign that a tool call wrote into 00_human/ outside ACTION_NEEDED/ACK: a write tool aimed
    there, a shell command writing there, or an interpreter command with a write op naming it."""
    from autolab import hooks
    ok = tuple(HUMAN_WRITABLE)
    for name, inp in calls:
        if name in _WRITE_TOOLS:
            target = str(inp.get("file_path") or inp.get("notebook_path") or "")
            if "00_human/" in target and not target.endswith(ok):
                return True
        elif name == "Bash":
            cmd = str(inp.get("command", ""))
            if hooks._AUTOLAB_CMD.match(cmd):
                continue                       # autolab commands leave receipts of their own
            if hooks._writes_into(cmd, "00_human/", ok):
                return True
            if "00_human" in cmd and hooks._INTERP.search(cmd) and hooks._WRITE_OPS.search(cmd):
                return True
    return False


def check(cfg: Config, log: Path | None) -> dict:
    d = _dir(cfg)
    snap = read_json(d / "snapshot.json")
    if not snap:
        return {"checked": False, "why": "no snapshot"}
    approved = read_json(d / "authorized.json", {}) or {}
    before, after = snap["hashes"], protected(cfg)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    violations, restored = [], []
    calls = _tool_calls(log)
    agent_in_human_zone = _agent_wrote_human_zone(calls)
    for rel in changed:
        if rel in approved and approved[rel] == after.get(rel):
            continue
        if rel == "state/DECISIONS.md":
            now = (cfg.layout.root / rel).read_text(encoding="utf-8") if (cfg.layout.root / rel).is_file() else ""
            if not now.startswith(snap.get("decisions", "")):
                violations.append({"file": rel, "kind": "decision log rewritten (append-only)"})
            continue
        if rel.startswith("findings/"):
            violations.append({"file": rel, "kind": "findings changed without a trusted mutation receipt"})
            continue
        if rel in CONFIG or rel.startswith(".claude/agents/"):
            src = d / "copies" / rel
            if src.is_file():
                shutil.copy2(src, cfg.layout.root / rel)
                restored.append(rel)
            violations.append({"file": rel, "kind": "configuration changed by a tool call (restored)"})
        elif rel.startswith("00_human/") and rel not in HUMAN_WRITABLE and not agent_in_human_zone:
            # The human's own zone: agents cannot write there (hook, sandbox) and the log shows no
            # attempt, so this is the human editing during a turn. Recorded, but it no longer stops
            # the lab (first-test, 2026-10-19: an inbox note uploaded mid-turn killed the runner).
            violations.append({"file": rel, "kind": HUMAN_EDIT})
        else:
            violations.append({"file": rel, "kind": "protected file changed without a trusted mutation receipt"})
    serious = [v for v in violations if "restored" not in v["kind"] and v["kind"] != HUMAN_EDIT]
    res = {"checked": True, "changed": changed, "violations": violations, "restored": restored,
           "serious": bool(serious)}
    if violations:
        from autolab.human import request_action
        append_jsonl(cfg.layout.state / "guard_violations.jsonl", {"at": now_iso(), "log": str(log), **res})
        if serious:
            (cfg.layout.human / "STOP").touch()
        only_human = all(v["kind"] == HUMAN_EDIT for v in violations)
        request_action(cfg, "approval" if not only_human else "other",
                       "Integrity guard: " + "; ".join(f"{v['file']}: {v['kind']}" for v in violations[:6])
                       + (". The lab is paused (00_human/STOP); check with `git diff` / `git log`, then delete STOP."
                          if serious else
                          (". FYI: if you did not make these edits, check them; uploads through `autolab human add` "
                           "carry a receipt and are not reported." if only_human
                           else ". Configuration was restored from the pre-turn snapshot.")),
                       blocking=bool(serious), rule="integrity")
    return res
