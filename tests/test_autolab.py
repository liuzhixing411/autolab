"""End-to-end interface tests. Standard library only:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

FW = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FW / "src"))
os.environ["AUTOLAB_HOME"] = str(FW)

from autolab import (cli, gates, handoff, hooks, human, knowledge, ledger, project,  # noqa: E402
                     reviewer, skills, slurm, tick)
from autolab.config import Config  # noqa: E402
from autolab.util import read_frontmatter, read_json, read_jsonl, write_json  # noqa: E402

FILLED = {
    "GOAL.md": "# Goal\nPredict bubble breakup in a submerged jet.\n",
    "RULES.md": "# Rules\nNo unpublished data.\n",
    "TASTE.md": "# Taste\nMechanism over accuracy.\n",
}


def make_project(domain="cfd", site="local", backend="mock") -> tuple[Path, Config]:
    tmp = Path(tempfile.mkdtemp(prefix="autolab-test-"))
    project.init(tmp / "proj", "proj", domain, site)
    root = tmp / "proj"
    for name, text in FILLED.items():
        (root / "00_human" / name).write_text(text, encoding="utf-8")
    cfgf = root / "autolab.toml"
    cfgf.write_text(cfgf.read_text(encoding="utf-8").replace('backend = "codex"', f'backend = "{backend}"'),
                    encoding="utf-8")
    (root / "00_human" / "READY").touch()
    return root, Config(root)


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def run_cli(*argv) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


def passing_plan(cfg: Config, eid="E001", topic="bubble breakup jet", keywords=("bubble", "breakup")):
    pack = knowledge.build_context_pack(cfg, topic, list(keywords))
    answers = "\n".join(f"- {i['id']}: considered in detail; this plan uses a different nozzle "
                        f"geometry and checks it explicitly" for i in pack["items"])
    write(cfg.layout.experiments / eid / "plan.md", f"""+++
id = "{eid}"
context_pack = "{pack['id']}"
hypotheses = ["H1"]
hypothesis = "Breakup frequency scales with Weber number"
prediction = "slope 0.5 +- 0.1"
decision_rule = "if slope in range continue, else revisit model"
budget_hours = 2.0
run_command = "echo run"

[resources]
partition = "cpu"
cpus = 4
mem_gb = 8
hours = 1.5
+++

## Prior knowledge consulted
{answers or '- none matched'}

## Precision
Unit: seed. Planned n = 5 seeds; pilot SD of the slope 0.08 gives a 95% CI half-width of about 0.1, enough to decide the rule.
""")
    hyp = cfg.layout.state / "HYPOTHESES.md"
    if not hyp.is_file() or "<<FILL" in hyp.read_text(encoding="utf-8"):
        write(hyp, "| H1 | - | Breakup frequency scales with Weber number | open | - | - | E001 |\n")
    for i in pack["items"]:
        if i["kind"] == "literature" and i["must_read"]:
            note = knowledge.note_template(cfg, i["id"])
            note.write_text(note.read_text(encoding="utf-8").replace(
                "## Key points\n", "## Key points\nBreakup regime map vs Weber number.\n"), encoding="utf-8")
    return knowledge.check_plan(cfg, eid), pack


class InitAndSkills(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def test_layout_and_generated_files(self):
        L = self.cfg.layout
        for d in L.dirs():
            self.assertTrue(d.is_dir(), d)
        settings = json.loads((L.claude / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("pre-tool-use", settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn("Computational fluid dynamics", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        agent = (L.agents / "student-experiment.md").read_text(encoding="utf-8")
        self.assertIn("model: sonnet", agent)
        self.assertIn("`run-experiment`", agent)
        self.assertIn("`uncertainty-and-units`", agent)          # added by the CFD pack
        self.assertNotIn("{{", agent)

    def test_every_routed_skill_exists(self):
        self.assertEqual(skills.check(self.cfg), [])

    def test_mount_applies_core_and_domain_overlays(self):
        L = self.cfg.layout
        m = read_json(L.skills_mount / "MOUNT.json")
        self.assertEqual(m["domain"], "cfd")
        self.assertGreaterEqual(len(m["skills"]), 60)
        run = (L.skills_mount / "run-experiment" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("autolab overlay: core", run)
        tc = (L.skills_mount / "training-check" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("autolab overlay: domain:cfd", tc)
        self.assertTrue((L.skills_mount / "shared-references" / "taste-calibration.md").is_file())
        # vendor copy untouched
        self.assertNotIn("autolab overlay", (FW / "core" / "skills" / "run-experiment" / "SKILL.md").read_text(encoding="utf-8"))

    def test_switch_domain_is_one_line(self):
        cfgf = self.root / "autolab.toml"
        cfgf.write_text(cfgf.read_text(encoding="utf-8").replace('domain = "cfd"', 'domain = "generic"'), encoding="utf-8")
        cfg = Config(self.root)
        project.remount(cfg)
        self.assertEqual(read_json(cfg.layout.skills_mount / "MOUNT.json")["domain"], "generic")
        tc = (cfg.layout.skills_mount / "training-check" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("domain:cfd", tc)
        self.assertNotIn("uncertainty-and-units", skills.resolve(cfg, "student", None, "experiment"))
        self.assertIn("General science", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_resolve_merges_core_and_domain(self):
        names = skills.resolve(self.cfg, "pi", "planning")
        self.assertEqual(names[0], "research-wiki")              # always
        self.assertIn("experiment-plan", names)
        self.assertIn("uncertainty-and-units", names)            # domain route


class HumanZone(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        human.check_changes(self.cfg, now=time.time() + 10_000)   # absorb template filling
        human.ack(self.cfg, "baseline")

    def test_debounce_then_ack(self):
        goal = self.cfg.layout.human / "GOAL.md"
        goal.write_text(goal.read_text(encoding="utf-8") + "\nNew priority: case B first.\n", encoding="utf-8")
        r = human.check_changes(self.cfg, now=time.time())
        self.assertEqual(r["changed"], [])
        self.assertIn("GOAL.md", r["settling"])
        r = human.check_changes(self.cfg, now=time.time() + 600)
        self.assertEqual(r["changed"], ["GOAL.md"])
        self.assertEqual(human.pending_ack(self.cfg), ["GOAL.md"])
        self.assertEqual(human.ack(self.cfg, "re-ordered tasks"), ["GOAL.md"])
        self.assertIn("re-ordered tasks", self.cfg.layout.ack.read_text(encoding="utf-8"))
        self.assertEqual(human.pending_ack(self.cfg), [])

    def test_ready_applies_initial_input_immediately(self):
        snap = read_json(self.cfg.layout.human_snapshot)
        snap.pop("_ready_seen", None)
        write_json(self.cfg.layout.human_snapshot, snap)
        goal = self.cfg.layout.human / "GOAL.md"
        goal.write_text(goal.read_text(encoding="utf-8") + "\nfinal wording\n", encoding="utf-8")
        r = human.check_changes(self.cfg, now=time.time())
        self.assertIn("GOAL.md", r["changed"])
        self.assertEqual(r["settling"], [])

    def test_unfilled_templates_detected(self):
        write(self.cfg.layout.human / "RULES.md", "<<FILL: rules>>")
        self.assertEqual(human.unfilled_templates(self.cfg), ["RULES.md"])

    def test_upload_lifecycle(self):
        up = self.cfg.layout.uploads / "2026-10-20_piv"
        write(up / "data.csv", "x,u\n0,1\n")
        r = human.scan_uploads(self.cfg)
        self.assertEqual(len(r["invalid"]), 1)
        human.scan_uploads(self.cfg)                              # same folder → asked only once
        self.assertEqual(len([l for l in human.open_actions(self.cfg) if "piv" in l]), 1)
        write(up / "meta.toml", 'title = "PIV of bubble jet"\nkind = "experimental_data"\n'
                                'description = "velocity field"\nprovenance = "lab rig 2"\n'
                                'use_as = "constraint"\nkeywords = ["bubble", "jet"]\n')
        r = human.scan_uploads(self.cfg)
        self.assertEqual(r["added"][0]["id"], "U001")
        self.assertEqual(human.scan_uploads(self.cfg)["added"], [])


class KnowledgeGate(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        L = self.cfg.layout
        knowledge.lit_add(self.cfg, "Bubble breakup in turbulent jets", "10.1/abc", 2020, "A", "theory",
                          "have_pdf", ["bubble", "breakup", "jet"])
        knowledge.lit_add(self.cfg, "Unrelated combustion paper", "10.1/zzz", 2019, "B", "background",
                          "oa", ["flame"])
        write(L.wiki / "ideas" / "idea-weber-scaling-v1.md",
              "---\ntitle: Weber scaling with round nozzle\noutcome: negative\n---\nBubble breakup did not follow Weber scaling.\n")
        write(L.rejected / "C009" / "claim.md", '+++\nstatement = "Jet breakup independent of bubble size"\n+++\nbubble breakup\n')
        up = L.uploads / "exp1"
        write(up / "d.csv", "a\n1\n")
        write(up / "meta.toml", 'title = "breakup frequencies"\nkind = "experimental_data"\n'
                                'description = "measured"\nprovenance = "lab"\nuse_as = "constraint"\n')
        human.scan_uploads(self.cfg)

    def test_pack_contains_all_kinds_and_must_reads(self):
        pack = knowledge.build_context_pack(self.cfg, "bubble breakup jet", ["bubble", "breakup"])
        kinds = {i["kind"]: i for i in pack["items"]}
        self.assertTrue({"literature", "failed_idea", "rejected_claim", "upload"} <= set(kinds))
        self.assertTrue(kinds["failed_idea"]["must_read"])
        self.assertTrue(kinds["literature"]["must_read"])          # tier A
        self.assertNotIn("L0002", [i["id"] for i in pack["items"]])  # unrelated paper filtered
        self.assertTrue((self.cfg.layout.context_packs / f"{pack['id']}.md").is_file())

    def test_preflight_keywords_with_unquoted_spaces(self):
        # turn 14: `--keywords timing,path compression,full cost` split by the shell -> argparse error
        rc, out = run_cli("--project", str(self.root), "preflight", "bubble topic", "--keywords",
                          "bubble,path", "compression,full", "cost", "--include", "L0002")
        self.assertEqual(rc, 0, out)
        pack = json.loads((self.cfg.layout.context_packs / f"{out.split(':')[0]}.json").read_text(encoding="utf-8"))
        self.assertIn("compression", pack["keywords"])

    def test_receipt_heading_variants(self):
        note = knowledge.note_template(self.cfg, "L0001")
        note.write_text(note.read_text(encoding="utf-8").replace(
            "## Key points\n", "## Key Points (full text, pp. 1-7)\nBreakup regime map.\n"), encoding="utf-8")
        self.assertTrue(knowledge.receipt(self.cfg, "L0001")["has_key_points"])

    def test_include_forces_items_into_pack(self):
        pack = knowledge.build_context_pack(self.cfg, "bubble breakup jet", ["bubble"], include=["L0002"])
        item = next(i for i in pack["items"] if i["id"] == "L0002")    # unrelated by keywords
        self.assertTrue(item["included"])
        with self.assertRaises(KeyError):
            knowledge.build_context_pack(self.cfg, "x", ["bubble"], include=["L9999"])

    def test_plan_gate_requires_answers_and_receipts(self):
        pack = knowledge.build_context_pack(self.cfg, "bubble breakup jet", ["bubble", "breakup"])
        write(self.cfg.layout.experiments / "E001" / "plan.md", f"""+++
id = "E001"
context_pack = "{pack['id']}"
hypothesis = "h"
prediction = "p"
decision_rule = "d"
budget_hours = 1.0
+++

## Prior knowledge consulted
- L0001: ok
""")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertFalse(res["ok"])
        text = " ".join(res["problems"])
        self.assertIn("wiki:idea-weber-scaling-v1", text)
        self.assertIn("too short", text)
        self.assertIn("reading receipt", text)

    def test_plan_gate_passes_then_goes_stale(self):
        res, _ = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res["problems"])
        self.assertTrue(knowledge.plan_gate_passed(self.cfg, "E001"))
        # an edit to an unrelated record does not invalidate the pack (seen in the first real run)
        knowledge.lit_update(self.cfg, "L0002", tier="C")
        self.assertTrue(knowledge.check_plan(self.cfg, "E001")["ok"])
        # a new relevant item does, and only that item has to be answered
        new = knowledge.lit_add(self.cfg, "New bubble paper", "10.1/new", 2026, "B", "method", "oa", ["bubble"])
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertFalse(res["ok"])
        self.assertIn(f"stale: new relevant item {new['id']}", " ".join(res["problems"]))
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "\n## Precision", f"- {new['id']}: different scaling regime; checked\n\n## Precision"), encoding="utf-8")
        self.assertTrue(knowledge.check_plan(self.cfg, "E001")["ok"])

    def test_download_requests_only_tier_a_and_no_duplicates(self):
        before = len(human.open_actions(self.cfg))
        knowledge.lit_add(self.cfg, "Tier B paywalled", "10.1/b", 2020, "B", "background", "human_needed", ["x"])
        self.assertEqual(len(human.open_actions(self.cfg)), before)
        rec = knowledge.lit_add(self.cfg, "Tier A paywalled", "10.1/a", 2020, "A", "theory", "human_needed", ["x"])
        self.assertIsNone(human.request_action(self.cfg, "download", f"again: download {rec['id']} please",
                                               dedupe=f"download {rec['id']} "))
        self.assertEqual(len(human.open_actions(self.cfg)), before + 1)

    def test_download_request_closes_when_agents_get_the_pdf(self):
        rec = knowledge.lit_add(self.cfg, "Paywalled paper", "10.1/pw", 2026, "A", "theory", "human_needed", ["bubble"])
        self.assertEqual(len([l for l in human.open_actions(self.cfg) if rec["id"] in l]), 1)
        write(self.cfg.layout.literature / "pdf" / f"{rec['id']}.pdf", "%PDF-1.4 fake")
        rc, out = run_cli("--project", str(self.root), "lit", "update", "--id", rec["id"],
                          "--set-access", "have_pdf", "--pdf", f"literature/pdf/{rec['id']}.pdf", "--url", "https://x")
        self.assertEqual(rc, 0, out)
        self.assertEqual([l for l in human.open_actions(self.cfg) if rec["id"] in l], [])
        self.assertIn("resolved automatically", self.cfg.layout.action_needed.read_text(encoding="utf-8"))

    def test_update_moves_pdf_to_canonical_place_and_corrects_metadata(self):
        stray = write(self.cfg.layout.literature / "pdfs" / "L0002.pdf", "%PDF-1.4 fake")
        knowledge.lit_update(self.cfg, "L0002", pdf=str(stray), title="Corrected title", year=2018)
        rec = next(r for r in knowledge.lit_records(self.cfg) if r["id"] == "L0002")
        self.assertEqual((rec["pdf"], rec["access"], rec["title"], rec["year"]),
                         ("literature/pdf/L0002.pdf", "have_pdf", "Corrected title", 2018))
        self.assertTrue((self.cfg.layout.literature / "pdf" / "L0002.pdf").is_file())
        self.assertFalse(stray.exists())

    def test_pdf_drop_intake(self):
        write(self.cfg.layout.pdf_drop / "L0002.pdf", "%PDF-1.4 fake")
        self.assertEqual(knowledge.intake_pdf_drop(self.cfg), ["L0002"])
        rec = next(r for r in knowledge.lit_records(self.cfg) if r["id"] == "L0002")
        self.assertEqual(rec["access"], "have_pdf")
        self.assertTrue((self.cfg.layout.literature / "pdf" / "L0002.pdf").is_file())


class Handoff(unittest.TestCase):
    def card(self, result: str, anomalies="none observed after checking residuals", status="done", extra="",
             kind="experiments"):
        d = Path(tempfile.mkdtemp()) / kind / "X001"
        write(d / "results" / "metrics.json", json.dumps({"acc": 0.874, "runs": [{"cd": 1.23}]}))
        return write(d / "handoff.md", f"""+++
task_id = "E001"
status = "{status}"
plan_followed = "yes"
confidence = "medium"
needs_pi_decision = false
{extra}
+++

## Result
{result}

## Deviations from plan
none

## Anomalies
{anomalies}

## Suggested next steps
refine mesh
""")

    def test_numbers_must_match_metrics(self):
        self.assertTrue(handoff.validate(self.card("Accuracy 87.4% (0.874), drag 1.23."))["ok"])
        bad = handoff.validate(self.card("Accuracy 0.91."))
        self.assertIn("0.91", " ".join(bad["problems"]))

    def test_config_numbers_may_be_quoted(self):
        card = self.card("2D grid 128 with threshold 0.5: accuracy 0.874.")
        write(card.parent / "config.yaml", "grid: [128, 128]\ndim: 2\nphi_c: 0.5\n")
        r = handoff.validate(card)
        self.assertTrue(r["ok"], r["problems"])

    def test_task_cards_only_warn_about_numbers(self):
        r = handoff.validate(self.card("Registered 17 papers, 2 targets found.", kind="tasks"))
        self.assertTrue(r["ok"], r["problems"])
        self.assertIn("17.0", " ".join(r["warnings"]))

    def test_value_words_and_empty_sections(self):
        r = handoff.validate(self.card("A promising 0.874.", anomalies=""))
        msgs = " ".join(r["problems"])
        self.assertIn("promising", msgs)
        self.assertIn("Anomalies", msgs)

    def test_blocked_needs_escalation(self):
        r = handoff.validate(self.card("Run 0.874.", status="blocked"))
        self.assertIn("escalation", " ".join(r["problems"]))
        r = handoff.validate(self.card("Run 0.874.", status="blocked",
                                       extra='escalation = "state/escalations/T001.md"'))
        self.assertTrue(r["ok"], r["problems"])


class Reviewer(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def test_tiers_by_difficulty(self):
        r = lambda *a, **k: reviewer.route(self.cfg, *a, **k)
        self.assertEqual(r("format_check")["tier"], "light")
        self.assertEqual(r("section_review")["tier"], "regular")
        self.assertEqual(r("claim_support")["tier"], "deep")
        self.assertEqual(r("format_check", central=True)["tier"], "regular")
        self.assertEqual(r("section_review", text="interface capturing of the cavitation bubble")["tier"], "deep")
        self.assertEqual(r("section_review", disagreement=True)["model"], "gpt-6-astra")
        self.assertEqual(r("claim_support")["effort"], "ultra")

    def test_verdict_never_on_light_tier(self):
        self.cfg.data["reviewer"]["tasks"]["citation_audit"] = "light"
        self.assertEqual(reviewer.route(self.cfg, "citation_audit")["tier"], "regular")

    def test_light_tier_record_has_no_verdict(self):
        f = write(self.root / "claims" / "C9" / "claim.md", "+++\nstatement = \"x\"\n+++\n")
        write(f.parent / "mock_review.json", '{"verdict": "almost", "grade": "B", "issues": '
                                             '[{"severity": "minor", "text": "units"}]}')
        rec = reviewer.run(self.cfg, "format_check", [f])
        self.assertEqual((rec["verdict"], rec["grade"]), ("n/a", None))
        self.assertEqual(len(rec["issues"]), 1)
        self.assertIn("non-verdict", Path(self.cfg.layout.reviews / f"{rec['id']}.prompt.md").read_text(encoding="utf-8"))

    def test_hard_topics_match_word_starts_only(self):
        hit = lambda s: reviewer.hard_topic_hit(self.cfg, s)
        self.assertEqual(hit("steady incompressible flow"), [])
        self.assertEqual(hit("compressible jet"), ["compressib"])
        self.assertEqual(hit("less mesh dependence"), [])
        self.assertEqual(hit("wall-resolved LES of a jet"), ["LES"])

    def test_prompt_has_paths_not_summaries(self):
        p = reviewer.build_prompt("claim_support", [self.root / "claims" / "C1" / "claim.md"], "", self.root)
        self.assertIn("claims/C1/claim.md", p)
        self.assertIn('"verdict"', p)

    def test_codex_command_rendering(self):
        cmd = [c.format(model="m", effort="e", workdir="/w", out_file="/o") for c in self.cfg["reviewer"]["command"]]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("model_reasoning_effort=e", cmd)


class ClaimGate(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        res, _ = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res["problems"])
        e = self.cfg.layout.experiments / "E001"
        write(e / "results" / "metrics.json", '{"slope": 0.52}')
        write(e / "results" / "mesh_convergence.json", '{"gci_fine": 0.012}')

    def claim(self, cid, grade="B", review=None, validation=True):
        val = '[validation]\nmesh_convergence = "experiments/E001/results/mesh_convergence.json"\n' if validation else ""
        write(self.cfg.layout.claims / cid / "mock_review.json",
              json.dumps(review or {"verdict": "yes", "grade": "B", "issues": []}))
        write(self.cfg.layout.claims / cid / "claim.md", f"""+++
statement = "Breakup frequency scales with We^0.52"
experiments = ["E001"]
evidence = ["experiments/E001/results/metrics.json"]
pi_grade = "{grade}"
{val}+++
""")
        rc, out = run_cli("--project", str(self.root), "review", "run", "claim_support", "--claim", cid)
        self.assertEqual(rc, 0, out)
        return gates.claim_gate(self.cfg, cid)

    def test_accept_writes_finding(self):
        res = self.claim("C001", "B", {"verdict": "yes", "grade": "A", "issues": []})
        self.assertEqual(res["decision"], "accepted", res)
        self.assertEqual(res["grade"], "B")                        # worse of PI and reviewer
        f = self.cfg.layout.findings / res["finding"]
        self.assertTrue((f / "finding.json").is_file())
        self.assertIn("We^0.52", (self.cfg.layout.findings / "INDEX.md").read_text(encoding="utf-8"))
        self.assertEqual(read_json(f / "finding.json")["reviewer_model"], "gpt-6-astra")

    def test_domain_validation_required(self):
        res = self.claim("C002", validation=False)
        self.assertEqual(res["decision"], "blocked")
        self.assertIn("mesh_convergence", " ".join(res["reasons"]))

    def test_rejection_becomes_negative_knowledge(self):
        res = self.claim("C003", "B", {"verdict": "no", "grade": "C", "issues": []})
        self.assertEqual(res["decision"], "rejected")
        self.assertTrue((self.cfg.layout.rejected / "C003" / "REJECTED.md").is_file())
        pack = knowledge.build_context_pack(self.cfg, "breakup frequency scales", ["breakup", "frequency"])
        self.assertIn("C003", [i["id"] for i in pack["items"]])

    def test_grade_split_goes_to_human_once(self):
        res = self.claim("C004", "A", {"verdict": "yes", "grade": "C", "issues": []})
        self.assertEqual(res["decision"], "human_decision")
        gates.claim_gate(self.cfg, "C004")
        self.assertEqual(len([l for l in human.open_actions(self.cfg) if "C004" in l]), 1)


class Ledger(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def rnd(self, verdict, issues):
        return ledger.record_round(self.cfg, "paper", {"id": "R", "verdict": verdict, "issues": issues})

    def test_oscillation_escalates(self):
        a = {"severity": "major", "text": "mesh convergence evidence missing for case B"}
        self.rnd("not ready", [a])
        ledger.set_status(self.cfg, "paper", "I001", "fixed")
        self.rnd("not ready", [a])
        ledger.set_status(self.cfg, "paper", "I001", "fixed")
        self.rnd("not ready", [a])
        res = ledger.check(self.cfg, "paper")
        self.assertEqual(res["state"], "escalate")
        self.assertIn("oscillating", " ".join(res["reasons"]))

    def test_candidate_done_after_two_clean_rounds(self):
        self.rnd("not ready", [{"severity": "major", "text": "abstract overstates generality"}])
        ledger.set_status(self.cfg, "paper", "I001", "fixed")
        self.rnd("almost", [{"severity": "minor", "text": "typo in figure three caption"}])
        self.rnd("ready", [])
        self.assertEqual(ledger.check(self.cfg, "paper")["state"], "candidate_done")

    def test_stall_and_max_rounds(self):
        for n in range(3):
            self.rnd("not ready", [{"severity": "major", "text": f"issue {n} {w}"} for w in
                                   ("alpha beta gamma", "delta epsilon zeta", "eta theta iota")[: n + 1]])
        self.assertIn("not decreasing", " ".join(ledger.check(self.cfg, "paper")["reasons"]))
        self.cfg.data["gates"]["max_review_rounds"] = 3
        self.assertIn("max_review_rounds", " ".join(ledger.check(self.cfg, "paper")["reasons"]))


@unittest.skipIf(os.name == "nt", "fake sbatch scripts need a POSIX shell")
class Slurm(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project(site="negishi")
        self.cfg.data["slurm"]["account"] = "testacct"
        self.cfg.data["slurm"]["conda_env"] = "/home/u/.conda/envs/v/env"
        self.cfg.data["slurm"]["require_clean_git"] = False
        self.bin = Path(tempfile.mkdtemp())
        for name, body in (("sbatch", 'if [ "$1" = "--test-only" ]; then echo "sbatch: Job 1 to start" >&2; exit 0; fi\necho "4242;negishi"'),
                           ("sacct", 'echo "COMPLETED"'), ("squeue", "true")):
            p = write(self.bin / name, f"#!/bin/bash\n{body}\n")
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
            os.environ[f"AUTOLAB_{name.upper()}"] = str(p)

    def tearDown(self):
        for name in ("SBATCH", "SACCT", "SQUEUE", "SCANCEL"):
            os.environ.pop(f"AUTOLAB_{name}", None)

    def test_render_has_site_rules(self):
        passing_plan(self.cfg)
        path, _ = slurm.render(self.cfg, "E001")
        s = path.read_text(encoding="utf-8")
        self.assertIn("#SBATCH --account=testacct", s)
        self.assertIn("#SBATCH --time=01:30:00", s)
        self.assertLess(s.index("set +u"), s.index("conda activate"))
        self.assertIn("CLUSTER_SCRATCH:-/scratch/negishi", s)

    def test_clean_git_checks_code_not_state(self):
        import subprocess
        passing_plan(self.cfg)
        self.cfg.data["slurm"]["require_clean_git"] = True
        r = str(self.root)
        git = lambda *a: subprocess.run(["git", "-C", r, *a], capture_output=True, text=True, check=True)
        git("init", "-q"); git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        write(self.root / "state" / "pi_logs" / "x.jsonl", "{}")          # churn outside code
        write(self.root / "state" / "STATE.md", "changed")
        self.assertEqual(slurm.submit(self.cfg, "E001")["job"], "4242")
        write(self.root / "experiments" / "E001" / "code" / "run.py", "print(1)")  # uncommitted code
        with self.assertRaises(RuntimeError):
            slurm.submit(self.cfg, "E001")

    def test_total_core_hour_cap(self):
        passing_plan(self.cfg)                              # 4 cpus x 1.5 h = 6 core-hours requested
        sacct = write(self.bin / "sacct_usage",
                      '#!/bin/bash\nif [[ "$*" == *CPUTimeRAW* ]]; then echo "4242|36000"; else echo COMPLETED; fi\n')
        sacct.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(sacct)
        self.cfg.data["slurm"]["total_core_hours"] = 15
        slurm.submit(self.cfg, "E001")                     # nothing used yet: 0 + 6 <= 15
        self.assertEqual(slurm.core_hours_used(self.cfg), 10.0)
        with self.assertRaises(RuntimeError) as e:
            slurm.submit(self.cfg, "E001")                 # 10 used + 6 > 15
        self.assertIn("core-hour cap", str(e.exception))

    def test_standby_qos_and_cancel(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace('partition = "cpu"', 'partition = "cpu"\nqos = "standby"'),
                        encoding="utf-8")
        knowledge.check_plan(self.cfg, "E001")
        path, r = slurm.render(self.cfg, "E001")
        self.assertIn("#SBATCH --qos=standby", path.read_text(encoding="utf-8"))
        plan.write_text(plan.read_text(encoding="utf-8").replace("hours = 1.5", "hours = 5"), encoding="utf-8")
        self.cfg.data["slurm"]["max_hours"] = 24
        with self.assertRaises(ValueError):
            slurm.render(self.cfg, "E001")                    # standby is limited to 4 h
        plan.write_text(plan.read_text(encoding="utf-8").replace("hours = 5", "hours = 1.5"), encoding="utf-8")
        knowledge.check_plan(self.cfg, "E001")
        os.environ["AUTOLAB_SACCT"] = str(write(self.bin / "sacct_pending", "#!/bin/bash\necho PENDING\n"))
        (self.bin / "sacct_pending").chmod(0o755)
        p = write(self.bin / "scancel", "#!/bin/bash\necho cancelled $1 >> " + str(self.bin / "scancel.log") + "\n")
        p.chmod(0o755)
        os.environ["AUTOLAB_SCANCEL"] = str(p)
        try:
            slurm.submit(self.cfg, "E001")
            self.assertEqual(slurm.cancel(self.cfg, "E001"), ["4242"])
            self.assertIn("cancelled 4242", (self.bin / "scancel.log").read_text())
            with self.assertRaises(ValueError):
                slurm.cancel(self.cfg, "E001", job="999")      # not ours
            self.assertEqual(len(slurm.status(self.cfg, "E001")), 1)
        finally:
            os.environ.pop("AUTOLAB_SCANCEL", None)

    def test_memory_billed_as_cores(self):
        # seen in turn 8: 8 cpus + 64 GB on Negishi was allocated and billed as 33 cores
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("mem_gb = 8", "mem_gb = 64"), encoding="utf-8")
        r = slurm.resources(self.cfg, "E001")
        self.assertEqual((r["cpus"], r["billed_cpus"]), (4, 33))
        self.assertIn("billed as 33 cores", r["warning"])
        self.assertIn("mem_reason", " ".join(r["problems"]))            # extra memory needs a reason
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "mem_gb = 64", 'mem_gb = 64\nmem_reason = "512^3 float32 fields x 6 arrays"'), encoding="utf-8")
        self.assertEqual(slurm.resources(self.cfg, "E001")["problems"], [])
        self.cfg.data["slurm"]["max_cpus"] = 16
        self.assertIn("billed as 33 cores", " ".join(slurm.resources(self.cfg, "E001")["problems"]))

    def test_parts_run_side_by_side(self):
        # seen in turn 7: part B of E003b could not be submitted while part A was running
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "+++\n\n## Prior", '\n[parts.A]\nrun_command = "python run.py --suite A --out results/A"\n'
            '[parts.B]\nrun_command = "python run.py --suite B --out results/B"\n[parts.B.resources]\nhours = 0.5\n'
            "+++\n\n## Prior"), encoding="utf-8")
        self.assertTrue(knowledge.check_plan(self.cfg, "E001")["ok"])
        pend = write(self.bin / "sacct_pend2", "#!/bin/bash\necho PENDING\n")
        pend.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(pend)
        a = slurm.submit(self.cfg, "E001", "A")
        b = slurm.submit(self.cfg, "E001", "B")
        self.assertEqual((a["part"], b["part"]), ("A", "B"))
        self.assertIn("--suite B", (self.cfg.layout.experiments / "E001" / "slurm" / "job-B.sbatch").read_text())
        self.assertIn("#SBATCH --time=00:30:00", (self.cfg.layout.experiments / "E001" / "slurm" / "job-B.sbatch").read_text())
        with self.assertRaises(RuntimeError):
            slurm.submit(self.cfg, "E001", "A")              # same part still pending
        with self.assertRaises(ValueError):
            slurm.render(self.cfg, "E001", "C")              # undeclared part

    def test_no_second_submit_while_job_unfinished(self):
        passing_plan(self.cfg)
        pend = write(self.bin / "sacct_pend", "#!/bin/bash\necho PENDING\n")
        pend.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(pend)
        slurm.submit(self.cfg, "E001")
        with self.assertRaises(RuntimeError) as e:
            slurm.submit(self.cfg, "E001")
        self.assertIn("unfinished", str(e.exception))

    def test_caps_enforced(self):
        passing_plan(self.cfg)
        self.cfg.data["slurm"]["max_cpus"] = 2
        with self.assertRaises(ValueError):
            slurm.render(self.cfg, "E001")
        self.cfg.data["slurm"]["max_cpus"] = 0                 # 0 = no project cap
        slurm.render(self.cfg, "E001")

    def test_job_array_render_caps_and_state(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("mem_gb = 8", 'mem_gb = 8\narray = "0-9%4"'),
                        encoding="utf-8")
        r = slurm.resources(self.cfg, "E001")
        self.assertEqual((r["array_size"], r["problems"]), (10, []))
        text = slurm.render(self.cfg, "E001")[0].read_text()
        self.assertIn("#SBATCH --array=0-9%4", text)
        self.assertIn("%x-%A_%a.out", text)
        self.assertEqual(slurm.validate(self.cfg, "E001")["core_hours_max"], round(r["billed_cpus"] * 1.5 * 10, 1))
        multi = write(self.bin / "sacct_multi", "#!/bin/bash\nprintf 'COMPLETED\\nRUNNING\\nPENDING\\n'\n")
        multi.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(multi)
        self.assertEqual(slurm.job_state("77"), "RUNNING")
        bad = write(self.bin / "sacct_bad", "#!/bin/bash\nprintf 'COMPLETED\\nTIMEOUT\\nCOMPLETED\\n'\n")
        bad.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(bad)
        self.assertEqual(slurm.job_state("77"), "TIMEOUT")
        plan.write_text(plan.read_text(encoding="utf-8").replace('array = "0-9%4"', 'array = "x-y"'), encoding="utf-8")
        self.assertIn("array", " ".join(slurm.resources(self.cfg, "E001")["problems"]))

    def test_usage_recorded_after_finish(self):
        passing_plan(self.cfg)
        slurm.submit(self.cfg, "E001")
        acct = write(self.bin / "sacct_usage", "#!/bin/bash\n"
                     'if [[ "$*" == *TotalCPU* ]]; then\n'
                     "  printf '4242|3600|01:00:00|4||16Gn\\n4242.batch|3600|01:00:00|4|3.5G|\\n'\n"
                     "else echo COMPLETED; fi\n")
        acct.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(acct)
        lines = slurm.record_usage(self.cfg)
        self.assertEqual(len(lines), 1)
        self.assertIn("CPU efficiency 25%", lines[0])
        self.assertIn("peak memory 3.5 GB", lines[0])
        rec = read_jsonl(self.cfg.layout.experiments / "E001" / "jobs_usage.jsonl")[0]
        self.assertEqual((rec["cpu_efficiency"], rec["max_rss_gb"], rec["req_mem_gb"]), (0.25, 3.5, 16.0))
        self.assertEqual(slurm.record_usage(self.cfg), [])                       # once per job

    def test_site_notes_rendered_and_core_is_site_neutral(self):
        claude_md = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        agent = (self.root / ".claude" / "agents" / "student-experiment.md").read_text(encoding="utf-8")
        for text in (claude_md, agent):
            self.assertIn("Cluster notes (site `negishi`", text)
            self.assertIn("standby", text)
            self.assertIn("1.95 GB counts as one core", text)
        for f in list((FW / "core" / "agents").glob("*.md")) + [FW / "templates" / "CLAUDE.md.tmpl"]:
            self.assertNotIn("negishi", f.read_text(encoding="utf-8").lower(), f)

    def test_every_site_profile_loads(self):
        for f in sorted((FW / "sites").glob("*.toml")):
            site = "_template" if f.stem == "_template" else f.stem
            root, cfg = make_project(site=site)
            self.assertEqual(cfg.site["site"]["name"], {"_template": "mycluster"}.get(site, site), f)
        root, cfg = make_project(site="gautschi")
        passing_plan(cfg)
        plan = cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("cpus = 4", "cpus = 192"), encoding="utf-8")
        cfg.data["slurm"]["max_cpus"] = 0
        cfg.data["slurm"]["account"] = "testacct"
        self.assertEqual(slurm.resources(cfg, "E001")["problems"], [])      # one full 192-core node

    def test_mpi_multi_node(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "mem_gb = 8", "mem_gb = 8\nnodes = 8\nntasks = 256"), encoding="utf-8")
        self.cfg.data["slurm"]["max_cpus"] = 0
        r = slurm.resources(self.cfg, "E001")
        self.assertEqual(r["problems"], [], r)
        self.assertEqual((r["total_cpus"], r["billed_cpus"]), (1024, 1024))   # 256 ranks x 4 cpus
        plan.write_text(plan.read_text(encoding="utf-8").replace("ntasks = 256", "ntasks = 64").replace(
            "nodes = 8", "nodes = 2"), encoding="utf-8")
        r = slurm.resources(self.cfg, "E001")
        self.assertEqual(r["problems"], [])
        text = slurm.render(self.cfg, "E001")[0].read_text()
        self.assertIn("#SBATCH --nodes=2", text)
        self.assertIn("#SBATCH --ntasks=64", text)
        plan.write_text(plan.read_text(encoding="utf-8").replace("nodes = 2", "nodes = 1"), encoding="utf-8")
        self.assertIn("cores of a", " ".join(slurm.resources(self.cfg, "E001")["problems"]))   # 64 x 4 > 128
        self.cfg.data["slurm"]["max_cpus"] = 128
        self.assertIn("total_cpus=256", " ".join(slurm.resources(self.cfg, "E001")["problems"]))

    def test_submit_requires_plan_gate_and_records_job(self):
        write(self.cfg.layout.experiments / "E002" / "plan.md", '+++\nid = "E002"\nrun_command = "x"\n+++\n')
        with self.assertRaises(RuntimeError):
            slurm.submit(self.cfg, "E002")
        passing_plan(self.cfg)
        self.assertTrue(slurm.validate(self.cfg, "E001")["ok"])
        rec = slurm.submit(self.cfg, "E001")
        self.assertEqual(rec["job"], "4242")
        self.assertEqual(slurm.status(self.cfg, "E001")[0]["state"], "COMPLETED")
        ok, why = hooks.decide({"tool_name": "Edit", "tool_input": {"file_path": "experiments/E001/plan.md"}}, self.root)
        self.assertFalse(ok)
        self.assertIn("locked", why)


class Hooks(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def d(self, tool, **inp):
        return hooks.decide({"tool_name": tool, "tool_input": inp}, self.root)[0]

    def test_zones(self):
        self.assertFalse(self.d("Write", file_path="00_human/GOAL.md"))
        self.assertTrue(self.d("Edit", file_path=str(self.root / "00_human" / "ACTION_NEEDED.md")))
        self.assertFalse(self.d("Write", file_path="findings/F001/claim.md"))
        self.assertFalse(self.d("Edit", file_path=".claude/skills/run-experiment/SKILL.md"))
        self.assertTrue(self.d("Write", file_path="experiments/E001/plan.md"))
        self.assertFalse(self.d("Bash", command="sbatch job.sh"))
        self.assertFalse(self.d("Bash", command="cd x && scancel 12"))
        self.assertTrue(self.d("Bash", command="autolab slurm submit E001"))
        self.assertFalse(self.d("Bash", command="echo hi > 00_human/RULES.md"))
        self.assertFalse(self.d("Bash", command="rm -rf findings/F001"))
        self.assertTrue(self.d("Bash", command="ls findings/"))

    def test_human_config_protected(self):
        # agents must not raise their own budget/caps or grant themselves tools
        self.assertFalse(self.d("Edit", file_path="autolab.toml"))
        self.assertFalse(self.d("Write", file_path=str(self.root / ".claude" / "settings.json")))
        self.assertFalse(self.d("Write", file_path=".claude/settings.local.json"))
        self.assertFalse(self.d("Edit", file_path=".claude/agents/student-experiment.md"))
        self.assertFalse(self.d("Edit", file_path="CLAUDE.md"))
        self.assertFalse(self.d("Bash", command="sed -i s/200/2000/ autolab.toml"))
        self.assertFalse(self.d("Bash", command="echo x >> .claude/settings.json"))
        self.assertTrue(self.d("Bash", command="cat autolab.toml"))
        self.assertTrue(self.d("Bash", command="grep budget autolab.toml"))

    def test_extra_allow_rules_and_env(self):
        self.cfg.data["agents"] = {"allow_bash": ["make", "gcc", "tools/basilisk/src/qcc"],
                                   "env": {"BASILISK": "{root}/tools/basilisk/src"}}
        project._write_settings(self.cfg)
        s = read_json(self.root / ".claude" / "settings.json")
        root = self.root.resolve().as_posix()
        for rule in ("Bash(make:*)", "Bash(gcc:*)", "Bash(tools/basilisk/src/qcc:*)",
                     "Bash(./tools/basilisk/src/qcc:*)", f"Bash({root}/tools/basilisk/src/qcc:*)"):
            self.assertIn(rule, s["permissions"]["allow"])
        self.assertEqual(s["env"]["BASILISK"], f"{root}/tools/basilisk/src")

    def test_writes_outside_project_blocked_except_write_roots(self):
        os.environ["AUTOLAB_WRITE_ROOTS"] = str(Path(tempfile.gettempdir()))
        try:
            self.assertFalse(self.d("Write", file_path=str(FW / "core" / "overlays" / "x.md")))
            self.assertTrue(self.d("Write", file_path=str(Path(tempfile.gettempdir()) / "run" / "out.vtk")))
        finally:
            os.environ.pop("AUTOLAB_WRITE_ROOTS", None)

    def test_shell_reads_of_protected_zones_allowed(self):
        # seen in the first real run: a read-only command was blocked because of `2>/dev/null`
        self.assertTrue(self.d("Bash", command="cat 00_human/GOAL.md 2>/dev/null; ls 00_human/ | head -5"))
        self.assertTrue(self.d("Bash", command="ls findings/ > state/listing.txt"))
        self.assertTrue(self.d("Bash", command="grep -r bubble 00_human/ 2>&1 | head"))
        self.assertTrue(self.d("Bash", command="cp 00_human/uploads/x/data.csv experiments/E001/data/"))
        self.assertFalse(self.d("Bash", command="ls; echo x >> 00_human/GOAL.md"))
        self.assertFalse(self.d("Bash", command="cp notes.md 00_human/inbox/"))
        self.assertFalse(self.d("Bash", command="sed -i s/a/b/ 00_human/RULES.md"))
        self.assertFalse(self.d("Bash", command="cat x | tee findings/F001/claim.md"))
        self.assertTrue(self.d("Bash", command="echo ok >> 00_human/ACK.md"))

    def test_package_installs_stay_in_project(self):
        # seen in the first real run: pip refused, then python -m pip download + install --user
        self.assertFalse(self.d("Bash", command="pip install --user connected-components-3d"))
        self.assertFalse(self.d("Bash", command="python -m pip download cc3d -d /tmp/x"))
        self.assertFalse(self.d("Bash", command="python -m pip install --user --no-deps /tmp/x/cc3d.whl"))
        self.assertFalse(self.d("Bash", command="conda install -y numba"))
        self.assertTrue(self.d("Bash", command=".venv/bin/python -m pip install connected-components-3d"))
        self.assertTrue(self.d("Bash", command="python -m pip install --target .pylib pypdf"))
        self.assertTrue(self.d("Bash", command="python -m venv --system-site-packages .venv"))
        self.assertTrue(self.d("Bash", command="python -m pip list"))

    def test_mentions_of_sbatch_are_not_calls(self):
        self.assertTrue(self.d("Bash", command='git commit -qm "E001: plan passed; sbatch script rendered"'))
        self.assertTrue(self.d("Bash", command="echo use sbatch via autolab"))
        self.assertFalse(self.d("Bash", command="git status && sbatch job.sh"))

    def test_stop_blocks_everything_but_reading(self):
        (self.root / "00_human" / "STOP").touch()
        self.assertFalse(self.d("Bash", command="ls"))
        self.assertTrue(self.d("Read", file_path="CLAUDE.md"))


class TickAndCli(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()

    def test_tick_digest(self):
        e = self.cfg.layout.experiments / "E001"
        write(e / "results" / "metrics.json", '{"slope": 0.52}')
        write(e / "handoff.md", '+++\ntask_id = "E001"\nstatus = "done"\nplan_followed = "yes"\n'
                                'confidence = "low"\nneeds_pi_decision = true\n+++\n\n## Result\nslope 0.61\n')
        res = tick.run(self.cfg)
        text = Path(res["digest"]).read_text(encoding="utf-8")
        self.assertIn("RETURN TO STUDENT", text)                  # 0.61 not in metrics, sections missing
        self.assertNotIn("_TEMPLATE", text)
        self.assertEqual(tick.run(self.cfg)["new_cards"], 0)       # unchanged card not re-reported

    def test_not_ready_means_no_work(self):
        self.cfg.layout.ready.unlink()
        res = tick.run(self.cfg)
        self.assertFalse(res["ready"])
        self.assertIn("Human input not ready", Path(res["digest"]).read_text(encoding="utf-8"))
        with self.assertRaises(RuntimeError):
            slurm.submit(self.cfg, "E001")

    def test_budget_cap(self):
        from autolab import budget
        self.cfg.data["budget"]["total_usd"] = 1.0
        log = write(self.root / "state" / "pi_logs" / "t.jsonl",
                    '{"type":"system"}\nnot json\n{"type":"result","total_cost_usd":0.6}\n')
        budget.record_turn(self.cfg, log)
        self.assertFalse(budget.status(self.cfg)["over"])
        budget.record_turn(self.cfg, log)
        st = budget.status(self.cfg)
        self.assertTrue(st["over"])
        self.assertAlmostEqual(st["spent_usd"], 1.2)
        self.assertIn("Budget: $1.20 of $1.00", Path(tick.run(self.cfg)["digest"]).read_text(encoding="utf-8"))

    def test_numerics_domain_requires_reference_and_timing(self):
        cfgf = self.root / "autolab.toml"
        cfgf.write_text(cfgf.read_text(encoding="utf-8").replace('domain = "cfd"', 'domain = "numerics"'), encoding="utf-8")
        cfg = Config(self.root)
        project.remount(cfg)
        self.assertEqual(skills.check(cfg), [])
        self.assertEqual(cfg.domain["validation"]["required_evidence"], ["reference_agreement", "timing_protocol"])
        self.assertEqual(reviewer.route(cfg, "section_review", text="incremental union-find with deletions")["tier"], "deep")

    def test_headless_permissions_written(self):
        s = json.loads((self.cfg.layout.claude / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("Bash(autolab:*)", s["permissions"]["allow"])
        self.assertEqual(s["permissions"]["additionalDirectories"], [self.cfg.fw.as_posix()])
        self.assertIn("Bash(.venv/bin/python:*)", s["permissions"]["allow"])
        self.assertIn(f"Bash({self.root.resolve().as_posix()}/.venv/bin/python:*)", s["permissions"]["allow"])
        self.assertIn("/tmp", s["env"]["AUTOLAB_WRITE_ROOTS"])
        self.assertTrue((self.root / "DOMAIN_NOTES.md").is_file())
        self.assertIn("`DOMAIN_NOTES.md`", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("Bash(sbatch:*)", s["permissions"]["deny"])
        self.assertIn("AUTOLAB_PYTHON", s["env"])

    def test_templates_have_start_form(self):
        h = self.cfg.layout.human
        self.assertTrue((h / "START_HERE.md").is_file())
        fresh = (FW / "templates" / "project" / "00_human" / "GOAL.md").read_text(encoding="utf-8")
        self.assertIn("[REQUIRED]", fresh)
        self.assertIn("<<FILL", fresh.split("<!--")[0])
        with self.assertRaises(FileNotFoundError):
            project.init(Path(tempfile.mkdtemp()) / "x", "x", "generic", "nosuchsite")

    def test_cli_surfaces(self):
        rc, out = run_cli("--project", str(self.root), "skills", "resolve", "--role", "student", "--task", "figure")
        self.assertEqual(rc, 0)
        self.assertIn("scientific-visualization", out)
        rc, out = run_cli("--project", str(self.root), "review", "route", "claim_support", "--central")
        self.assertIn('"deep"', out)
        rc, out = run_cli("--project", str(self.root), "human", "request", "--kind", "download", "get L0003")
        self.assertTrue(out.strip().startswith("A"))
        rc, out = run_cli("--project", str(self.root), "doctor")
        self.assertIn("skill routes resolve", out)
        self.assertIn("[ok] skills mounted", out)

    def test_decision_requests_block_by_default(self):
        rc, out = run_cli("--project", str(self.root), "human", "request", "--kind", "decision",
                          "--title", "Stop or modify?", "E003 slower than baseline")
        line = [l for l in human.open_actions(self.cfg) if "Stop or modify?" in l][0]
        self.assertIn("[BLOCKING]", line)
        run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "--fyi", "just so you know")
        self.assertIn("[optional]", [l for l in human.open_actions(self.cfg) if "just so you know" in l][0])

    def test_delegation_makes_decisions_non_blocking(self):
        import datetime as dt
        until = (dt.datetime.now().astimezone() + dt.timedelta(hours=1)).isoformat(timespec="seconds")
        write(self.cfg.layout.human / "DELEGATED", f'until = "{until}"\nto = "pi"\n')
        run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "stop E003?")
        line = [l for l in human.open_actions(self.cfg) if "stop E003?" in l][0]
        self.assertIn("[optional]", line)
        self.assertIn("PI decided under delegation", line)
        self.assertIn("final authority delegated", Path(tick.run(self.cfg)["digest"]).read_text(encoding="utf-8"))
        past = (dt.datetime.now().astimezone() - dt.timedelta(minutes=1)).isoformat(timespec="seconds")
        write(self.cfg.layout.human / "DELEGATED", f'until = "{past}"\nto = "pi"\n')
        self.assertIsNone(human.delegation(self.cfg))         # expired: normal rules again

    def test_job_ids_and_result_files_count_in_handoff(self):
        e = self.cfg.layout.experiments / "E009"
        write(e / "results" / "metrics.json", '{"speedup": 0.36}')
        write(e / "results" / "pilot" / "metrics.json", '{"t_update_ms": 8.96}')
        write(e / "handoff.md", '+++\ntask_id = "E009"\nstatus = "done"\nplan_followed = "yes"\n'
                                'confidence = "low"\nneeds_pi_decision = true\n+++\n\n## Result\n'
                                'Job 44371319: speedup 0.36, update 8.96 ms.\n\n## Deviations from plan\nnone\n\n'
                                '## Anomalies\nnone seen\n\n## Suggested next steps\nprofile\n')
        r = handoff.validate(e / "handoff.md")
        self.assertTrue(r["ok"], r["problems"])

    def test_inbox_answer_closes_request(self):
        aid = human.request_action(self.cfg, "decision", "stop or modify?", blocking=True)
        other = human.request_action(self.cfg, "download", "get L0042")
        write(self.cfg.layout.inbox / f"2026-10-16-{aid}-answer.md", "modify\n")
        write(self.cfg.layout.inbox / "2026-10-16-unrelated.md", "note\n")
        done = human.resolve_from_inbox(self.cfg)
        self.assertEqual([a for a, _ in done], [aid])
        open_ids = " ".join(human.open_actions(self.cfg))
        self.assertNotIn(f"**{aid}**", open_ids)
        self.assertIn(f"**{other}**", open_ids)
        self.assertEqual(human.resolve_from_inbox(self.cfg), [])        # idempotent

    def test_wake_check(self):
        from autolab import wake
        go, why = wake.check(self.cfg)
        self.assertTrue(go)                                   # no snapshot yet
        human.check_changes(self.cfg, now=time.time() + 10_000)
        human.ack(self.cfg, "baseline")
        wake.mark(self.cfg)
        go, why = wake.check(self.cfg)
        self.assertFalse(go, why)                             # nothing new
        write(self.cfg.layout.tasks / "T001" / "handoff.md", "+++\n+++\n")
        self.assertTrue(wake.check(self.cfg)[0])              # new card
        wake.mark(self.cfg)
        self.assertFalse(wake.check(self.cfg)[0])
        self.assertTrue(wake.check(self.cfg, max_idle_min=45, now=time.time() + 46 * 60)[0])
        rc, out = run_cli("--project", str(self.root), "wake", "check")
        self.assertEqual(rc, 3, out)

    def test_hook_entrypoint(self):
        import subprocess
        env = {**os.environ, "PYTHONPATH": str(FW / "src"), "CLAUDE_PROJECT_DIR": str(self.root)}
        ev = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "00_human/GOAL.md"}, "cwd": str(self.root)})
        p = subprocess.run([sys.executable, "-m", "autolab", "hook", "pre-tool-use"], input=ev,
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("belongs to the human", p.stderr)


def skill_log(cfg: Config, *names: str) -> Path:
    """A turn log with Skill-tool calls, shaped like claude -p stream-json."""
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": f"t{i}", "name": "Skill", "input": {"skill": n}}]}})
        for i, n in enumerate(names)]
    p = cfg.layout.state / "pi_logs" / f"20991231-{len(list((cfg.layout.state / 'pi_logs').glob('*.jsonl'))) if (cfg.layout.state / 'pi_logs').is_dir() else 0}.jsonl"
    return write(p, "\n".join(lines) + "\n")


class Workflow(unittest.TestCase):
    """Task cards with required skills, skill-use checks, precision gate, decision log, lessons."""

    def setUp(self):
        self.root, self.cfg = make_project()

    def test_task_card_gets_required_skills_and_model(self):
        from autolab import tasks
        card = tasks.new(self.cfg, "Analyse E001 runs", "analysis")
        meta, _ = read_frontmatter(card)
        self.assertIn("analyze-results", meta["skills"])
        self.assertIn("verification-before-completion", meta["skills"])   # always required for students
        self.assertEqual(meta["model"], "sonnet")
        exp = read_frontmatter(tasks.new(self.cfg, "Build solver", "experiment", difficulty="hard"))[0]
        self.assertEqual(exp["model"], "opus")
        self.assertIn("run-experiment", exp["skills"])
        self.assertIn("test-driven-development", exp["skills"])
        self.assertNotEqual(meta["id"], exp["id"])
        dbg = read_frontmatter(tasks.new(self.cfg, "Find the mismatch", "experiment", task="debug",
                                         difficulty="mechanical"))[0]
        self.assertIn("systematic-debugging", dbg["skills"])
        self.assertEqual(dbg["model"], "haiku")
        with self.assertRaises(ValueError):
            tasks.new(self.cfg, "x", "analysis", extra_skills=["no-such-skill"])

    def test_handoff_requires_invoked_skills(self):
        from autolab import tasks
        card_dir = tasks.new(self.cfg, "Analyse", "analysis").parent
        body = ('+++\ntask_id = "T001"\nstatus = "done"\nplan_followed = "yes"\nconfidence = "medium"\n'
                'needs_pi_decision = false\nskills_used = [{used}]\n+++\n\n## Result\nno numbers\n\n'
                '## Deviations from plan\nnone\n\n## Anomalies\nnone after checking\n\n## Suggested next steps\nmore\n')
        card = write(card_dir / "handoff.md", body.format(used='"analyze-results"'))
        res = handoff.validate_in(self.cfg, card)
        self.assertFalse(res["ok"])
        self.assertIn("verification-before-completion", " ".join(res["problems"]))
        card.write_text(body.format(used='"analyze-results", "verification-before-completion"'), encoding="utf-8")
        self.assertTrue(handoff.validate_in(self.cfg, card)["ok"])                 # no logs: unverifiable, warns
        skill_log(self.cfg, "analyze-results")                                     # now logs exist
        res = handoff.validate_in(self.cfg, card)
        self.assertFalse(res["ok"])
        self.assertIn("never invoked", " ".join(res["problems"]))
        skill_log(self.cfg, "verification-before-completion")
        self.assertTrue(handoff.validate_in(self.cfg, card)["ok"])

    def test_plan_gate_precision_hypotheses_and_planning_skills(self):
        res, _ = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        text = plan.read_text(encoding="utf-8")
        plan.write_text(text.split("## Precision")[0], encoding="utf-8")
        self.assertIn("Precision", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        plan.write_text(text.replace('hypotheses = ["H1"]', 'hypotheses = ["H9"]'), encoding="utf-8")
        self.assertIn("H9", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        plan.write_text(text, encoding="utf-8")
        skill_log(self.cfg, "experiment-plan")                                     # statistical-power missing
        self.assertIn("statistical-power", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        skill_log(self.cfg, "statistical-power")
        self.assertTrue(knowledge.check_plan(self.cfg, "E001")["ok"])

    def test_decisions_are_append_only(self):
        from autolab import tasks
        d1 = tasks.decide(self.cfg, "rerun E003 with 8 seeds", "CI too wide", "E003/analysis/summary.json")
        d2 = tasks.decide(self.cfg, "stop track B", "no gain possible", track="B")
        self.assertEqual((d1, d2), ("D001", "D002"))
        text = (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn("rerun E003", text)
        self.assertIn("track B", text)
        d = lambda tool, **inp: hooks.decide({"tool_name": tool, "tool_input": inp}, self.root)[0]
        self.assertFalse(d("Edit", file_path="state/DECISIONS.md"))
        self.assertFalse(d("Bash", command="echo x >> state/DECISIONS.md"))
        self.assertTrue(d("Bash", command='autolab decide "x" --why "y"'))
        self.assertTrue(d("Bash", command="cat state/DECISIONS.md"))

    def test_retrospective_due_and_lessons_reach_context_packs(self):
        from autolab import tasks
        self.assertEqual(tasks.retro_due(self.cfg, 25), 0)
        self.assertIsNone(tasks.retro_due(self.cfg, 5))
        write(self.cfg.layout.experience / "RETRO-25.md", "# retro\n")
        self.assertIsNone(tasks.retro_due(self.cfg, 30))
        self.assertEqual(tasks.retro_due(self.cfg, 45), 25)
        lid = tasks.add_lesson(self.cfg, "Size seeds from a pilot variance before timing runs",
                               "three seeds gave a CI from -1.3 to 15.9", ["timing", "seeds", "bubble"])
        pack = knowledge.build_context_pack(self.cfg, "bubble timing study", ["bubble", "timing", "seeds"])
        self.assertIn(lid, [i["id"] for i in pack["items"]])

    def test_tick_reports_skill_use(self):
        from autolab import tick
        tick.run(self.cfg)
        skill_log(self.cfg, "analyze-results", "analyze-results")
        res = tick.run(self.cfg)
        digest = Path(res["digest"]).read_text(encoding="utf-8")
        self.assertIn("analyze-results x2", digest)


if __name__ == "__main__":
    unittest.main()
