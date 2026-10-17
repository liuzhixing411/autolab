"""Rebuildable audit index of durable research records, never approval authority.

Source writes precede indexing. A later sync repairs missed indexing after a crash.
Atomic replacement commits the entire logical append, avoiding torn JSONL tails.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
from autolab.util import FileLock, now_iso, read_json, read_jsonl, write_atomic

def collect(cfg):
    root = cfg.layout.root
    for pattern, kind in (("experiments/*/outcomes.jsonl", "experiment.outcome"),
                          ("state/human_notifications.jsonl", "human.progress")):
        for path in sorted(root.glob(pattern)):
            for index, row in enumerate(read_jsonl(path)):
                yield kind, path.relative_to(root).as_posix(), str(index), row
    for pattern, kind in (("claims/*/gate.json", "claim.gate"),
                          ("state/pi_logs/*.execution.json", "turn.execution")):
        for path in sorted(root.glob(pattern)):
            yield kind, path.relative_to(root).as_posix(), "", read_json(path)
    path = cfg.layout.action_needed
    if path.exists():
        import re
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"- \[( |x)\] \*\*(A\d+)\*\*", line)
            if match:
                yield "human.action", path.relative_to(root).as_posix(), match[2], {
                    "action_id": match[2], "resolved": match[1] == "x", "text": line}
    # Read committed delivery state only; never copy credentials or context tokens.
    from autolab.weixin import directory
    state = read_json(directory(cfg) / "state.json", {}) or {}
    for bucket, kind in (("events", "notification.event"), ("deliveries", "notification.delivery"), ("replies", "human.reply")):
        for key, row in state.get(bucket, {}).items():
            safe = {k: row[k] for k in ("aid", "kind", "version", "status", "sent", "failures", "last_attempt", "retry_at", "members") if k in row}
            yield kind, "weixin/" + bucket, key, safe

def sync(cfg):
    path = cfg.layout.state / "events.jsonl"
    with FileLock(path):
        history = read_jsonl(path)
        latest = {(row["kind"], row["source"], row["identity"]): row["fingerprint"] for row in history}
        added = []
        for kind, source, identity, data in collect(cfg):
            payload = dict(kind=kind, source=source, identity=identity, data=data)
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            key = (kind, source, identity)
            if latest.get(key) == digest:
                continue
            latest[key] = digest
            added.append(dict(schema=1, seq=len(history) + len(added) + 1,
                              id=f"EV{len(history) + len(added) + 1:08d}", fingerprint=digest, observed_at=now_iso(), **payload))
        if added:
            write_atomic(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in history + added))
        return added

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["sync", "list"])
    a = p.parse_args()
    from autolab.config import Config
    cfg = Config(Path.cwd())
    if a.action == "sync":
        print(json.dumps({"indexed": len(sync(cfg))}))
    else:
        for row in read_jsonl(cfg.layout.state / "events.jsonl"):
            print(json.dumps(row, ensure_ascii=False))

if __name__ == "__main__":
    main()
