"""Human zone: change detection + ACK, ACTION_NEEDED, STOP, and uploaded material.

Humans edit 00_human/ at any time. Agents never write there except ACTION_NEEDED.md
(requests to the human) and ACK.md (acknowledging a human change).
"""

from __future__ import annotations

import re
import time
import tomllib
from pathlib import Path

from autolab.config import Config
from autolab.util import (append_jsonl, now_iso, read_json, read_jsonl, sha256_file,
                          sha256_tree, write_json)

PLACEHOLDER = "<<FILL"          # templates mark unfilled fields with <<FILL: ...>>
UPLOAD_META = "meta.toml"
UPLOAD_REQUIRED = ("title", "kind", "description", "provenance", "use_as")
UPLOAD_KINDS = {"experimental_data", "simulation_data", "reference_solution", "figure",
                "paper", "notes", "code", "other"}
UPLOAD_USES = {"evidence", "validation_reference", "background", "constraint"}


# ---------------------------------------------------------------- change detection

def _watched(cfg: Config) -> dict[str, Path]:
    L = cfg.layout
    out = {p.name: p for p in L.human_docs}
    out["inbox"] = L.inbox
    out["taste"] = L.human / "taste"
    out["uploads"] = L.uploads
    out["pdf_drop"] = L.pdf_drop
    return out


def _fingerprint(p: Path) -> dict:
    if not p.exists():
        return {"hash": None, "mtime": 0.0}
    files = [p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]
    mtime = max((x.stat().st_mtime for x in files), default=0.0)
    return {"hash": sha256_tree(p), "mtime": mtime}


def check_changes(cfg: Config, now: float | None = None, commit: bool = True) -> dict:
    """Compare 00_human/ with the last snapshot.

    Returns {"changed": [...], "settling": [...]}. A change counts only after the files
    have been untouched for `gates.human_debounce_minutes`, so a half-saved edit is not
    picked up. Committed changes update the snapshot and need an ACK from the PI.
    """
    now = now or time.time()
    debounce = cfg["gates"]["human_debounce_minutes"] * 60
    snap = read_json(cfg.layout.human_snapshot, {}) or {}
    if is_ready(cfg) and not snap.get("_ready_seen"):
        debounce = 0                     # READY = "I finished the initial input": no settling wait
        if commit:
            snap["_ready_seen"] = now_iso()
    changed, settling = [], []
    for name, path in _watched(cfg).items():
        fp = _fingerprint(path)
        if snap.get(name, {}).get("hash") == fp["hash"]:
            continue
        if now - fp["mtime"] < debounce:
            settling.append(name)
            continue
        changed.append(name)
        if commit:
            snap[name] = fp
    if commit and (changed or snap.get("_ready_seen")):
        pending = snap.setdefault("_pending_ack", [])
        pending.extend(n for n in changed if n not in pending)
        write_json(cfg.layout.human_snapshot, snap)
    return {"changed": changed, "settling": settling}


def pending_ack(cfg: Config) -> list[str]:
    return (read_json(cfg.layout.human_snapshot, {}) or {}).get("_pending_ack", [])


def ack(cfg: Config, note: str) -> list[str]:
    """PI acknowledges every pending human change with a note of what it did about it."""
    snap = read_json(cfg.layout.human_snapshot, {}) or {}
    items = snap.get("_pending_ack", [])
    if not items:
        return []
    with cfg.layout.ack.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"\n## {now_iso()} — {', '.join(items)}\n\n{note.strip()}\n")
    snap["_pending_ack"] = []
    write_json(cfg.layout.human_snapshot, snap)
    return items


def unfilled_templates(cfg: Config) -> list[str]:
    return [p.name for p in cfg.layout.human_docs
            if p.is_file() and PLACEHOLDER in p.read_text(encoding="utf-8")]


def stopped(cfg: Config) -> bool:
    return cfg.layout.stop.exists()


def delegation(cfg: Config) -> dict | None:
    """Active 00_human/DELEGATED (TOML: until = ISO time, to = "pi"), or None.

    While active, decisions the RULES reserve for the human are made by the PI and logged
    for review instead of blocking the lab (the human is away)."""
    import datetime as dt
    p = cfg.layout.human / "DELEGATED"
    if not p.is_file():
        return None
    try:
        d = tomllib.loads(p.read_text(encoding="utf-8"))
        until = dt.datetime.fromisoformat(str(d["until"]))
    except (tomllib.TOMLDecodeError, KeyError, ValueError):
        return None
    now = dt.datetime.now(until.tzinfo) if until.tzinfo else dt.datetime.now()
    return {**d, "until": until.isoformat()} if now < until else None


def is_ready(cfg: Config) -> bool:
    """The human signals the initial input is complete by creating 00_human/READY."""
    return cfg.layout.ready.exists()


# ---------------------------------------------------------------- ACTION_NEEDED


def notify(cfg: Config, text: str) -> None:
    """PI progress or reply, explicitly informational (never an accepted finding)."""
    from autolab.util import FileLock
    if not text.strip():
        raise ValueError("notification text is required")
    path = cfg.layout.state / "human_notifications.jsonl"
    with FileLock(path):
        append_jsonl(path, {"at": now_iso(), "text": text.strip()})

def request_action(cfg: Config, kind: str, text: str, blocking: bool = False,
                   dedupe: str | None = None, rule: str | None = None, recommend: str | None = None,
                   deadline_hours: float | None = None, enforce: bool = False) -> str | None:
    """Append a request for the human. kind: download | decision | data | approval | other.
    With `dedupe`, nothing is added while an open request already contains that text.
    A blocking request carries the PI's recommendation and a deadline (autonomy.py); with
    `enforce` (the CLI) a missing recommendation is an error."""
    from autolab import autonomy
    from autolab.util import FileLock, next_id
    meta = None
    if blocking:
        if enforce or rule or recommend:
            meta = autonomy.plan_request(cfg, rule, recommend, deadline_hours)
        else:
            meta = {"rule": "", "recommend": "", "deadline": None}
        text = text.strip() + " " + autonomy.suffix(meta)
    with FileLock(cfg.layout.action_needed):
        if dedupe and any(dedupe in line for line in open_actions(cfg)):
            return None
        aid = next_id(cfg.layout.ids, "action")
        tag = "BLOCKING" if blocking else "optional"
        with cfg.layout.action_needed.open("a", encoding="utf-8", newline="\n") as f:
            f.write(f"- [ ] **{aid}** [{tag}] [{kind}] {text.strip()}  _(added {now_iso()})_\n")
    if meta is not None:
        autonomy.register(cfg, aid, meta)
    return aid


def resolve_actions(cfg: Config, needle: str, note: str) -> int:
    """Tick off open requests containing `needle` (e.g. a paper the agents obtained themselves)."""
    from autolab.util import FileLock, write_atomic
    p = cfg.layout.action_needed
    if not p.is_file():
        return 0
    with FileLock(p):
        lines = p.read_text(encoding="utf-8").splitlines()
        n = 0
        for i, line in enumerate(lines):
            if line.startswith("- [ ]") and needle in line:
                lines[i] = "- [x]" + line[5:] + f"  — resolved automatically {now_iso()}: {note}"
                n += 1
        if n:
            write_atomic(p, "\n".join(lines) + "\n")
    return n


def resolve_from_inbox(cfg: Config) -> list[tuple[str, str]]:
    """Close open requests that the human answered with an inbox file named after them
    (e.g. `inbox/2026-10-16-A016-decision.md`). Agents cannot tick ACTION_NEEDED themselves,
    so answered requests used to stay open (seen 2026-10-16)."""
    inbox = cfg.layout.inbox
    if not inbox.is_dir():
        return []
    named: dict[str, str] = {}
    for f in sorted(inbox.glob("*.md")):
        for aid in re.findall(r"(?<![A-Za-z0-9])(A\d{3,})(?![0-9])", f.name):
            named.setdefault(aid, f"00_human/inbox/{f.name}")
    out = []
    for line in open_actions(cfg):
        m = re.match(r"- \[ \] \*\*(A\d+)\*\*", line)
        if m and m.group(1) in named:
            resolve_actions(cfg, f"**{m.group(1)}**", f"answered by the human in {named[m.group(1)]}")
            out.append((m.group(1), named[m.group(1)]))
    return out


def open_actions(cfg: Config) -> list[str]:
    p = cfg.layout.action_needed
    if not p.is_file():
        return []
    return [line for line in p.read_text(encoding="utf-8").splitlines() if line.startswith("- [ ]")]


# ---------------------------------------------------------------- uploads

def _upload_items(cfg: Config) -> list[Path]:
    up = cfg.layout.uploads
    if not up.is_dir():
        return []
    return sorted(p for p in up.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def validate_upload(folder: Path) -> list[str]:
    meta_path = folder / UPLOAD_META
    if not meta_path.is_file():
        return [f"missing {UPLOAD_META} (copy 00_human/uploads/_TEMPLATE/{UPLOAD_META})"]
    try:
        meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        return [f"{UPLOAD_META} is not valid TOML: {e}"]
    problems = [f"missing field '{k}'" for k in UPLOAD_REQUIRED if not meta.get(k)]
    if meta.get("kind") and meta["kind"] not in UPLOAD_KINDS:
        problems.append(f"kind must be one of {sorted(UPLOAD_KINDS)}")
    if meta.get("use_as") and meta["use_as"] not in UPLOAD_USES:
        problems.append(f"use_as must be one of {sorted(UPLOAD_USES)}")
    if any(PLACEHOLDER in str(v) for v in meta.values()):
        problems.append("template placeholders still present")
    if not [p for p in folder.rglob("*") if p.is_file() and p.name != UPLOAD_META]:
        problems.append("no data files next to meta.toml")
    return problems


def scan_uploads(cfg: Config) -> dict:
    """Register new or changed human uploads as U-items; ask the human to fix bad metadata."""
    from autolab.util import next_id
    registry = read_jsonl(cfg.layout.uploads_registry)
    by_path = {r["path"]: r for r in registry}
    flagged_file = cfg.layout.state / "uploads_flagged.json"
    flagged = read_json(flagged_file, {}) or {}
    added, invalid = [], []
    for folder in _upload_items(cfg):
        rel = folder.relative_to(cfg.layout.root).as_posix()
        digest = sha256_tree(folder)
        known = by_path.get(rel)
        if known and known["hash"] == digest:
            continue
        problems = validate_upload(folder)
        if problems:
            invalid.append({"path": rel, "problems": problems})
            if flagged.get(rel) != digest:          # ask once per version of the folder
                request_action(cfg, "data", f"Upload `{rel}` needs fixing: " + "; ".join(problems))
                flagged[rel] = digest
                write_json(flagged_file, flagged)
            continue
        meta = tomllib.loads((folder / UPLOAD_META).read_text(encoding="utf-8"))
        rec = {"id": known["id"] if known else next_id(cfg.layout.ids, "upload"),
               "path": rel, "hash": digest, "registered": now_iso(),
               "files": sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")
                               if p.is_file() and p.name != UPLOAD_META),
               **{k: meta.get(k) for k in ("title", "kind", "description", "provenance",
                                           "use_as", "keywords", "units", "conditions",
                                           "date", "confidential")}}
        append_jsonl(cfg.layout.uploads_registry, rec)
        added.append(rec)
    return {"added": added, "invalid": invalid}


def uploads(cfg: Config) -> list[dict]:
    """Latest registry record per upload path."""
    latest: dict[str, dict] = {}
    for r in read_jsonl(cfg.layout.uploads_registry):
        latest[r["path"]] = r
    return list(latest.values())


def pdf_drop_files(cfg: Config) -> list[Path]:
    d = cfg.layout.pdf_drop
    return sorted(p for p in d.glob("*") if p.is_file() and p.suffix.lower() == ".pdf") if d.is_dir() else []


def file_hash(p: Path) -> str:
    return sha256_file(p)
