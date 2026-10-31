"""Full-text search over the papers and reading notes the lab holds.

Context packs used to match only titles and keywords, so a paper the lab had already read stayed out
of a pack unless its title shared a word with the topic (a real run needed `--include` by hand).
SQLite FTS5 ships with Python and adds no dependency. The index is a cache: delete it and it rebuilds.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from autolab.config import Config
from autolab.util import FileLock

_PAGE = re.compile(r"^===== page (\d+) =====$", re.M)
SCHEMA = ("CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL);"
          "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(path UNINDEXED, lid UNINDEXED, page UNINDEXED, "
          "body, tokenize='porter unicode61');")


def _db(cfg: Config) -> Path:
    return cfg.layout.literature / "fulltext.sqlite"


def _connect(path: Path) -> sqlite3.Connection:
    """`nolock`: POSIX locks are unreliable on cluster file systems; FileLock serialises access."""
    con = sqlite3.connect(f"{path.resolve().as_uri()}?nolock=1", uri=True)
    con.executescript(SCHEMA)
    return con


def _pages(text: str) -> list[tuple[int, str]]:
    marks = list(_PAGE.finditer(text))
    if not marks:
        return [(0, text)]
    return [(int(m.group(1)), text[m.end():marks[i + 1].start() if i + 1 < len(marks) else len(text)])
            for i, m in enumerate(marks)]


def _refresh(cfg: Config, con: sqlite3.Connection) -> int:
    lit = cfg.layout.literature
    files = sorted((lit / "text").glob("L*.txt")) + sorted(cfg.layout.lit_notes.glob("L*.md"))
    known = dict(con.execute("SELECT path, mtime FROM files"))
    changed = 0
    for f in files:
        rel, mtime = f.relative_to(lit).as_posix(), f.stat().st_mtime
        if known.pop(rel, None) == mtime:
            continue
        con.execute("DELETE FROM pages WHERE path = ?", (rel,))
        for page, body in _pages(f.read_text(encoding="utf-8", errors="ignore")):
            con.execute("INSERT INTO pages(path, lid, page, body) VALUES (?, ?, ?, ?)", (rel, f.stem, page, body))
        con.execute("INSERT OR REPLACE INTO files(path, mtime) VALUES (?, ?)", (rel, mtime))
        changed += 1
    for rel in known:                                   # the file is gone
        con.execute("DELETE FROM pages WHERE path = ?", (rel,))
        con.execute("DELETE FROM files WHERE path = ?", (rel,))
    con.commit()
    return changed


def search(cfg: Config, terms: list[str], per_term: int = 40) -> dict[str, dict]:
    """lid -> {terms: distinct query terms found, page, snippet}. Each term runs on its own, so a
    paper is ranked by how much of the topic it covers rather than by one frequent word. Returns {}
    where SQLite has no FTS5: packs then fall back to titles and keywords."""
    db = _db(cfg)
    if not (cfg.layout.literature / "text").is_dir() and not cfg.layout.lit_notes.is_dir():
        return {}
    hits: dict[str, dict] = {}
    try:
        with FileLock(db):
            con = _connect(db)
            try:
                _refresh(cfg, con)
                for term in terms:
                    q = '"' + term.replace('"', '""') + '"'
                    rows = con.execute("SELECT lid, page, snippet(pages, 3, '[', ']', ' ... ', 18), bm25(pages) FROM pages "
                                       "WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?", (q, per_term)).fetchall()
                    for lid, page, snip, rank in rows:
                        h = hits.setdefault(lid, {"seen": set(), "rank": rank, "page": page, "snippet": snip})
                        h["seen"].add(term.lower())
                        if rank < h["rank"]:
                            h.update(rank=rank, page=page, snippet=snip)
            finally:
                con.close()
    except sqlite3.Error:
        return {}
    return {k: {"terms": len(v["seen"]), "page": v["page"], "snippet": " ".join(v["snippet"].split())}
            for k, v in hits.items()}


def available() -> bool:
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(SCHEMA)
        return True
    except sqlite3.Error:
        return False
    finally:
        con.close()
