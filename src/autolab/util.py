"""Small shared helpers: TOML frontmatter, atomic writes, JSONL, hashing, IDs."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_tree(path: Path) -> str:
    """Hash of every file under `path` (names + contents), stable across runs."""
    h = hashlib.sha256()
    if path.is_file():
        h.update(sha256_file(path).encode())
        return h.hexdigest()
    for p in sorted(x for x in path.rglob("*") if x.is_file()):
        h.update(p.relative_to(path).as_posix().encode())
        h.update(sha256_file(p).encode())
    return h.hexdigest()


# ---------------------------------------------------------------- frontmatter

_TOML_FM = re.compile(r"\A\+\+\+\s*\n(.*?)\n\+\+\+\s*\n?", re.S)
_YAML_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


def read_frontmatter(path: Path) -> tuple[dict, str]:
    """Return (metadata, body).

    autolab files use TOML frontmatter between `+++` lines. Files written by ARIS
    (research-wiki pages) use simple `---` YAML; for those only flat `key: value`
    lines are read, which is all autolab needs from them.
    """
    text = path.read_text(encoding="utf-8")
    m = _TOML_FM.match(text)
    if m:
        return tomllib.loads(m.group(1)), text[m.end():]
    m = _YAML_FM.match(text)
    if m:
        meta = {}
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith((" ", "\t", "-")):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip("'\"")
        return meta, text[m.end():]
    return {}, text


def sections(body: str) -> dict[str, str]:
    """Split markdown into {'## heading' text (lowercased, stripped): content}."""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current = line[3:].strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


# ---------------------------------------------------------------- ids

_ID_PREFIX = {"experiment": "E", "claim": "C", "finding": "F", "task": "T",
              "literature": "L", "upload": "U", "context_pack": "CP", "action": "A",
              "issue": "I", "review": "R"}


def next_id(ids_file: Path, kind: str, width: int = 3) -> str:
    counters = read_json(ids_file, {}) or {}
    n = int(counters.get(kind, 0)) + 1
    counters[kind] = n
    write_json(ids_file, counters)
    return f"{_ID_PREFIX[kind]}{n:0{width}d}"


# ---------------------------------------------------------------- text matching

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+\-]{2,}|[\u4e00-\u9fff]{2,}")   # CJK runs count as words
_STOP = {"the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
         "into", "using", "use", "our", "not", "but", "has", "have", "its", "can"}


def tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


def overlap(query: Iterable[str], text: str) -> int:
    q = {t.lower() for t in query}
    return len(q & tokens(text))
