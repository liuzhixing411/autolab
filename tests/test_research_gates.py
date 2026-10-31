"""0.7: usage limits, literature tools, novelty audits, due rules, skill routing. No network or model calls."""
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_autolab as fixtures
from test_autolab import make_project, passing_plan, run_cli, write
from autolab import (budget, due, execution, fulltext, guard, handoff, hooks, knowledge, novelty, project,
                     report, research, reviewer, scholar, skills, tasks, tick)
from autolab.util import append_jsonl, now_iso, read_json, read_jsonl, write_json

LIMIT_LOG = "\n".join(json.dumps(x) for x in (
    {"type": "system", "subtype": "init"},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1792929600,
                                                     "rateLimitType": "seven_day", "overageStatus": "rejected"}},
    {"type": "assistant", "error": "rate_limit", "api_error_status": "429", "message": {"model": "<synthetic>"}},
    {"type": "result", "subtype": "success", "is_error": True, "api_error_status": "429", "total_cost_usd": 0,
     "result": "You've hit your weekly limit"})) + "\n"


class ReviewerQuota(unittest.TestCase):
    REFUSED = "You've hit your weekly limit · resets 7am (America/Indiana/Indianapolis)"       # verbatim, 2026-10-31

    def setUp(self):
        self.root, self.cfg = make_project()
        self.cfg.data["reviewer"]["backend"] = "codex"
        self.cfg.data["triggers"]["enabled"] = True
        self.f = write(self.root / "input.md", "input")

    def provider(self, err=REFUSED):
        which = patch.object(reviewer.shutil, "which", return_value="codex")
        which.start()
        self.addCleanup(which.stop)
        return patch.object(reviewer.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=err))

    def test_a_refusal_pauses_reviews_and_tells_the_human_once(self):
        with self.provider() as call:
            with self.assertRaises(reviewer.ReviewerLimit):
                reviewer.run(self.cfg, "claim_support", [self.f])
            with self.assertRaisesRegex(reviewer.ReviewerLimit, "paused"):
                reviewer.run(self.cfg, "claim_support", [self.f])
        self.assertEqual(call.call_count, 1)                    # the second attempt never reaches the provider
        self.assertEqual(len(read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")), 1)
        self.assertIn("reviewer_limit", [i["key"] for i in due.compute(self.cfg)])
        rc, out = run_cli("--project", str(self.root), "review", "limit", "clear")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(reviewer.limit_status(self.cfg))

    def test_the_pause_ends_by_itself(self):
        rec = reviewer.record_limit(self.cfg, "quota", now=1000.0)
        self.assertEqual(rec["until"], 1000.0 + 6 * 3600)
        self.assertIsNotNone(reviewer.limit_status(self.cfg, now=1060.0))
        self.assertIsNone(reviewer.limit_status(self.cfg, now=rec["until"] + 1))

    def test_other_failures_do_not_pause(self):
        with self.provider(err="segmentation fault"):
            with self.assertRaisesRegex(RuntimeError, "reviewer failed"):
                reviewer.run(self.cfg, "claim_support", [self.f])
        self.assertIsNone(reviewer.limit_status(self.cfg))

    def test_a_review_from_the_labs_own_vendor_stays_due(self):
        self.cfg.data["reviewer"].update(backend="mock", independence="same_vendor")
        subject = {"audit": "N001", "fingerprint": "abc"}
        rec = reviewer.run(self.cfg, "novelty", [self.f], subject=subject)
        self.assertEqual(rec["independence"], "same_vendor")
        self.assertNotIn(f"cross_review:{rec['id']}", [i["key"] for i in due.compute(self.cfg)])   # no other vendor set up yet
        self.cfg.data["reviewer"]["independence"] = "cross_vendor"
        item = next(i for i in due.compute(self.cfg) if i["key"] == f"cross_review:{rec['id']}")
        self.assertIn("review run novelty --audit N001", item["do"])
        reviewer.run(self.cfg, "novelty", [self.f], subject=subject)
        self.assertFalse([i for i in due.compute(self.cfg) if i["key"].startswith("cross_review")])


class UsageLimit(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        self.log = write(self.cfg.layout.state / "pi_logs" / "t.jsonl", LIMIT_LOG)

    def test_rejected_window_is_recorded_once_and_expires(self):
        self.assertEqual(execution.usage_limit(self.log), {"window": "seven_day", "resets_at": 1792929600})
        now = 1792929600 - 3600
        rec = execution.record_limit(self.cfg, self.log, now=now)
        self.assertEqual(rec["until"], 1792929600)
        execution.record_limit(self.cfg, self.log, now=now + 60)
        notes = read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")
        self.assertEqual(len(notes), 1)                       # one notice per window, not one per turn
        self.assertIn("7 天", notes[0]["text"])
        self.assertIsNotNone(execution.limit_status(self.cfg, now=now + 60))
        self.assertIsNone(execution.limit_status(self.cfg, now=1792929600 + 1))
        self.assertFalse((self.cfg.layout.state / "usage_limit.json").exists())

    def test_ordinary_turn_records_nothing(self):
        ok = write(self.cfg.layout.state / "pi_logs" / "ok.jsonl", json.dumps(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 1792929600}}) + "\n")
        self.assertIsNone(execution.record_limit(self.cfg, ok))

    def test_missing_reset_time_waits_a_fallback_period(self):
        log = write(self.cfg.layout.state / "pi_logs" / "x.jsonl",
                    json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}) + "\n")
        rec = execution.record_limit(self.cfg, log, now=1000.0)
        self.assertEqual(rec["until"], 1000.0 + execution.LIMIT_FALLBACK_S)

    def test_cli_exit_codes_and_call_status(self):
        rc, _ = run_cli("--project", str(self.root), "limit", "check")
        self.assertEqual(rc, 0)
        write_json(self.cfg.layout.state / "usage_limit.json", {"until": time.time() + 600, "window": "five_hour"})
        rc, out = run_cli("--project", str(self.root), "limit", "check")
        self.assertEqual(rc, 3)
        self.assertRegex(out, r"^\d{4}-\d{2}-\d{2}T")
        self.assertEqual(budget.turn_info(self.log)["status"], "rate_limited")
        self.assertFalse(hooks.decide({"tool_name": "Bash", "tool_input": {"command": "autolab limit record x"}}, self.root)[0])


class FakeWeb:
    """Answers `_http` from a list of (substring, status, json). Records every URL."""

    def __init__(self, routes):
        self.routes, self.urls, self.headers = routes, [], []

    def __call__(self, url, headers, limit=None):
        self.urls.append(url)
        self.headers.append(headers)
        for key, status, body in self.routes:
            if key in url:
                if isinstance(body, list):
                    status, body = body.pop(0) if len(body) > 1 else body[0]
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return status, {"X-RateLimit-Remaining-USD": "0.9"}, raw
        return 404, {}, b""


class Literature(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        keys = write(Path(tempfile.mkdtemp()) / "keys.env", 'export SEMANTIC_SCHOLAR_API_KEY="s2-SECRET"\nOPENALEX_API_KEY=oa-SECRET\nCONTACT_EMAIL=a@b.c\n')
        self.cfg.data["literature"].update(keys_file=str(keys), s2_min_interval_s=0, backoff_s=0, tries=3,
                                           queue_dir=tempfile.mkdtemp())

    def web(self, routes):
        fake = FakeWeb(routes)
        p = patch.object(scholar, "_http", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def test_keys_never_reach_logs_cache_or_results(self):
        self.web([("/paper/search", 200, {"data": [{"title": "A", "externalIds": {"DOI": "10.1/a"}}]}),
                  ("/works", 200, {"results": []})])
        res = scholar.search(self.cfg, "bubbles")
        self.assertEqual(res["hits"][0]["doi"], "10.1/a")
        for f in self.cfg.layout.literature.rglob("*"):
            if f.is_file():
                self.assertNotIn("SECRET", f.read_text(encoding="utf-8", errors="ignore"), f)

    def test_search_merges_sources_and_marks_the_index(self):
        knowledge.lit_add(self.cfg, "Tracking bubbles in simulations", "10.1/known")
        self.web([("/paper/search", 200, {"data": [{"title": "Tracking bubbles in simulations", "externalIds": {"DOI": "10.1/KNOWN"}, "year": 2020},
                                                   {"title": "Only in S2", "externalIds": {}}]}),
                  ("/works", 200, {"results": [{"id": "https://openalex.org/W1", "title": "Tracking bubbles in simulations",
                                                "doi": "https://doi.org/10.1/known", "publication_year": 2020}]})])
        res = scholar.search(self.cfg, "bubble tracking", since=2024)
        self.assertEqual(res["hits"][0]["lid"], "L0001")
        self.assertEqual(sorted(res["hits"][0]["sources"]), ["openalex", "s2"])
        self.assertEqual(len(res["hits"]), 2)
        row = scholar.search_log(self.cfg)[-1]
        self.assertEqual((row["kind"], row["hits"], row["failed"], row["since"]), ("search", 2, [], 2024))
        self.assertEqual(read_json(self.cfg.layout.literature / "searches" / f"{res['id']}.json")["since"], 2024)

    def test_rate_limited_source_is_retried_then_reported(self):
        fake = self.web([("/paper/search", 200, [(429, {}), (429, {}), (200, {"data": [{"title": "Late"}]})]),
                         ("/works", 200, {"results": []})])
        res = scholar.search(self.cfg, "x")
        self.assertEqual(res["failed"], [])
        self.assertEqual(sum("/paper/search" in u for u in fake.urls), 3)
        self.assertEqual(scholar.search_log(self.cfg)[-1]["http"]["s2:429"], 2)
        self.web([("/paper/search", 429, {}), ("/works", 200, {"results": []})])
        res = scholar.search(self.cfg, "y")
        self.assertEqual(res["failed"], ["s2"])
        self.assertIn("WARNING: no answer from s2", scholar.render(res))

    def test_cache_answers_a_repeated_query(self):
        fake = self.web([("/paper/search", 200, {"data": []}), ("/works", 200, {"results": []})])
        scholar.search(self.cfg, "same")
        first = len(fake.urls)
        scholar.search(self.cfg, "same")
        self.assertEqual(len(fake.urls), first)
        scholar.search(self.cfg, "same", fresh=True)
        self.assertEqual(len(fake.urls), first * 2)

    def test_budget_floor_stops_with_a_clean_message(self):
        with patch.object(scholar, "_http", lambda u, h, limit=None: (200, {"X-RateLimit-Remaining-USD": "0.001"}, b"{}")):
            with self.assertRaisesRegex(scholar.ScholarError, "allowance") as e:
                scholar.search(self.cfg, "x")
        self.assertNotIn("SECRET", str(e.exception))

    def test_snowball_ranks_by_seed_links_and_finds_a_paper_with_no_shared_words(self):
        knowledge.lit_add(self.cfg, "Seed one", "10.1/s1")
        knowledge.lit_add(self.cfg, "Seed two", "10.1/s2")
        wid = "https://openalex.org/"
        gem = {"id": wid + "W9", "title": "Droplet genealogies", "publication_year": 2019, "doi": "https://doi.org/10.1/gem"}
        noise = {"id": wid + "W8", "title": "An unrelated topic", "publication_year": 2000}
        self.web([("/works/doi:10.1/s1", 200, {"id": wid + "W1", "title": "Seed one", "referenced_works": [wid + "W9", wid + "W8"], "related_works": []}),
                  ("/works/doi:10.1/s2", 200, {"id": wid + "W2", "title": "Seed two", "referenced_works": [wid + "W9"], "related_works": []}),
                  ("openalex%3AW9%7CW8", 200, {"results": [gem, noise]}), ("openalex%3AW9", 200, {"results": [gem]}),
                  ("cites%3A", 200, {"results": [], "meta": {}})])
        res = scholar.snowball(self.cfg, ["L0001", "L0002"], keywords=["bubble"])
        self.assertEqual([h["title"] for h in res["hits"]], ["Droplet genealogies"])       # two seeds beat no keyword
        self.assertEqual(res["hits"][0]["seeds"], 2)
        self.assertEqual(scholar.search_log(self.cfg)[-1]["not_in_index"], 1)
        all_rows = scholar.snowball(self.cfg, ["L0001", "L0002"])                           # the file keeps every row
        self.assertIn("1 more rows", scholar.render(all_rows, limit=1))
        self.assertEqual(len(read_json(self.cfg.layout.literature / "searches" / f"{all_rows['id']}.json")["hits"]), 2)

    def test_one_paper_is_registered_once(self):
        first = knowledge.lit_add(self.cfg, "Extraction of droplet genealogies", "10.1/one")
        self.assertEqual(knowledge.lit_add(self.cfg, "EXTRACTION of droplet genealogies.", "10.1/two")["id"], first["id"])
        self.assertEqual(knowledge.lit_add(self.cfg, "Another title", "10.1/one")["id"], first["id"])

    def test_a_new_note_claims_no_reading_level(self):
        rec = knowledge.lit_add(self.cfg, "A paper", "10.1/x")
        knowledge.note_template(self.cfg, rec["id"])
        self.assertEqual(knowledge.receipt(self.cfg, rec["id"])["read_level"], "")

    def test_snowball_falls_back_to_the_title_when_a_doi_is_unknown(self):
        knowledge.lit_add(self.cfg, "Extraction of droplet genealogies", "10.1/other-doi")
        wid = "https://openalex.org/"
        self.web([("/works/doi:", 404, {}),
                  ("title.search", 200, {"results": [{"id": wid + "W7", "title": "EXTRACTION OF DROPLET GENEALOGIES", "referenced_works": [], "related_works": []}]}),
                  ("cites%3A", 200, {"results": [{"id": wid + "W6", "title": "Citing paper"}], "meta": {}})])
        res = scholar.snowball(self.cfg, ["L0001"])
        self.assertEqual(res["missing"], [])
        self.assertEqual(res["hits"][0]["title"], "Citing paper")

    def test_fetch_takes_an_open_pdf_and_rejects_html(self):
        knowledge.lit_add(self.cfg, "Open paper", "10.1/open")
        self.web([("/works/doi:", 200, {"id": "W1", "best_oa_location": {"pdf_url": "https://host/a.pdf"}, "locations": [{"pdf_url": "https://host/b.pdf"}]}),
                  ("/graph/v1/paper/DOI", 404, {}), ("unpaywall", 404, {}),
                  ("host/a.pdf", 200, b"<html>login page</html>"), ("host/b.pdf", 200, b"%PDF-1.4 tiny")])
        res = scholar.fetch(self.cfg, "L0001")
        self.assertEqual((res["status"], res["source"]), ("fetched", "openalex"))
        rec = next(r for r in knowledge.lit_records(self.cfg) if r["id"] == "L0001")
        self.assertEqual((rec["access"], rec["url"]), ("have_pdf", "https://host/b.pdf"))
        self.assertTrue((self.cfg.layout.literature / "pdf" / "L0001.pdf").is_file())
        self.assertEqual(scholar.fetch(self.cfg, "L0001")["status"], "have_pdf")

    def test_fetch_missing_marks_what_it_tried(self):
        knowledge.lit_add(self.cfg, "Closed paper", "10.1/closed")
        self.web([("/works/doi:", 200, {"id": "W1"}), ("/graph/v1/paper/DOI", 404, {}), ("unpaywall", 404, {})])
        res = scholar.fetch_missing(self.cfg)
        self.assertEqual(res["counts"], {"no_open_copy": 1})
        self.assertFalse(due._literature(self.cfg, 0))

    def test_watch_reports_only_new_citing_papers(self):
        knowledge.lit_add(self.cfg, "Key paper", "10.1/key", tier="A", access="have_pdf")
        knowledge.lit_add(self.cfg, "Already here", "10.1/here")
        wid = "https://openalex.org/"
        self.web([("/works/doi:", 200, {"id": wid + "W1", "title": "Key paper"}),
                  ("cites%3A", 200, {"results": [{"id": wid + "W2", "title": "Brand new", "publication_year": 2026},
                                                 {"id": wid + "W3", "title": "Already here", "doi": "https://doi.org/10.1/here"}], "meta": {}})])
        res = scholar.watch(self.cfg, tick=7, fresh=True)
        self.assertEqual([h["title"] for h in res["hits"]], ["Brand new"])
        self.assertEqual(scholar.watch(self.cfg, tick=8, fresh=True)["hits"], [])

    def test_agents_cannot_reach_the_key_file(self):
        project._write_settings(self.cfg)
        settings = json.loads((self.cfg.layout.claude / "settings.json").read_text(encoding="utf-8"))
        path = self.cfg["literature"]["keys_file"]
        self.assertTrue(any(path.lstrip("/").replace("\\", "/") in r for r in settings["permissions"]["deny"]))
        self.cfg.data["agents"]["sandbox"] = True
        project._write_settings(self.cfg)
        settings = json.loads((self.cfg.layout.claude / "settings.json").read_text(encoding="utf-8"))
        self.assertTrue(any("keys.env" in p for p in settings["sandbox"]["filesystem"]["denyRead"]))
        for cmd in ("cat ~/.scholar_keys.env", "python -c \"open('/home/x/.scholar_keys.env')\""):
            self.assertFalse(hooks.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, self.root)[0], cmd)
        self.assertTrue(hooks.decide({"tool_name": "Bash", "tool_input": {"command": 'autolab lit search "bubbles"'}}, self.root)[0])
        self.assertFalse(hooks.decide({"tool_name": "Write", "tool_input": {"file_path": "literature/search_log.jsonl"}}, self.root)[0])


class FullText(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        rec = knowledge.lit_add(self.cfg, "On digital images", tier="B")
        self.lid = rec["id"]
        write(self.cfg.layout.literature / "text" / f"{self.lid}.txt",
              "# header\n\n===== page 1 =====\n\nIntroduction.\n\n===== page 2 =====\n\n"
              "Removing a simple point keeps every bubble connected, so no breakup search runs.\n")

    def test_pack_finds_a_paper_by_its_text_and_shows_the_page(self):
        pack = knowledge.build_context_pack(self.cfg, "bubble breakup", ["bubble", "breakup"])
        item = next(i for i in pack["items"] if i["id"] == self.lid)
        self.assertIn("p.2", item["passage"])
        self.assertIn("[", item["passage"])
        md = (self.cfg.layout.context_packs / f"{pack['id']}.md").read_text(encoding="utf-8")
        self.assertIn("p.2", md)

    def test_one_shared_term_is_not_enough(self):
        pack = knowledge.build_context_pack(self.cfg, "bubble chemistry", ["bubble"])
        self.assertNotIn(self.lid, [i["id"] for i in pack["items"]])

    def test_index_follows_changes_and_old_packs_stay_as_they_were(self):
        self.assertIn(self.lid, fulltext.search(self.cfg, ["simple point"]))
        f = self.cfg.layout.literature / "text" / f"{self.lid}.txt"
        write(f, "===== page 1 =====\n\nNothing relevant here.\n")
        import os
        os.utime(f, (time.time() + 5, time.time() + 5))
        self.assertNotIn(self.lid, fulltext.search(self.cfg, ["simple point"]))
        write(f, "===== page 1 =====\n\nbubble breakup\n")
        os.utime(f, (time.time() + 9, time.time() + 9))
        self.assertEqual(knowledge._select(self.cfg, {"bubble", "breakup"}, False), [])   # pack made before 0.7
        self.assertTrue(knowledge._select(self.cfg, {"bubble", "breakup"}, True))


def judge(sid, paper, degree="genuinely_new", level="full", quote=""):
    row = {"sid": sid, "paper": paper, "read_level": level,
           "types": {"constraint_limitation": {"degree": degree, "known": "k", "introduced": "i", "novel": "n"}}}
    return {**row, "quote": quote} if quote else row


class NoveltyAudit(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        self.cfg.data["gates"]["require_novelty"] = True
        p = patch.object(guard, "_dir", side_effect=lambda cfg: cfg.layout.root.parent / "guard")
        p.start()
        self.addCleanup(p.stop)
        for i in range(1, 7):
            knowledge.lit_add(self.cfg, f"Bubble breakup paper {i}", access="have_pdf")
        for lid in ("L0001", "L0002"):
            write(self.cfg.layout.lit_notes / f"{lid}.md", '+++\nid = "x"\nread_level = "full"\n+++\n\n## Key points\n\n'
                  f"- Result {lid[-1]}: bubble breakup follows a Weber number law across every tested regime of jet speed.\n")
        write(self.cfg.layout.literature / "text" / "L0001.txt", "===== page 1 =====\n\nThe bubble breakup law is well\nknown here.\n")
        research.new_candidate(self.cfg, "H1", "Breakup rule", origin="pi", closest_work="L0001", mechanism_difference="m",
                               key_uncertainty="u", discriminating_experiment="d", continue_if="c", kill_if="k")

    def open(self, **kw):
        return novelty.new(self.cfg, "H1", ["Our seed rule keeps bubble breakup partitions exact"], "constraint_limitation", **kw)

    def searched(self, created=None):
        log = self.cfg.layout.literature / "search_log.jsonl"
        for row in ({"kind": "snowball", "candidates": 30, "failed": []}, {"kind": "search", "hits": 9, "failed": []},
                    {"kind": "snippets", "hits": 5, "failed": []}):
            append_jsonl(log, {"id": "S0001", "at": now_iso(), **row})

    def fill(self, audit, decoy_degree="explicit_established"):
        secret = read_json(novelty._secret(self.cfg, audit["id"]))
        rows = []
        for s in audit["statements"]:
            for p in ("L0001", "L0002", "L0003", "L0004", "L0005"):
                known = s["sid"] in secret["decoys"] and p == "L0001"
                rows.append(judge(s["sid"], p, decoy_degree if known else "genuinely_new",
                                  quote="bubble breakup law is well known" if known else ""))
        d = self.cfg.layout.novelty / audit["id"]
        write(d / "judgments.jsonl", "\n".join(json.dumps(r) for r in rows) + "\n")
        write(d / "report.md", "## What was searched\nsnowball\n## Closest prior work\nL0001\n## Positioning\nd\n## Risks\nr\n")
        return secret

    def review(self, nid, **body):
        if body:
            write(self.cfg.layout.novelty / nid / "mock_review.json", json.dumps(body))
        rc, out = run_cli("--project", str(self.root), "review", "run", "novelty", "--audit", nid)
        self.assertEqual(rc, 0, out)

    def test_control_statements_do_not_show_where_they_come_from(self):
        self.assertEqual(novelty._plain("Upper bound A (p.2, §3): the planar method of L0018 needs O(log n) per update"),
                         "The planar method of needs O(log n) per update")
        self.assertEqual(novelty._plain("Enumerated over neighbours (order up to a cap of 6-10; Sec 5.3 p.12-13). Then filtered"),
                         "Enumerated over neighbours. Then filtered")

    def test_quotes_survive_ligatures_and_line_breaks(self):
        self.assertEqual(novelty._clean("the eﬃcient de-\n tection of ﬁne\n  structure"), "the efficient detection of fine structure")

    def test_only_claims_become_controls(self):
        for junk in ("none stated (no MPI, threads, or timing section).",
                     "volume filter (Eqs. 5.1-5.3, worst-case child count), proximity filter (neighbour list), continuity filter with volume",
                     "The paper is read only as an abstract here, so its detailed method needs a later full text pass"):
            self.assertFalse(novelty._is_claim(novelty._plain(junk)), junk)
        self.assertTrue(novelty._is_claim(novelty._plain(
            "Abstract (p.1): any algorithm that maintains the connected components of a digital image must take Omega(log n / log log n) time per change")))

    def test_controls_are_mixed_in_blind(self):
        a = self.open()
        self.assertEqual(len(a["statements"]), 3)
        secret = read_json(novelty._secret(self.cfg, a["id"]))
        self.assertEqual(len(secret["decoys"]), 2)
        self.assertEqual(len(secret["real"]), 1)
        text = (self.cfg.layout.novelty / a["id"] / "audit.json").read_text(encoding="utf-8")
        self.assertNotIn("decoy", text.lower())
        self.assertNotIn("real", text.lower())

    def test_no_notes_means_no_audit_unless_a_reason_is_given(self):
        for lid in ("L0001", "L0002"):
            (self.cfg.layout.lit_notes / f"{lid}.md").unlink()
        with self.assertRaisesRegex(ValueError, "control statement"):
            self.open()
        a = self.open(no_decoy_why="first audit of the project, human approved")
        self.assertEqual(len(a["statements"]), 1)

    def test_check_needs_search_coverage_and_real_quotes(self):
        a = self.open()
        res = novelty.check(self.cfg, a["id"])
        self.assertFalse(res["ok"])
        self.assertTrue(any("snowball" in p for p in res["problems"]))
        self.assertTrue(any("judged against 0 papers" in p for p in res["problems"]))
        self.searched()
        self.fill(a)
        self.assertEqual(novelty.check(self.cfg, a["id"])["problems"], [])
        # a quote that is not in the paper
        f = self.cfg.layout.novelty / a["id"] / "judgments.jsonl"
        f.write_text(f.read_text(encoding="utf-8").replace("law is well known", "law was never studied"), encoding="utf-8")
        self.assertTrue(any("found verbatim" in p for p in novelty.check(self.cfg, a["id"])["problems"]))

    def test_a_missed_control_voids_the_audit_without_naming_it(self):
        a = self.open()
        self.searched()
        self.fill(a, decoy_degree="genuinely_new")
        res = novelty.check(self.cfg, a["id"])
        msg = next(p for p in res["problems"] if "control statements" in p)
        self.assertNotRegex(msg, r"\d")                         # neither which ones nor how many
        self.assertFalse(res["ok"])

    def test_failed_search_does_not_count(self):
        a = self.open()
        append_jsonl(self.cfg.layout.literature / "search_log.jsonl",
                     {"id": "S0002", "at": now_iso(), "kind": "search", "hits": 0, "failed": ["s2", "openalex"]})
        self.assertTrue(any("catalogue search" in p for p in novelty.check(self.cfg, a["id"])["problems"]))

    def test_close_needs_a_review_of_this_version_and_keeps_the_limits(self):
        a = self.open()
        nid = a["id"]
        self.searched()
        secret = self.fill(a)
        with self.assertRaisesRegex(ValueError, "review of this version"):
            novelty.close(self.cfg, nid, "PROCEED", "L0002", "no paper restricts the seeds")
        self.review(nid)
        with self.assertRaisesRegex(ValueError, "ABANDON must name"):
            novelty.close(self.cfg, nid, "ABANDON", "L0002", "crowded field")
        with self.assertRaisesRegex(ValueError, "CAUTION needs --why"):
            novelty.close(self.cfg, nid, "CAUTION", "L0002", "delta")
        rec = novelty.close(self.cfg, nid, "PROCEED", "L0002", "no paper restricts the seeds")
        self.assertEqual(rec["verdict"], "PROCEED")
        self.assertEqual([s["sid"] for s in rec["statements"]], secret["real"])       # controls stay out of the record
        self.assertEqual(research.candidate(self.cfg, "H1")["novelty_audit"], nid)
        self.assertIn(nid, (self.cfg.layout.state / "CONTRIBUTIONS.md").read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            novelty.close(self.cfg, nid, "PROCEED", "L0002", "again")

    def test_established_statement_cannot_proceed_and_abandon_names_the_paper(self):
        a = self.open()
        nid = a["id"]
        self.searched()
        secret = self.fill(a)
        f = self.cfg.layout.novelty / nid / "judgments.jsonl"
        rows = read_jsonl(f)
        for r in rows:
            if r["sid"] == secret["real"][0] and r["paper"] == "L0001":
                r["types"]["constraint_limitation"]["degree"] = "explicit_established"
                r["quote"] = "bubble breakup law is well known"
        write(f, "\n".join(json.dumps(r) for r in rows) + "\n")
        self.review(nid)
        with self.assertRaisesRegex(ValueError, "PROCEED is closed"):
            novelty.close(self.cfg, nid, "PROCEED", "L0001", "delta")
        rec = novelty.close(self.cfg, nid, "ABANDON", "L0001", "L0001 already has it")
        self.assertEqual(rec["verdict"], "ABANDON")

    def test_failed_review_stops_close(self):
        a = self.open()
        self.searched()
        self.fill(a)
        self.review(a["id"], verdict="no", grade=None, issues=[])
        fp = novelty.fingerprint(self.cfg, a["id"])
        self.assertEqual(novelty.reviews(self.cfg, a["id"])[-1]["subject"]["fingerprint"], fp)
        (self.cfg.layout.novelty / a["id"] / "mock_review.json").unlink()
        with self.assertRaisesRegex(ValueError, "does not hold"):
            novelty.close(self.cfg, a["id"], "PROCEED", "L0002", "d")

    def test_major_issues_stop_close_unless_caution_names_them(self):
        a = self.open()
        self.searched()
        self.fill(a)
        self.review(a["id"], verdict="partial", grade="C", issues=[{"id": "N03", "severity": "major", "text": "too vague to verify"}])
        (self.cfg.layout.novelty / a["id"] / "mock_review.json").unlink()
        with self.assertRaisesRegex(ValueError, "unresolved issues"):
            novelty.close(self.cfg, a["id"], "PROCEED", "L0002", "d")
        with self.assertRaisesRegex(ValueError, "name each open issue"):
            novelty.close(self.cfg, a["id"], "CAUTION", "L0002", "d", "thin delta")
        rec = novelty.close(self.cfg, a["id"], "CAUTION", "L0002", "d", "N03 stands: the repair rule is not specified")
        self.assertEqual(rec["verdict"], "CAUTION")

    def test_a_paper_registered_during_the_audit_is_judged_or_explained(self):
        a = self.open()
        self.searched()
        self.fill(a)
        rec = knowledge.lit_add(self.cfg, "A late find", "10.1/late")
        self.assertTrue(any(rec["id"] in w and "not judged" in w for w in novelty.check(self.cfg, a["id"])["warnings"]))

    def test_plan_gate_wants_an_audit_for_a_new_line_and_big_plans(self):
        res, _ = passing_plan(self.cfg)
        self.assertTrue(any("no novelty audit" in p for p in res["problems"]), res["problems"])
        self.assertTrue(any("new line of work" in p for p in res["problems"]))
        a = self.open()
        self.searched()
        self.fill(a)
        self.review(a["id"])
        research.set_field(self.cfg, "H1", "novelty_audit", a["id"])
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertTrue(any("not closed" in p for p in res["problems"]))
        novelty.close(self.cfg, a["id"], "PROCEED", "L0002", "delta")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertFalse([p for p in res["problems"] if "novelty" in p], res["problems"])

    def test_small_follow_up_needs_no_audit_but_a_big_one_does(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace('hypotheses = ["H1"]', 'hypotheses = ["H1"]\nparent = "E000"'), encoding="utf-8")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertFalse([p for p in res["problems"] if "novelty" in p])
        plan.write_text(plan.read_text(encoding="utf-8").replace("cpus = 4", "cpus = 64"), encoding="utf-8")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertTrue(any("core-hours" in p for p in res["problems"]), res["problems"])

    def test_abandon_stops_a_plan_until_overridden_with_a_reason(self):
        a = self.open()
        nid = a["id"]
        d = self.cfg.layout.novelty / nid
        write_json(d / "verdict.json", {"verdict": "ABANDON", "at": now_iso()})
        research.set_field(self.cfg, "H1", "novelty_audit", nid)
        res, _ = passing_plan(self.cfg)
        self.assertTrue(any("says ABANDON" in p for p in res["problems"]))
        novelty.override(self.cfg, nid, "the human wants the comparison anyway")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertFalse([p for p in res["problems"] if "ABANDON" in p])
        self.assertIn("continue despite ABANDON", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))

    def test_audit_records_are_protected(self):
        for path in ("state/novelty/N001/verdict.json", "state/novelty/N001/audit.json", "state/due.json"):
            self.assertFalse(hooks.decide({"tool_name": "Write", "tool_input": {"file_path": path}}, self.root)[0], path)
        self.assertTrue(hooks.decide({"tool_name": "Write", "tool_input": {"file_path": "state/novelty/N001/judgments.jsonl"}}, self.root)[0])


class DueRules(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        self.cfg.data["triggers"]["enabled"] = True
        for name in ("H1", "H2", "H3"):
            research.new_candidate(self.cfg, name, f"Candidate {name}", origin="pi")

    def outcome(self, eid, value="refine", hypothesis="refutes", at=None):
        write_json(self.cfg.layout.experiments / eid / "outcome.json",
                   {"experiment": eid, "at": at or now_iso(), "engineering": "valid", "hypothesis": hypothesis, "value": value})

    def test_a_stalled_line_requires_an_ideation_round_and_blocks_plans(self):
        self.outcome("E001", at="2026-01-01T00:00:00+00:00")
        self.outcome("E002", at="2026-01-02T00:00:00+00:00")
        self.assertEqual(due.compute(self.cfg), [])
        self.outcome("E003", at="2026-01-03T00:00:00+00:00")
        items = due.compute(self.cfg)
        self.assertEqual(items[0]["key"], "ideation:E001")
        self.assertIn("brainstorming-research-ideas", items[0]["do"])
        self.assertTrue(due.plan_blockers(self.cfg))
        res, _ = passing_plan(self.cfg)
        self.assertTrue(any("ideation:E001" in p for p in res["problems"]), res["problems"])

    def test_progress_resets_the_streak(self):
        self.outcome("E001", at="2026-01-01T00:00:00+00:00")
        self.outcome("E002", at="2026-01-02T00:00:00+00:00")
        self.outcome("E003", value="continue", hypothesis="supports", at="2026-01-03T00:00:00+00:00")
        self.outcome("E004", at="2026-01-04T00:00:00+00:00")
        self.assertEqual(due.compute(self.cfg), [])

    def test_the_round_closes_only_with_its_record_and_review(self):
        for i, eid in enumerate(("E001", "E002", "E003"), 1):
            self.outcome(eid, at=f"2026-01-0{i}T00:00:00+00:00")
        idea = write(self.cfg.layout.state / "ideation" / "2026-01-05-next.md", "H1, H2 and H3 compared\n")
        self.assertEqual(len(due.compute(self.cfg)), 1)                   # no review yet
        write_json(self.cfg.layout.reviews / "R0001.json", {"task": "idea_review", "created": now_iso()})
        self.assertLess(idea.stat().st_mtime, time.time() + 1)
        self.assertEqual(due.compute(self.cfg), [])

    def test_a_new_project_starts_with_alternatives(self):
        root, cfg = make_project()
        cfg.data["triggers"]["enabled"] = True
        self.assertEqual(due.compute(cfg)[0]["key"], "ideation:start")

    def test_waiver_needs_a_reason_and_is_on_record(self):
        for i, eid in enumerate(("E001", "E002", "E003"), 1):
            self.outcome(eid, at=f"2026-01-0{i}T00:00:00+00:00")
        with self.assertRaises(ValueError):
            due.waive(self.cfg, "ideation:E001", " ")
        due.waive(self.cfg, "ideation:E001", "the line ends with this report")
        self.assertEqual(due.compute(self.cfg), [])
        self.assertIn("waived due item", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        with self.assertRaises(KeyError):
            due.waive(self.cfg, "nothing", "x")

    def test_findings_call_for_a_value_review(self):
        for i in range(1, 4):
            write_json(self.cfg.layout.findings / f"F00{i}" / "finding.json", {"id": f"F00{i}", "accepted": now_iso()})
        items = due.compute(self.cfg)
        self.assertEqual(items[0]["key"], "value:F001")
        write_json(self.cfg.layout.reviews / "R0001.json", {"task": "value_grade", "created": "2999-01-01T00:00:00+00:00"})
        self.assertEqual(due.compute(self.cfg), [])

    def test_literature_rules_do_not_block(self):
        knowledge.lit_add(self.cfg, "A paper")
        keys = [i["key"] for i in due.compute(self.cfg, tick=25)]
        self.assertEqual(keys, ["lit_watch:0", "lit_fetch"])
        self.assertEqual(due.plan_blockers(self.cfg), [])

    def test_digest_opens_with_what_must_be_done(self):
        for i, eid in enumerate(("E001", "E002", "E003"), 1):
            self.outcome(eid, at=f"2026-01-0{i}T00:00:00+00:00")
        res = tick.run(self.cfg)
        text = Path(res["digest"]).read_text(encoding="utf-8")
        self.assertIn("## Must do now", text)
        heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads[0], "## Must do now")
        self.assertTrue((self.cfg.layout.state / "CONTRIBUTIONS.md").is_file())


class SkillRouting(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def test_unknown_task_is_refused_with_the_valid_names(self):
        with self.assertRaisesRegex(ValueError, "no route for task 'draft'.*writing"):
            tasks.new(self.cfg, "Report", "writing", task="draft")

    def test_every_student_default_reaches_a_required_skill(self):
        for student, skill in (("literature", "research-lit"), ("experiment", "run-experiment"), ("analysis", "analyze-results"),
                               ("figure", "scientific-visualization"), ("writing", "research-paper-writing")):
            meta = fixtures.read_frontmatter(tasks.new(self.cfg, f"Card {student}", student))[0]
            self.assertIn(skill, meta["skills"], student)

    def test_a_task_finds_its_route_without_the_stage(self):
        meta = fixtures.read_frontmatter(tasks.new(self.cfg, "Write section", "writing", task="section"))[0]
        self.assertIn("paper-write", meta["skills"])
        self.assertEqual(skills.resolve(self.cfg, "student", "polish", None), ["humanizer"])

    def test_new_routes(self):
        meta = fixtures.read_frontmatter(tasks.new(self.cfg, "Plan E9", "analysis", task="plan"))[0]
        self.assertIn("statistical-power", meta["skills"])
        meta = fixtures.read_frontmatter(tasks.new(self.cfg, "Audit", "literature", task="novelty"))[0]
        self.assertIn("novelty-audit", meta["skills"])

    def card(self, task, surprise, used):
        d = tasks.new(self.cfg, "Run it", "analysis", task=task).parent
        write(d / "results" / "metrics.json", '{"x": 1}')
        return write(d / "handoff.md", f'''+++
task_id = "T"
status = "done"
plan_followed = "yes"
confidence = "medium"
needs_pi_decision = false
surprise = {str(surprise).lower()}
skills_used = {json.dumps(used)}
+++

## Result
done

## Deviations from plan
none

## Anomalies
none

## Suggested next steps
none
''')

    def test_a_surprising_result_requires_debugging_first(self):
        base = ["verification-before-completion", "analyze-results"]
        res = handoff.validate_in(self.cfg, self.card("analysis", True, base))
        self.assertTrue(any("systematic-debugging" in p for p in res["problems"]), res["problems"])
        res = handoff.validate_in(self.cfg, self.card("analysis", True, base + ["systematic-debugging"]))
        self.assertFalse([p for p in res["problems"] if "systematic-debugging" in p])
        res = handoff.validate_in(self.cfg, self.card("analysis", False, base))
        self.assertFalse([p for p in res["problems"] if "systematic-debugging" in p])

    def test_literature_tasks_must_use_the_literature_tools(self):
        d = tasks.new(self.cfg, "Prior art", "literature").parent
        probs = skills.missing_artifacts(self.cfg, ["research-lit"], (d / "task.md").stat().st_mtime - 60)
        self.assertTrue(any("autolab lit search" in p and "snowball" in p for p in probs), probs)
        for kind in ("search", "snowball"):
            append_jsonl(self.cfg.layout.literature / "search_log.jsonl", {"id": "S0001", "at": now_iso(), "kind": kind})
        self.assertEqual(skills.missing_artifacts(self.cfg, ["research-lit"], (d / "task.md").stat().st_mtime - 60), [])

    def test_agents_get_situations_not_menus(self):
        text = (self.cfg.layout.agents / "student-experiment.md").read_text(encoding="utf-8")
        self.assertIn("`systematic-debugging`: a run fails", text)
        self.assertNotIn("experiment-queue", text)
        self.assertNotIn("invoke any that fit", text)
        pi = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("| ideation | `brainstorming-research-ideas`", pi)

    def test_listing_keeps_descriptions_for_skills_with_a_job(self):
        settings = json.loads((self.cfg.layout.claude / "settings.json").read_text(encoding="utf-8"))
        over = settings["skillOverrides"]
        self.assertEqual(over["experiment-queue"], "name-only")
        self.assertNotIn("novelty-audit", over)
        self.assertNotIn("brainstorming-research-ideas", over)
        self.assertGreater(settings["skillListingBudgetFraction"], 0.01)
        how = skills.activation(self.cfg)
        self.assertEqual(how["run-experiment"], "gate")
        self.assertEqual(how["web-debug-search"], "situation")
        self.assertEqual(how["kill-argument"], "reviewer")
        self.assertEqual(how["skill-creator"], "human")
        self.assertEqual(how["experiment-queue"], "library")

    def test_codex_steps_are_translated_at_mount(self):
        kill = (self.cfg.layout.skills_mount / "kill-argument" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("do not exist in this lab", kill)
        run = (self.cfg.layout.skills_mount / "run-experiment" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("do not exist in this lab", run)
        self.assertNotIn("do not exist in this lab", (fixtures.FW / "core/skills/kill-argument/SKILL.md").read_text(encoding="utf-8"))

    def test_doctor_names_what_is_broken(self):
        rows = {r["skill"]: r for r in skills.doctor(self.cfg)}
        self.assertTrue(rows["novelty-check"]["codex_step"])
        self.assertGreater(rows["novelty-check"]["ml_terms"], 0)
        self.assertEqual(rows["research-lit"]["use"], "gate")
        self.assertFalse(rows["analyze-results"]["codex_step"])

    def test_metrics_report_origins_and_skills(self):
        res, _ = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res["problems"])
        m = report.metrics(self.cfg)
        self.assertEqual(m["ideas"]["candidates_by_origin"], {"pi": 1})
        self.assertIsNone(m["ideas"]["lab_originated_share"])          # unknown, not zero
        self.assertEqual(m["novelty"]["audits"], 0)
        rc, out = run_cli("--project", str(self.root), "skills", "report")
        self.assertEqual(rc, 0)
        self.assertIn("run-experiment", out)
        rc, out = run_cli("--project", str(self.root), "skills", "doctor")
        self.assertEqual(rc, 0)
        self.assertIn("novelty-check", out)


class CentralClaims(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ClaimGate()
        self.case.setUp()
        self.cfg, self.root = self.case.cfg, self.case.root

    def central(self, cid, extra='novelty = "none-claimed"\n'):
        res = self.case.claim(cid)
        d = self.cfg.layout.claims / cid
        text = (d / "claim.md").read_text(encoding="utf-8")
        (d / "claim.md").write_text(text.replace('pi_grade = "B"\n', f'pi_grade = "B"\ncentral = true\n{extra}'), encoding="utf-8")
        return res

    def test_a_central_claim_needs_a_kill_argument_on_this_version(self):
        self.central("C001")
        rc, out = run_cli("--project", str(self.root), "review", "run", "claim_support", "--claim", "C001")
        self.assertEqual(rc, 0, out)
        from autolab import gates
        res = gates.claim_gate(self.cfg, "C001")
        self.assertEqual(res["decision"], "needs_review")
        self.assertIn("kill_argument", res["reasons"][0])
        rc, out = run_cli("--project", str(self.root), "review", "run", "kill_argument", "--claim", "C001", "--central")
        self.assertEqual(rc, 0, out)
        self.assertEqual(gates.claim_gate(self.cfg, "C001")["decision"], "accepted")

    def test_a_central_claim_states_its_novelty(self):
        self.cfg.data["gates"]["require_novelty"] = True
        self.central("C002", extra="")
        from autolab import gates
        res = gates.claim_gate(self.cfg, "C002")
        self.assertEqual(res["decision"], "blocked")
        self.assertTrue(any("novelty" in r for r in res["reasons"]))

    def test_unknown_paper_in_prior_work_blocks(self):
        d = self.cfg.layout.claims / "C003"
        write(d / "mock_review.json", "{}")
        write(d / "claim.md", '+++\nstatement = "x"\nexperiments = ["E001"]\nevidence = ["experiments/E001/results/metrics.json"]\n'
              'pi_grade = "B"\n+++\n\n## Closest prior work\n| L0099 | invented | x | y |\n')
        from autolab import gates
        self.assertTrue(any("L0099" in p for p in gates.precheck(self.cfg, "C003")["problems"]))


if __name__ == "__main__":
    unittest.main()
