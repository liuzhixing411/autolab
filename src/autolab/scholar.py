"""`autolab lit` search commands: Semantic Scholar, OpenAlex and open full text.

first-test showed what the old path (web search plus a fetcher nobody called) costs: Semantic
Scholar answered 429 on the first day and was never tried again, the closest comparator reached the
lab on day 5 through the human, and 17 of the 53 papers without full text had an open PDF. A
retrospective test (2026-10-31) showed keyword search alone would not have fixed it, while a
one-hop citation snowball from the day-0 seed papers ranked that comparator 10th of 210.

The agents never see the keys: `autolab` runs outside the OS sandbox and reads the key file in this
process, so keys stay out of the environment, logs, cache and error messages. Semantic Scholar calls
of all projects queue on one lock (a key allows about one call per second). Every command leaves a
row in literature/search_log.jsonl, the record gates and reviewers read.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from autolab.config import Config
from autolab.util import (FileLock, append_jsonl, next_id, now_iso, overlap, read_json, read_jsonl,
                          write_json)

S2_API = "https://api.semanticscholar.org"
OA_API = "https://api.openalex.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
S2_FIELDS = "title,year,venue,externalIds,citationCount,openAccessPdf"
OA_FIELDS = "id,title,publication_year,doi,cited_by_count,primary_location,best_oa_location,locations"
OA_LINKS = OA_FIELDS + ",referenced_works,related_works"
KEY_NAMES = {"s2": "SEMANTIC_SCHOLAR_API_KEY", "openalex": "OPENALEX_API_KEY"}
RETRY = (-1, 429, 500, 502, 503, 504)
MAX_PDF_BYTES = 80 * 1024 * 1024
MAX_CITING = 300


class ScholarError(RuntimeError):
    """Raised with a message that is safe to print: it never contains a URL or a key."""


def keys(cfg: Config) -> dict[str, str]:
    """KEY=value lines of the key file; a missing file means anonymous, slower access."""
    out: dict[str, str] = {}
    p = Path(cfg["literature"]["keys_file"]).expanduser()
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


def _http(url: str, headers: dict[str, str], limit: int = MAX_PDF_BYTES) -> tuple[int, dict, bytes]:
    """One GET: (status, headers, body), status -1 on a network failure. Tests replace this."""
    req = urllib.request.Request(url, headers={"User-Agent": "autolab (research literature tool)", **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, dict(r.headers), r.read(limit + 1)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), b""
    except (urllib.error.URLError, OSError, ValueError):
        return -1, {}, b""


class Client:
    """Cached, paced JSON GETs. `get` returns None when the source did not answer, so a command can
    report a failed source instead of an empty result."""

    def __init__(self, cfg: Config, fresh: bool = False):
        self.cfg, self.fresh = cfg, fresh
        self.opts = cfg["literature"]
        self.keys = keys(cfg)
        self.http: Counter = Counter()
        self.cache = cfg.layout.literature / "cache"
        self.queue = Path(self.opts["queue_dir"]).expanduser() / "s2"

    def _pace(self) -> None:
        """Hold the lock for the whole gap, so concurrent commands go one at a time."""
        gap = float(self.opts["s2_min_interval_s"])
        if not gap:
            return
        stamp = self.queue.with_suffix(".json")
        with FileLock(self.queue, timeout_s=600.0):
            wait = float((read_json(stamp, {}) or {}).get("t", 0)) + gap - time.time()
            if wait > 0:
                time.sleep(wait)
            write_json(stamp, {"t": time.time()})

    def get(self, source: str, path: str, params: dict) -> dict | None:
        tag = json.dumps([source, path, sorted(params.items())], default=str)
        f = self.cache / f"{hashlib.sha256(tag.encode()).hexdigest()[:24]}.json"
        ttl = float(self.opts["cache_days"]) * 86400
        if not self.fresh and f.is_file() and time.time() - f.stat().st_mtime < ttl:
            self.http[f"{source}:cached"] += 1
            return read_json(f)
        query, headers = dict(params), {}
        secret = self.keys.get(KEY_NAMES[source], "")
        if source == "s2":
            base = S2_API
            if secret:
                headers["x-api-key"] = secret
        else:
            base = OA_API
            if secret:
                query["api_key"] = secret
            mail = self.keys.get("CONTACT_EMAIL", "")
            if mail:
                query["mailto"] = mail
        url = f"{base}{path}?{urllib.parse.urlencode(query)}"
        delay = float(self.opts["backoff_s"])
        for attempt in range(int(self.opts["tries"])):
            if source == "s2":
                self._pace()
            code, head, body = _http(url, headers)
            self.http[f"{source}:{code}"] += 1
            if code == 200:
                self._check_budget(source, head)
                try:
                    data = json.loads(body.decode("utf-8", "ignore"))
                except json.JSONDecodeError:
                    return None
                write_json(f, data)
                return data
            if code not in RETRY:
                return None
            if attempt + 1 < int(self.opts["tries"]):
                after = {k.lower(): v for k, v in head.items()}.get("retry-after", "")
                time.sleep(float(after) if str(after).isdigit() else delay)
                delay = min(delay * 2, 60.0)
        return None

    def _check_budget(self, source: str, head: dict) -> None:
        """OpenAlex meters a daily dollar allowance; stop before it runs out mid-search."""
        if source != "openalex":
            return
        left = {k.lower(): v for k, v in head.items()}.get("x-ratelimit-remaining-usd")
        try:
            if left is not None and float(left) < float(self.opts["openalex_usd_floor"]):
                raise ScholarError(f"OpenAlex daily allowance nearly used up ({left} USD left); try again tomorrow")
        except ValueError:
            pass


# ---------------------------------------------------------------- the lab's own index

def norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _bare_doi(doi: str | None) -> str:
    return (doi or "").lower().replace("https://doi.org/", "").strip()


class Index:
    """Matches a hit to a literature record by DOI, then by the start of the title: one paper can
    carry two DOIs (first-test L0079 in the index and in OpenAlex)."""

    def __init__(self, cfg: Config):
        from autolab.knowledge import lit_records
        self.records = {r["id"]: r for r in lit_records(cfg)}
        self.by_doi = {_bare_doi(r.get("doi")): r["id"] for r in self.records.values() if r.get("doi")}
        self.by_title = {norm_title(r["title"])[:45]: r["id"] for r in self.records.values()}

    def find(self, title: str | None, doi: str | None) -> str | None:
        return self.by_doi.get(_bare_doi(doi)) or self.by_title.get(norm_title(title)[:45])


def _s2_row(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    return {"title": p.get("title"), "year": p.get("year"), "doi": ext.get("DOI") or "", "venue": p.get("venue") or "",
            "cites": p.get("citationCount"), "arxiv": ext.get("ArXiv") or "",
            "pdf": (p.get("openAccessPdf") or {}).get("url") or ""}


def _oa_row(w: dict) -> dict:
    loc = (w.get("primary_location") or {}).get("source") or {}
    return {"title": w.get("title"), "year": w.get("publication_year"), "doi": _bare_doi(w.get("doi")),
            "venue": loc.get("display_name") or "", "cites": w.get("cited_by_count"), "openalex": w.get("id") or "",
            "pdf": (w.get("best_oa_location") or {}).get("pdf_url") or ""}


def _record(cfg: Config, kind: str, client: Client, row: dict, result: dict) -> dict:
    """One row in the search log, full results next to it."""
    sid = next_id(cfg.layout.ids, "search", width=4)
    stats = {**{k: v for k, v in row.items() if k != "audit" or v}, "http": dict(client.http)}
    append_jsonl(cfg.layout.literature / "search_log.jsonl", {"id": sid, "at": now_iso(), "kind": kind, **stats})
    out = {"id": sid, "kind": kind, **result}
    write_json(cfg.layout.literature / "searches" / f"{sid}.json", out)
    return out


def search_log(cfg: Config) -> list[dict]:
    return read_jsonl(cfg.layout.literature / "search_log.jsonl")


# ---------------------------------------------------------------- search and snippets

def search(cfg: Config, query: str, limit: int = 20, since: int | None = None, audit: str = "",
           fresh: bool = False) -> dict:
    """Both catalogues, merged by reciprocal rank. A source that did not answer is reported, not hidden."""
    c, idx = Client(cfg, fresh), Index(cfg)
    s2 = {"query": query, "limit": min(limit, 100), "fields": S2_FIELDS}
    oa = {"search": query, "per-page": min(limit, 100), "select": OA_FIELDS}
    if since:
        s2["year"] = f"{since}-"
        oa["filter"] = f"from_publication_date:{since}-01-01"
    lists = {"s2": c.get("s2", "/graph/v1/paper/search", s2), "openalex": c.get("openalex", "/works", oa)}
    rows = {"s2": [_s2_row(p) for p in (lists["s2"] or {}).get("data") or []],
            "openalex": [_oa_row(w) for w in (lists["openalex"] or {}).get("results") or []]}
    failed = sorted(k for k, v in lists.items() if v is None)
    merged: dict[str, dict] = {}
    for source, hits in rows.items():
        for rank, r in enumerate(hits, 1):
            key = _bare_doi(r["doi"]) or norm_title(r["title"])
            if not key:
                continue
            m = merged.setdefault(key, {**r, "score": 0.0, "sources": []})
            m["score"] += 1 / (60 + rank)
            m["sources"].append(source)
            for k, v in r.items():
                m[k] = m.get(k) or v
    hits = sorted(merged.values(), key=lambda m: -m["score"])
    for h in hits:
        h["lid"] = idx.find(h["title"], h["doi"])
        h["score"] = round(h["score"], 5)
    return _record(cfg, "search", c, {"query": query, "since": since, "audit": audit, "failed": failed,
                                      "counts": {k: len(v) for k, v in rows.items()}, "hits": len(hits)},
                   {"query": query, "since": since, "failed": failed, "hits": hits})


def snippets(cfg: Config, query: str, limit: int = 20, audit: str = "", fresh: bool = False) -> dict:
    """Passages from paper bodies: how a method is really described, not how its title reads."""
    c, idx = Client(cfg, fresh), Index(cfg)
    d = c.get("s2", "/graph/v1/snippet/search", {"query": query, "limit": min(limit, 100),
                                                 "fields": "snippet.text,snippet.section,snippet.snippetKind"})
    hits = []
    for h in (d or {}).get("data") or []:
        p, s = h.get("paper") or {}, h.get("snippet") or {}
        hits.append({"title": p.get("title"), "corpus_id": p.get("corpusId"), "section": s.get("section"),
                     "kind": s.get("snippetKind"), "text": str(s.get("text") or "")[:900], "score": h.get("score"),
                     "lid": idx.find(p.get("title"), None)})
    failed = ["s2"] if d is None else []
    return _record(cfg, "snippets", c, {"query": query, "audit": audit, "failed": failed, "hits": len(hits)},
                   {"query": query, "failed": failed, "hits": hits})


# ---------------------------------------------------------------- citation snowball

def _title_query(title: str) -> str:
    return re.sub(r"[^\w ]+", " ", title).strip()[:200]


def _work(c: Client, doi: str, title: str, fields: str = OA_LINKS) -> dict | None:
    if doi:
        w = c.get("openalex", "/works/doi:" + urllib.parse.quote(_bare_doi(doi)), {"select": fields})
        if w:
            return w
    if title:
        d = c.get("openalex", "/works", {"filter": "title.search:" + _title_query(title), "per-page": 5, "select": fields})
        for w in (d or {}).get("results") or []:
            if norm_title(w.get("title"))[:45] == norm_title(title)[:45]:
                return w
    return None


def _by_ids(c: Client, ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 50):
        chunk = "|".join(x.rsplit("/", 1)[-1] for x in ids[i:i + 50])
        d = c.get("openalex", "/works", {"filter": "openalex:" + chunk, "per-page": 50, "select": OA_FIELDS})
        out += (d or {}).get("results") or []
    return out


def _citing(c: Client, wid: str, since: str = "") -> list[dict]:
    short = wid.rsplit("/", 1)[-1]
    flt = f"cites:{short}" + (f",from_publication_date:{since}" if since else "")
    out, cursor = [], "*"
    while cursor and len(out) < MAX_CITING:
        d = c.get("openalex", "/works", {"filter": flt, "per-page": 100, "cursor": cursor, "select": OA_FIELDS})
        page = (d or {}).get("results") or []
        out += page
        cursor = (d or {}).get("meta", {}).get("next_cursor") if page else None
    return out


def snowball(cfg: Config, seeds: list[str], keywords: list[str] | None = None, audit: str = "",
             fresh: bool = False) -> dict:
    """One hop from each seed: its references, the papers citing it, OpenAlex's related works.
    Papers linked to several seeds rank first. Citation links reach work whose title shares no word
    with the query, which is how first-test's closest comparator would have been found."""
    c, idx = Client(cfg, fresh), Index(cfg)
    found, missing, own = {}, [], set()
    for seed in seeds:
        rec = idx.records.get(seed.upper())
        doi, title = (rec.get("doi", ""), rec["title"]) if rec else (seed, "")
        w = _work(c, doi, title)
        if not w:
            missing.append(seed)
            continue
        own.add(w["id"])
        hops = (("back", _by_ids(c, w.get("referenced_works") or [])), ("forward", _citing(c, w["id"])),
                ("related", _by_ids(c, w.get("related_works") or [])))
        for how, works in hops:
            for x in works:
                if x.get("title") and x.get("id"):
                    found.setdefault(x["id"], {**_oa_row(x), "links": []})["links"].append(f"{seed}:{how}")
    if missing and len(missing) == len(seeds):
        raise ScholarError(f"no seed could be resolved in OpenAlex: {', '.join(missing)}")
    kw = [k for k in keywords or [] if k.strip()]
    rows = []
    for wid, r in found.items():
        if wid in own:
            continue
        r["seeds"] = len({ln.split(":")[0] for ln in r["links"]})
        r["match"] = overlap(kw, r["title"]) if kw else 0
        r["lid"] = idx.find(r["title"], r["doi"])
        if not kw or r["match"] or r["seeds"] >= 2:
            rows.append(r)
    rows.sort(key=lambda r: (-r["seeds"], -r["match"], -(r.get("cites") or 0)))
    new = [r for r in rows if not r["lid"]]
    return _record(cfg, "snowball", c, {"seeds": seeds, "audit": audit, "missing": missing, "keywords": kw,
                                        "candidates": len(found), "kept": len(rows), "not_in_index": len(new)},
                   {"seeds": seeds, "missing": missing, "candidates": len(found), "kept": len(rows), "hits": rows})


# ---------------------------------------------------------------- open full text

def _pdf_urls(c: Client, rec: dict) -> list[tuple[str, str]]:
    """Legal open copies, best first: arXiv, OpenAlex locations, Semantic Scholar, Unpaywall."""
    out, doi = [], _bare_doi(rec.get("doi"))
    arxiv = re.match(r"10\.48550/arxiv\.(.+)$", doi)
    if arxiv:
        out.append(("arxiv", f"https://arxiv.org/pdf/{arxiv.group(1)}"))
    w = _work(c, doi, rec.get("title", ""), OA_FIELDS)
    for loc in ([(w or {}).get("best_oa_location") or {}] + list((w or {}).get("locations") or [])):
        if loc.get("pdf_url"):
            out.append(("openalex", loc["pdf_url"]))
    if doi:
        p = c.get("s2", f"/graph/v1/paper/DOI:{urllib.parse.quote(doi)}", {"fields": "openAccessPdf,externalIds"})
        if p:
            if (p.get("openAccessPdf") or {}).get("url"):
                out.append(("s2", p["openAccessPdf"]["url"]))
            if (p.get("externalIds") or {}).get("ArXiv"):
                out.append(("arxiv", f"https://arxiv.org/pdf/{p['externalIds']['ArXiv']}"))
        mail = c.keys.get("CONTACT_EMAIL", "")
        if mail:
            code, _h, body = _http(f"{UNPAYWALL_API}/{urllib.parse.quote(doi)}?{urllib.parse.urlencode({'email': mail})}", {})
            c.http[f"unpaywall:{code}"] += 1
            if code == 200:
                u = json.loads(body.decode("utf-8", "ignore"))
                for loc in ([u.get("best_oa_location") or {}] + list(u.get("oa_locations") or [])):
                    if loc.get("url_for_pdf"):
                        out.append(("unpaywall", loc["url_for_pdf"]))
    return list(dict.fromkeys(out))


def fetch(cfg: Config, lid: str, client: Client | None = None) -> dict:
    """Download a legal open copy into literature/pdf and extract its text. Papers with no open
    copy stay as they are; a tier A one is requested from the human by `lit add`, as before."""
    from autolab import knowledge
    c = client or Client(cfg)
    rec = next((r for r in knowledge.lit_records(cfg) if r["id"] == lid), None)
    if rec is None:
        raise KeyError(f"no literature record {lid}")
    canon = cfg.layout.literature / "pdf" / f"{lid}.pdf"
    if canon.is_file():
        return {"id": lid, "status": "have_pdf"}
    tried = []
    for source, url in _pdf_urls(c, rec):
        code, _h, body = _http(url, {})
        c.http[f"pdf:{code}"] += 1
        tried.append(source)
        if code == 200 and body[:5] == b"%PDF-" and len(body) <= MAX_PDF_BYTES:
            tmp = canon.with_suffix(".download")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(body)
            knowledge.lit_update(cfg, lid, pdf=str(tmp), access="have_pdf", url=url, fetch_tried=now_iso())
            return {"id": lid, "status": "fetched", "source": source, "bytes": len(body)}
    knowledge.lit_update(cfg, lid, fetch_tried=now_iso())
    return {"id": lid, "status": "copy_refused" if tried else "no_open_copy", "tried": tried}


def fetch_missing(cfg: Config, limit: int = 0, fresh: bool = False) -> dict:
    """Every record without full text that was not tried yet, tier A first."""
    from autolab import knowledge
    c = Client(cfg, fresh)
    todo = [r for r in knowledge.lit_records(cfg) if r.get("access") in ("abstract_only", "oa", "human_needed")
            and not r.get("fetch_tried")]
    todo.sort(key=lambda r: (r.get("tier", "C"), r["id"]))
    results = [fetch(cfg, r["id"], c) for r in (todo[:limit] if limit else todo)]
    counts = Counter(r["status"] for r in results)
    return _record(cfg, "fetch", c, {"tried": len(results), "counts": dict(counts)},
                   {"tried": len(results), "counts": dict(counts), "results": results})


# ---------------------------------------------------------------- watch

def watch(cfg: Config, tick: int = 0, fresh: bool = False) -> dict:
    """New papers citing the lab's tier A records since the last watch."""
    c, idx = Client(cfg, fresh), Index(cfg)
    state_file = cfg.layout.literature / "watch.json"
    state = read_json(state_file, {}) or {}
    since = state.get("last") or (date.today() - timedelta(days=365)).isoformat()
    seen, hits, failed = set(state.get("seen", [])), {}, []
    for rec in idx.records.values():
        if rec.get("tier") != "A":
            continue
        w = _work(c, rec.get("doi", ""), rec["title"], "id,title")
        if not w:
            failed.append(rec["id"])
            continue
        for x in _citing(c, w["id"], since):
            if x.get("id") not in seen and not idx.find(x.get("title"), x.get("doi")):
                hits.setdefault(x["id"], {**_oa_row(x), "cites_seed": []})["cites_seed"].append(rec["id"])
    write_json(state_file, {"last": date.today().isoformat(), "tick": tick, "seen": sorted(seen | set(hits))})
    rows = sorted(hits.values(), key=lambda r: (-len(r["cites_seed"]), -(r.get("year") or 0)))
    return _record(cfg, "watch", c, {"since": since, "new": len(rows), "failed": failed},
                   {"since": since, "failed": failed, "hits": rows})


def render(res: dict, width: int = 110, limit: int = 40) -> str:
    """Compact text for agents; the full result stays in literature/searches/<id>.json."""
    head = f"{res['id']} {res['kind']}"
    if res.get("query"):
        head += f' "{res["query"]}"'
    if res.get("seeds"):
        head += f" seeds {', '.join(res['seeds'])}: {res['candidates']} candidates, {res['kept']} kept"
    lines = [head]
    if res.get("failed"):
        lines.append(f"WARNING: no answer from {', '.join(map(str, res['failed']))}; this result is incomplete, retry later")
    if res.get("missing"):
        lines.append(f"WARNING: seeds not found: {', '.join(res['missing'])}")
    for i, h in enumerate(res.get("hits", [])[:limit], 1):
        mark = f"[{h['lid']}] " if h.get("lid") else ""
        how = f"seeds {h['seeds']}" if "seeds" in h else "+".join(h.get("sources", [])) or str(h.get("section") or "")
        lines.append(f"{i:3d}. {mark}{h.get('year') or ''} {str(h.get('title'))[:width]} | {how} | {h.get('doi') or ''}".rstrip(" |"))
        if h.get("text"):
            lines.append("     " + " ".join(h["text"].split())[:width * 2])
    if "results" in res:
        lines += [f"{r['id']}: {r['status']}" for r in res["results"]]
    more = len(res.get("hits", [])) - limit
    lines.append(f"({f'{more} more rows, ' if more > 0 else ''}full result: literature/searches/{res['id']}.json)")
    return "\n".join(lines)
