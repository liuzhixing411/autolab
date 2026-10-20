"""End-to-end interface tests. Standard library only:  python -m unittest discover -s tests -v"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
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
                     report, research, reviewer, skills, slurm, tasks, tick)
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

## Discrimination
Separates Weber scaling (H1) from a Reynolds-dominated alternative: the two predict different slopes.
If the result supports H1 (slope 0.4-0.6) we extend to three nozzles; if it refutes H1 we test the Reynolds model.
""")
    hyp = cfg.layout.state / "HYPOTHESES.md"
    if not hyp.is_file() or "<<FILL" in hyp.read_text(encoding="utf-8"):
        write(hyp, "| H1 | - | Breakup frequency scales with Weber number | open | - | - | E001 |\n")
    if research.candidate(cfg, "H1") is None:
        research.new_candidate(cfg, "H1", "Breakup frequency scales with Weber number",
                               closest_work="L0001 regime map", mechanism_difference="jet shear instead of turbulence",
                               key_uncertainty="slope at high Weber", discriminating_experiment="E001 slope fit",
                               continue_if="slope 0.4-0.6", kill_if="slope outside 0.2-0.8")
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
        research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "Validated slope and mesh study")

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

## Closest prior work
| L0001 | relabels every snapshot | updates labels from the change set | experiments/E001/results/metrics.json |
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

    def test_unverified_out_of_scope_items_do_not_block(self):
        base = {"verdict": "yes", "grade": "B", "issues": []}
        res = self.claim("C004", "B", {**base, "unverified": [
            {"item": "speedup inside the real solver", "claimed": False},
            {"item": "novelty of the seed rule", "claimed": False}]})
        self.assertEqual(res["decision"], "accepted", res)
        rec = read_json(self.cfg.layout.findings / res["finding"] / "finding.json")
        self.assertEqual(rec["not_claimed_unverified"], ["speedup inside the real solver", "novelty of the seed rule"])
        res = self.claim("C005", "B", {**base, "unverified": [{"item": "the headline slope", "claimed": True}]})
        self.assertEqual(res["decision"], "needs_work", res)
        res = self.claim("C006", "B", {**base, "unverified": ["an old-format item"]})          # before 0.6.2
        self.assertEqual(res["decision"], "needs_work")
        self.assertIn("run the review again", " ".join(res["reasons"]))

    def test_diagnosis_files_added_after_outcome_do_not_make_it_stale(self):
        res = self.claim("C007", "B")
        self.assertEqual(res["decision"], "accepted", res)
        e = self.cfg.layout.experiments / "E001"
        write(e / "analysis_diag" / "diagnose.py", "print('why')\n")        # post-outcome diagnosis
        outcome = read_json(e / "outcome.json")
        self.assertTrue(research.outcome_current(self.cfg, "E001", outcome))
        write(e / "plan_check.py", "x = 1\n")
        self.assertTrue(research.outcome_current(self.cfg, "E001", outcome))
        code = next(p for p in outcome["code_manifest"] if p.endswith(".py")) if any(
            p.endswith(".py") for p in outcome["code_manifest"]) else None
        if code:
            write(self.root / code, "changed = True\n")
            self.assertFalse(research.outcome_current(self.cfg, "E001", outcome))

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

    def test_review_counts_only_for_the_reviewed_version(self):
        res = self.claim("C010", "B", {"verdict": "yes", "grade": "B", "issues": []})
        self.assertEqual(res["decision"], "accepted", res)
        claim = self.cfg.layout.claims / "C010" / "claim.md"
        claim.write_text(claim.read_text(encoding="utf-8").replace("We^0.52", "We^0.9"), encoding="utf-8")
        res = gates.claim_gate(self.cfg, "C010")
        self.assertEqual(res["decision"], "needs_review", res)
        self.assertIn("claim", " ".join(res["reasons"]))
        claim.write_text(claim.read_text(encoding="utf-8").replace("We^0.9", "We^0.52"), encoding="utf-8")
        self.assertEqual(gates.claim_gate(self.cfg, "C010")["decision"], "accepted")      # back to the reviewed version
        write(self.cfg.layout.experiments / "E001" / "results" / "metrics.json", '{"slope": 0.91}')
        research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "Updated analysis")
        res = gates.claim_gate(self.cfg, "C010")
        self.assertEqual(res["decision"], "needs_review")
        self.assertIn("evidence:experiments/E001/results/metrics.json", " ".join(res["reasons"]))

    def test_accept_is_idempotent(self):
        first = self.claim("C011", "B")
        again = gates.claim_gate(self.cfg, "C011")
        self.assertEqual((first["decision"], again["decision"]), ("accepted", "accepted"))
        self.assertEqual(first["finding"], again["finding"])
        self.assertEqual(len(list(self.cfg.layout.findings.glob("F*"))), 1)

    def test_human_verdict_only_from_protected_zone_and_bound_to_version(self):
        res = self.claim("C012", "A", {"verdict": "yes", "grade": "C", "issues": []})
        self.assertEqual(res["decision"], "human_decision")
        fp = gates.fingerprint(self.cfg, "C012")["fingerprint"]
        vfile = self.cfg.layout.human / "verdicts" / "C012.toml"
        write(vfile, 'grade = "A"\nfingerprint = "0123456789abcdef"\n')         # another version
        res = gates.claim_gate(self.cfg, "C012")
        self.assertEqual(res["decision"], "human_decision")
        self.assertIn("another version", " ".join(res["reasons"]))
        write(vfile, f'grade = "A"\nfingerprint = "{fp[:16]}"\n')
        res = gates.claim_gate(self.cfg, "C012")
        self.assertEqual((res["decision"], res["grade"]), ("accepted", "A"))
        d = lambda tool, **inp: hooks.decide({"tool_name": tool, "tool_input": inp}, self.root)[0]
        self.assertFalse(d("Write", file_path="00_human/verdicts/C012.toml"))   # agents cannot write it

    def test_claim_needs_closest_prior_work(self):
        write(self.cfg.layout.claims / "C013" / "claim.md",
              '+++\nstatement = "x"\nexperiments = ["E001"]\nevidence = ["experiments/E001/results/metrics.json"]\n'
              'pi_grade = "B"\n[validation]\nmesh_convergence = "experiments/E001/results/mesh_convergence.json"\n+++\n')
        res = gates.claim_gate(self.cfg, "C013")
        self.assertEqual(res["decision"], "blocked")
        self.assertIn("Closest prior work", " ".join(res["reasons"]))

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
                      '#!/bin/bash\nif [[ "$*" == *State,CPUTimeRAW* ]]; then echo "4242|COMPLETED|36000"; '
                      'elif [[ "$*" == *CPUTimeRAW* ]]; then echo "4242|36000"; else echo COMPLETED; fi\n')
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
        write(self.cfg.layout.experiments / "E001" / "run.py", "print(1)\n")
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

    def test_core_hour_cap_counts_pending_jobs(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "+++\n\n## Prior", '\n[parts.A]\nrun_command = "echo a"\n[parts.B]\nrun_command = "echo b"\n'
            "+++\n\n## Prior"), encoding="utf-8")
        knowledge.check_plan(self.cfg, "E001")
        pend = write(self.bin / "sacct_pend3", "#!/bin/bash\necho '4242|PENDING|0'\n")
        pend.chmod(0o755)
        os.environ["AUTOLAB_SACCT"] = str(pend)
        each = slurm.resources(self.cfg, "E001", "A")
        per_job = each["billed_cpus"] * each["hours"]
        self.cfg.data["slurm"]["total_core_hours"] = per_job * 1.5
        slurm.submit(self.cfg, "E001", "A")                                   # pending: uses 0 but claims per_job
        with self.assertRaises(RuntimeError) as e:
            slurm.submit(self.cfg, "E001", "B")
        self.assertIn("reserved for unfinished jobs", str(e.exception))
        self.assertEqual(slurm.committed_core_hours(self.cfg)["reserved"], round(per_job, 2))

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

    def test_resources_override_after_lock(self):
        passing_plan(self.cfg)
        exp = self.cfg.layout.experiments / "E001"
        plan = exp / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            "+++\n\n## Prior", '\n[parts.D]\nrun_command = "echo replay"\n[parts.D.resources]\nmem_gb = 32\n'
            "+++\n\n## Prior"), encoding="utf-8")
        self.assertIn("mem_reason", " ".join(slurm.resources(self.cfg, "E001", "D")["problems"]))
        write(exp / "jobs.jsonl", "{}\n")                                       # plan now locked for agents
        self.assertFalse(hooks.decide({"tool_name": "Edit", "tool_input": {"file_path": "experiments/E001/plan.md"}},
                                      self.root)[0])
        ovf = write(exp / "resources_override.toml", '[parts.D.resources]\nmem_gb = 8\n')
        self.assertIn("reason", " ".join(slurm.resources(self.cfg, "E001", "D")["problems"]))
        ovf.write_text('reason = "peak 0.36 GB measured in pilot P"\n[parts.D.resources]\nmem_gb = 8\n', encoding="utf-8")
        r = slurm.resources(self.cfg, "E001", "D")
        self.assertEqual((r["mem_gb"], r["problems"]), (8, []))
        self.assertIn("pilot P", r["override"])
        self.assertEqual(slurm.resources(self.cfg, "E001")["mem_gb"], 8)       # other parts unchanged
        ovf.write_text('reason = "x"\n[parts.D.resources]\nrun_command = "rm -rf /"\n', encoding="utf-8")
        self.assertIn("resources only", " ".join(slurm.resources(self.cfg, "E001", "D")["problems"]))

    def test_missing_script_and_idle_cpu_warning(self):
        passing_plan(self.cfg)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        r = slurm.resources(self.cfg, "E001")
        self.assertIn("warning_cpus", r)                                       # 4 cpus for `echo run`
        plan.write_text(plan.read_text(encoding="utf-8").replace('run_command = "echo run"',
                        'run_command = "python -I run_x.py --seed $SLURM_ARRAY_TASK_ID"\n'
                        'analysis_command = "python analysis/verdicts.py"'), encoding="utf-8")
        probs = " ".join(slurm.resources(self.cfg, "E001")["problems"])
        self.assertIn("run_x.py", probs)
        self.assertIn("analysis/verdicts.py", probs)
        write(self.cfg.layout.experiments / "E001" / "run_x.py", "print(1)\n")
        write(self.cfg.layout.experiments / "E001" / "analysis" / "verdicts.py", "print(1)\n")
        self.assertEqual(slurm.resources(self.cfg, "E001")["problems"], [])
        plan.write_text(plan.read_text(encoding="utf-8").replace("cpus = 4", 'cpus = 4\ncpus_reason = "3 cores kept idle for timing isolation"'),
                        encoding="utf-8")
        self.assertNotIn("warning_cpus", slurm.resources(self.cfg, "E001"))
        plan.write_text(plan.read_text(encoding="utf-8").replace('cpus_reason = "3 cores kept idle for timing isolation"', "")
                        .replace("python -I run_x.py", "srun python -I run_x.py"), encoding="utf-8")
        self.assertNotIn("warning_cpus", slurm.resources(self.cfg, "E001"))

    def test_uncommitted_code_lists_files_not_outputs(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        run = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], capture_output=True, text=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        e = self.cfg.layout.experiments / "E001"
        write(e / "run.py", "print(1)\n")
        write(e / "results" / "metrics.json", "{}\n")
        write(e / "pilot" / "frames.csv", "a\n")
        self.assertEqual(slurm.uncommitted_code(self.root, ["experiments/E001", "src"]), ["experiments/E001/run.py"])

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

    def test_interpreter_writes_to_protected_paths(self):
        self.assertFalse(self.d("Bash", command="python3 -c \"from pathlib import Path; Path('autolab.toml').write_text('x')\""))
        self.assertFalse(self.d("Bash", command="python - <<'EOF'\nopen('findings/F001/claim.md','w').write('x')\nEOF"))
        self.assertTrue(self.d("Bash", command="python3 analysis.py --in findings/F001/finding.json 2>&1 | tail -3"))
        self.assertTrue(self.d("Bash", command="python3 -c \"print(open('autolab.toml').read())\""))

    def test_sandbox_settings_and_edit_denies(self):
        project._write_settings(self.cfg)
        s = read_json(self.root / ".claude" / "settings.json")
        root = self.root.resolve().as_posix()
        self.assertIn(f"Edit(//{root.lstrip('/')}/autolab.toml)", s["permissions"]["deny"])
        self.assertNotIn("sandbox", s)
        self.cfg.data["agents"]["sandbox"] = True
        project._write_settings(self.cfg)
        sb = read_json(self.root / ".claude" / "settings.json")["sandbox"]
        self.assertTrue(sb["enabled"] and sb["failIfUnavailable"] and not sb["allowUnsandboxedCommands"])
        self.assertIn(f"{root}/findings/**", sb["filesystem"]["denyWrite"])
        self.assertIn("autolab *", sb["excludedCommands"])

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

    def test_budget_unknown_cost_reserve_and_turn_cap(self):
        from autolab import budget
        self.cfg.data["budget"]["total_usd"] = 20.0
        ok = write(self.root / "state" / "pi_logs" / "a.jsonl", '{"type":"result","total_cost_usd":4.0}\n')
        budget.record_turn(self.cfg, ok)
        killed = write(self.root / "state" / "pi_logs" / "b.jsonl",
                       '{"type":"assistant","message":{"id":"m1","model":"claude-opus","usage":{"input_tokens":10,'
                       '"output_tokens":5,"cache_read_input_tokens":100}}}\n'
                       '{"type":"assistant","message":{"id":"m1","model":"claude-opus","usage":{"input_tokens":10,'
                       '"output_tokens":5,"cache_read_input_tokens":100}}}\n')
        rec = budget.record_turn(self.cfg, killed)
        self.assertTrue(rec["unknown"])
        self.assertEqual(rec["usage"]["claude-opus"]["input_tokens"], 10)        # deduplicated by message id
        st = budget.status(self.cfg)
        self.assertEqual(st["unknown_cost_turns"], 1)
        self.assertAlmostEqual(st["spent_usd"], 4.0 + 5.0)                      # unknown turn charged provisionally
        self.assertAlmostEqual(budget.turn_cap(self.cfg), 11.0)
        self.cfg.data["budget"]["total_usd"] = 13.0
        self.assertFalse(budget.status(self.cfg)["over"])
        self.assertTrue(budget.status(self.cfg, reserve=True)["over"])          # 9 + reserve 5 >= 13

    def test_guard_restores_config_and_pauses_on_findings(self):
        from autolab import guard
        guard.snapshot(self.cfg)
        toml = self.root / "autolab.toml"
        original = toml.read_text(encoding="utf-8")
        toml.write_text(original.replace("total_usd = 0.0", "total_usd = 9999.0"), encoding="utf-8")
        log = write(self.root / "state" / "pi_logs" / "g.jsonl", json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "x", "name": "Bash",
             "input": {"command": "python -c \"open('autolab.toml','w')\""}}]}}) + "\n")
        res = guard.check(self.cfg, log)
        self.assertIn("autolab.toml", res["restored"])
        self.assertEqual(toml.read_text(encoding="utf-8"), original)
        self.assertFalse((self.cfg.layout.human / "STOP").exists())
        guard.snapshot(self.cfg)
        write(self.cfg.layout.findings / "F777" / "finding.json", "{}")       # no `autolab gate claim` ran
        res = guard.check(self.cfg, log)
        self.assertTrue(any(v["file"].startswith("findings/") for v in res["violations"]))
        self.assertTrue((self.cfg.layout.human / "STOP").exists())
        (self.cfg.layout.human / "STOP").unlink()
        guard.snapshot(self.cfg)
        write(self.cfg.layout.human / "GOAL.md", "# changed by the human\n")      # not in the log: no violation
        self.assertTrue(guard.check(self.cfg, log)["violations"])  # ambiguous edits require operator review

    def test_guard_human_edits_mid_turn_do_not_stop_the_lab(self):
        from autolab import guard
        def turn(*calls):
            return write(self.root / "state" / "pi_logs" / "h.jsonl", "\n".join(json.dumps(
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": str(i), "name": n, "input": inp}]}})
                for i, (n, inp) in enumerate(calls)) + "\n")
        guard.snapshot(self.cfg)
        write(self.cfg.layout.inbox / "note.md", "# from the human\n")                 # uploaded during the turn
        log = turn(("Bash", {"command": "cat 00_human/inbox/note.md"}), ("Read", {"file_path": "00_human/RULES.md"}))
        res = guard.check(self.cfg, log)
        self.assertFalse(res["serious"])
        self.assertEqual([v["kind"] for v in res["violations"]], [guard.HUMAN_EDIT])
        self.assertFalse((self.cfg.layout.human / "STOP").exists())
        self.assertFalse(any("BLOCKING" in a for a in human.open_actions(self.cfg)))
        rc, _ = run_cli("--project", str(self.root), "guard", "check", str(log))
        guard.snapshot(self.cfg)
        write(self.cfg.layout.human / "RULES.md", "# rewritten\n")
        res = guard.check(self.cfg, turn(("Write", {"file_path": str(self.cfg.layout.human / "RULES.md")})))
        self.assertTrue(res["serious"])                                                  # an agent wrote there
        self.assertTrue((self.cfg.layout.human / "STOP").exists())
        (self.cfg.layout.human / "STOP").unlink()
        guard.snapshot(self.cfg)
        write(self.cfg.layout.human / "GOAL.md", "# again\n")
        self.assertTrue(guard.check(self.cfg, turn(("Bash", {"command": "echo x > 00_human/GOAL.md"})))["serious"])
        (self.cfg.layout.human / "STOP").unlink()
        guard.snapshot(self.cfg)                                                         # the receipted upload path
        src = write(self.root.parent / "outside" / "plan-note.md", "# note\n")
        rc, out = run_cli("--project", str(self.root), "human", "add", str(src))
        self.assertEqual((rc, out.strip()), (0, "00_human/inbox/plan-note.md"))
        self.assertEqual(guard.check(self.cfg, turn())["violations"], [])
        with self.assertRaises(ValueError):
            human_add = run_cli("--project", str(self.root), "human", "add", str(src), "--dest", "../autolab.toml")
            if human_add[0] == 1:
                raise ValueError
        d = lambda cmd: hooks.decide({"tool_name": "Bash", "tool_input": {"command": cmd}}, self.root)[0]
        self.assertFalse(d("autolab human add notes.md"))                              # agents may not use it

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
        self.assertEqual(rc, 1)                                              # blocking needs a recommendation
        rc, out = run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "--rule", "goal-adjacent",
                          "--recommend", "stop E003", "--title", "Stop or modify?", "E003 slower than baseline")
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


def _ids_worker(ids_file: str, n: int) -> list[str]:
    from autolab.util import next_id
    return [next_id(Path(ids_file), "review", width=4) for _ in range(n)]


def skill_log(cfg: Config, *names: str, task: str | None = None) -> Path:
    """A turn log with Skill-tool calls, shaped like claude -p stream-json. With `task`, the calls
    come from a student dispatched by an Agent call whose description names that task."""
    lines = []
    parent = None
    if task:
        parent = f"agent-{task}-{time.time_ns()}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": parent, "name": "Agent", "input": {"description": f"{task} work", "prompt": "go"}}]}}))
    lines += [json.dumps({"type": "assistant", "parent_tool_use_id": parent, "message": {"content": [
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
        rev = read_frontmatter(tasks.new(self.cfg, "Systematic review", "literature", task="review"))[0]
        self.assertIn("literature-review", rev["skills"])
        rig = read_frontmatter(tasks.new(self.cfg, "Rigour review of C001", "analysis", task="rigor",
                                         difficulty="hard"))[0]
        self.assertIn("ara-rigor-reviewer", rig["skills"])
        from autolab import skills
        self.assertIn("brainstorming-research-ideas", skills.resolve(self.cfg, "pi", "ideation"))
        self.assertEqual(skills.check(self.cfg), [])

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
        tid = read_frontmatter(card_dir / "task.md")[0]["id"]
        skill_log(self.cfg, "analyze-results", task=tid)                            # now logs exist
        res = handoff.validate_in(self.cfg, card)
        self.assertFalse(res["ok"])
        self.assertIn("never invoked", " ".join(res["problems"]))
        skill_log(self.cfg, "verification-before-completion", task="T999")         # another student's call
        self.assertFalse(handoff.validate_in(self.cfg, card)["ok"])
        skill_log(self.cfg, "verification-before-completion")                      # the PI's own call
        self.assertFalse(handoff.validate_in(self.cfg, card)["ok"])
        skill_log(self.cfg, "verification-before-completion", task=tid)
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

    def test_ids_unique_under_concurrency(self):
        import concurrent.futures as cf
        ids_file = self.cfg.layout.ids
        with cf.ProcessPoolExecutor(4) as ex:
            got = [i for chunk in ex.map(_ids_worker, [str(ids_file)] * 4, [25] * 4) for i in chunk]
        self.assertEqual(len(got), 100)
        self.assertEqual(len(set(got)), 100)
        from autolab.util import write_new
        write_new(self.root / "state" / "once.txt", "a")
        with self.assertRaises(FileExistsError):
            write_new(self.root / "state" / "once.txt", "b")

    def test_synonyms_widen_context_packs(self):
        knowledge.lit_add(self.cfg, "Pinch-off dynamics of liquid threads", "10.1/pinch", 2020, "B", "theory", "oa",
                          ["pinch-off"])
        pack = knowledge.build_context_pack(self.cfg, "fragmentation statistics", ["fragmentation"])
        self.assertIn("Pinch-off dynamics of liquid threads", [i["title"] for i in pack["items"]])

    def test_review_pack_has_standards(self):
        from autolab import reviewer
        pack = reviewer.review_pack(self.cfg, "claim_support")
        self.assertIn(".claude/skills/experiment-audit/SKILL.md", pack)
        self.assertIn("00_human/TASTE.md", pack)
        self.assertIn("uncalibrated", pack)                                         # no taste examples yet
        self.assertIn("unverified", pack)

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
                               "RETRO-25", ["timing", "seeds", "bubble"], {"data": "synthetic V1", "resolution": "256^3"},
                               recheck="real data or other resolutions")
        pack = knowledge.build_context_pack(self.cfg, "bubble timing study", ["bubble", "timing", "seeds"])
        self.assertIn(lid, [i["id"] for i in pack["items"]])
        md = (self.cfg.layout.context_packs / f"{pack['id']}.md").read_text(encoding="utf-8")
        self.assertIn("only under: data=synthetic V1, resolution=256^3", md)

    def test_tick_reports_skill_use(self):
        from autolab import tick
        tick.run(self.cfg)
        skill_log(self.cfg, "analyze-results", "analyze-results")
        res = tick.run(self.cfg)
        digest = Path(res["digest"]).read_text(encoding="utf-8")
        self.assertIn("analyze-results x2", digest)


def finish_jobs(cfg: Config, eid: str, job: str = "777") -> None:
    """Pretend one job of `eid` was submitted and finished (usage recorded)."""
    from autolab.util import append_jsonl, now_iso
    exp = cfg.layout.experiments / eid
    append_jsonl(exp / "jobs.jsonl", {"job": job, "eid": eid, "submitted": now_iso(),
                                      "resources": {"total_cpus": 4, "billed_cpus": 4, "hours": 1}})
    append_jsonl(exp / "jobs_usage.jsonl", {"job": job, "state": "COMPLETED", "elapsed_h": 0.5,
                                            "core_hours": 2.0, "requested": {"total_cpus": 4}})


class ResearchSteering(unittest.TestCase):
    """Candidate registry, discrimination, frozen evaluator, outcomes, tree, scoped lessons, metrics."""

    def setUp(self):
        self.root, self.cfg = make_project()

    def test_failed_secondary_array_task_can_be_excused(self):
        from autolab.util import append_jsonl, now_iso
        passing_plan(self.cfg)
        exp = self.cfg.layout.experiments / "E001"
        append_jsonl(exp / "jobs.jsonl", {"job": "900", "eid": "E001", "submitted": now_iso(), "resources": {"total_cpus": 2}})
        append_jsonl(exp / "jobs_usage.jsonl", {"job": "900", "state": "OUT_OF_MEMORY", "elapsed_h": 1, "core_hours": 2})
        fake = write(Path(tempfile.mkdtemp()) / "sacct", "#!/bin/bash\nprintf '900_0|COMPLETED\\n900_1|OUT_OF_MEMORY\\n'\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["AUTOLAB_SACCT"] = str(fake)
        self.addCleanup(os.environ.pop, "AUTOLAB_SACCT", None)
        with self.assertRaises(ValueError):                                   # the OOM task is not excused
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "face task fine")
        with self.assertRaises(ValueError):                                   # an excuse needs a reason
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "x", failed_ok={"900_1": ""})
        rec = research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "face task fine",
                                      failed_ok={"900_1": "secondary full-connectivity task, out of memory"})
        self.assertEqual(rec["excused_failures"], {"900_1": "secondary full-connectivity task, out of memory"})
        self.assertIn("excused failures: 900_1", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):                                   # nothing completed at all
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "x",
                                    failed_ok={"900_0": "a", "900_1": "b"})

    def test_decision_amends_and_standby_hint(self):
        d1 = tasks.decide(self.cfg, "run E030 on standby", "short job")
        d2 = tasks.decide(self.cfg, "run E030 on normal QOS", "needs 6 h", amends=d1)
        self.assertIn(f"[amends {d1}] run E030 on normal QOS", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        with self.assertRaises(KeyError):
            tasks.decide(self.cfg, "x", "y", amends="D999")
        _, cfg = make_project(site="negishi")
        import datetime as dt
        later = (dt.datetime.now() + dt.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        hint = slurm._start_hint(f"sbatch: Job 1 to start at {later} using 4 processors", {"qos": "", "hours": 2}, cfg)
        self.assertIn('qos = "standby"', hint)
        self.assertEqual(slurm._start_hint("sbatch: Job 1 to start at 2000-01-01T00:00:00", {"hours": 2}, cfg), "")

    def test_plan_needs_candidate_cards_and_discrimination(self):
        res, _ = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res)
        plan = self.cfg.layout.experiments / "E001" / "plan.md"
        text = plan.read_text(encoding="utf-8")
        hyp = self.cfg.layout.state / "HYPOTHESES.md"
        write(hyp, hyp.read_text(encoding="utf-8") + "| H2 | - | Reynolds model | open | - | - | - |\n")
        plan.write_text(text.replace('hypotheses = ["H1"]', 'hypotheses = ["H1", "H2"]'), encoding="utf-8")
        self.assertIn("state/candidates/H2.md", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        research.new_candidate(self.cfg, "H2", "Reynolds model")            # card exists but is empty
        self.assertIn("candidate H2: fill", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        plan.write_text(text.split("## Discrimination")[0], encoding="utf-8")
        self.assertIn("Discrimination", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        plan.write_text(text.replace('hypotheses = ["H1"]', 'hypotheses = ["H1"]\nparent = "E000"'), encoding="utf-8")
        self.assertIn("parent experiment E000", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        research.set_candidate(self.cfg, "H1", "killed", "slope far outside the range")
        plan.write_text(text, encoding="utf-8")
        self.assertIn("killed", " ".join(knowledge.check_plan(self.cfg, "E001")["problems"]))
        self.assertIn("candidate H1 -> killed", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        self.assertEqual(research.discrimination_problems(
            "## Discrimination\nSeparates A from B because they predict opposite signs of the drift.\n"
            "If it supports A we scale up; if it refutes A we drop the method.\n"), [])

    def test_outcome_layers_and_only_goal_changes_wait(self):
        passing_plan(self.cfg)
        with self.assertRaises(ValueError):                                  # invalid run, research conclusion
            research.record_outcome(self.cfg, "E001", "invalid", "refutes", "refine", "OOM")
        with self.assertRaises(ValueError):                                  # invalid run cannot kill a line
            research.record_outcome(self.cfg, "E001", "invalid", "n/a", "stop", "OOM")
        rec = research.record_outcome(self.cfg, "E001", "invalid", "n/a", "refine", "node ran out of memory")
        self.assertFalse(rec.get("needs_human"))
        # stopping or redirecting a sub-direction is the PI's call: a notification, no request
        rec = research.record_outcome(self.cfg, "E001", "valid", "refutes", "pivot", "slope 0.1, far below",
                                      ["experiments/E001/plan.md"])
        self.assertFalse(rec.get("needs_human"))
        self.assertEqual(human.open_actions(self.cfg), [])
        notes = read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")
        self.assertIn("PI 已决定转向 E001", notes[-1]["text"])
        self.assertIn("E001 outcome", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        self.assertEqual(len(read_jsonl(self.cfg.layout.experiments / "E001" / "outcomes.jsonl")), 2)
        # changing the research goal waits for the human, without a timeout (always_ask)
        rec = research.record_outcome(self.cfg, "E001", "valid", "refutes", "stop", "goal is covered by L0005",
                                      changes_goal=True)
        self.assertTrue(rec["needs_human"])
        acts = human.open_actions(self.cfg)
        self.assertTrue(any("BLOCKING" in a and "no timeout" in a for a in acts), acts)
        from autolab import autonomy
        aid = autonomy.blocked(self.cfg)[0]
        self.assertIsNone(autonomy.read_registry(self.cfg)[aid]["deadline"])

    def test_frozen_evaluator_needs_amendment(self):
        passing_plan(self.cfg)
        exp = self.cfg.layout.experiments / "E001"
        plan = exp / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            'run_command = "echo run"', 'run_command = "echo run"\nanalysis_command = "python analysis/analyze.py"'),
            encoding="utf-8")
        write(exp / "analysis" / "analyze.py", "print('mean')\n")
        rec = research.freeze_evaluator(self.cfg, "E001")
        self.assertIn("analysis/analyze.py", rec["files"])
        write(exp / "analysis" / "analyze.py", "print('median')\n")       # changed after submission
        with self.assertRaises(ValueError):
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "slope 0.5")
        write(exp / "plan_amendments.md", "2026-10-17: analysis/analyze.py uses the median: heavy tails seen in "
                                          "the pilot, decided before reading the full results.\n")
        from autolab.util import sha256_file
        write(exp / "plan_amendments.md", f"2026-10-17 analysis/analyze.py {rec['files']['analysis/analyze.py']} "
              f"{sha256_file(exp / 'analysis' / 'analyze.py')} reason: robust estimator for heavy tails\n")
        self.assertEqual(research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "slope 0.5")
                         ["hypothesis"], "supports")
        d = lambda tool, **inp: hooks.decide({"tool_name": tool, "tool_input": inp}, self.root)[0]
        self.assertFalse(d("Edit", file_path="experiments/E001/evaluator.json"))
        self.assertFalse(d("Bash", command="echo {} > experiments/E001/outcome.json"))
        self.assertFalse(d("Write", file_path="experience/lessons.jsonl"))
        self.assertTrue(d("Bash", command="autolab outcome E001 --engineering valid --hypothesis n/a --value refine --why x"))
        self.assertTrue(d("Bash", command="cat experiments/E001/outcomes.jsonl"))

    def test_locked_plan_answers_new_items_in_amendments(self):
        from autolab.util import append_jsonl, now_iso
        res, pack = passing_plan(self.cfg)
        self.assertTrue(res["ok"], res)
        exp = self.cfg.layout.experiments / "E001"
        append_jsonl(exp / "jobs.jsonl", {"job": "1", "eid": "E001", "submitted": now_iso()})   # plan now locked
        lid = tasks.add_lesson(self.cfg, "Bubble breakup timings need pinned cores", "E001", ["bubble", "breakup"],
                               {"resources": "pinned cores"}, recheck="other nodes")
        res = knowledge.check_plan(self.cfg, "E001")
        self.assertIn(lid, " ".join(res["problems"]))
        self.assertIn("plan_amendments.md", " ".join(res["problems"]))
        write(exp / "plan_amendments.md", f"## A1 (PI): prior knowledge {lid}, added after the lock\n"
                                          f"{lid} applies: timings here already ran on pinned cores, no change to the rule.\n")
        self.assertTrue(knowledge.check_plan(self.cfg, "E001")["ok"])
        # an item that was in the pack before the lock must still be answered in the plan itself
        plan = exp / "plan.md"
        text = plan.read_text(encoding="utf-8")
        if pack["items"]:
            first = pack["items"][0]["id"]
            plan.write_text("\n".join(l for l in text.splitlines() if first not in l) + "\n", encoding="utf-8")
            write(exp / "plan_amendments.md", (exp / "plan_amendments.md").read_text(encoding="utf-8")
                  + f"{first} answered only here, which does not count for an original pack item.\n")
            self.assertFalse(knowledge.check_plan(self.cfg, "E001")["ok"])

    def test_legacy_experiment_without_evaluator_can_be_closed(self):
        from autolab.util import append_jsonl
        passing_plan(self.cfg)
        exp = self.cfg.layout.experiments / "E001"
        plan = exp / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace(
            'run_command = "echo run"', 'run_command = "echo run"\nanalysis_command = "python analysis/analyze.py"'),
            encoding="utf-8")
        write(exp / "analysis" / "analyze.py", "print('mean')\n")
        finish_jobs(self.cfg, "E001")                                         # submitted now: not legacy
        with self.assertRaises(ValueError):
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "slope 0.5")
        (exp / "jobs.jsonl").unlink()                                         # first job before 0.5.0
        append_jsonl(exp / "jobs.jsonl", {"job": "777", "eid": "E001", "submitted": "2026-10-16T23:51:49-04:00",
                                          "resources": {"total_cpus": 4}})
        self.assertEqual(research.legacy_unfrozen(self.cfg, "E001"), "2026-10-16T23:51:49-04:00")
        rec = research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "slope 0.5")
        self.assertTrue(rec["legacy"])
        self.assertEqual(rec["evaluator"], "legacy-unfrozen")
        self.assertTrue(any(w.startswith("legacy:") for w in rec["warnings"]))
        self.assertTrue(read_json(exp / "outcome.json")["legacy"])
        self.assertEqual(research.missing_outcomes(self.cfg), [])

    def test_finished_experiment_blocks_new_plans_and_tree(self):
        passing_plan(self.cfg)
        finish_jobs(self.cfg, "E001")
        self.assertEqual(research.missing_outcomes(self.cfg), ["E001"])
        res, _ = passing_plan(self.cfg, "E002")
        self.assertIn("without an outcome", " ".join(res["problems"]))
        research.record_outcome(self.cfg, "E001", "valid", "supports", "refine", "slope 0.55, CI 0.48-0.62")
        plan = self.cfg.layout.experiments / "E002" / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace('hypotheses = ["H1"]', 'hypotheses = ["H1"]\nparent = "E001"'),
                        encoding="utf-8")
        self.assertTrue(knowledge.check_plan(self.cfg, "E002")["ok"])
        finish_jobs(self.cfg, "E001", job="778")                             # a rerun needs a new outcome
        self.assertEqual(research.missing_outcomes(self.cfg), ["E001"])
        text = research.tree(self.cfg)
        self.assertIn("- E001 [H1] valid/supports/refine, 4.0 ch", text)
        self.assertIn("  - E002 [H1] open", text)
        digest = Path(tick.run(self.cfg)["digest"]).read_text(encoding="utf-8")
        self.assertIn("Finished experiments without an outcome: E001", digest)
        self.assertIn("H1 [open]", digest)

    def test_scoped_lessons(self):
        with self.assertRaises(ValueError):                                  # no conditions
            tasks.add_lesson(self.cfg, "screening helps", "E003", ["screen"], recheck="x")
        with self.assertRaises(ValueError):                                  # unknown condition key
            tasks.add_lesson(self.cfg, "screening helps", "E003", ["screen"], {"mood": "good"}, recheck="x")
        with self.assertRaises(ValueError):                                  # vague origin
            tasks.add_lesson(self.cfg, "screening helps", "a feeling", ["screen"], {"data": "V1"}, recheck="x")
        write(self.root / "experiments" / "E003" / "summary.json", "{}")
        lid = tasks.add_lesson(self.cfg, "Foreground-only screening saves 30%", "E003", ["screen"],
                               {"data": "synthetic V1", "resolution": "256^3"}, ["experiments/E003/summary.json"],
                               "medium", "real 3D data, where the gas region is large")
        tasks.lesson_event(self.cfg, lid, "counter", "E004", "no gain on the real 3D case", "low")
        tasks.lesson_event(self.cfg, lid, "support", "E005", "same gain at 384^3 synthetic")
        l = next(x for x in tasks.lessons(self.cfg) if x["id"] == lid)
        self.assertEqual((len(l["support"]), len(l["counter"]), l["confidence"]), (1, 1, "low"))
        self.assertIn("experiments/E003/summary.json", l["evidence"])
        tasks.lesson_event(self.cfg, lid, "retire", "E006", "superseded by the new screen")
        self.assertEqual([x["id"] for x in tasks.lessons(self.cfg)], [])
        rc, out = run_cli("--project", str(self.root), "lesson", "list", "--all")
        self.assertIn("(retired)", out)

    def test_metrics_and_call_record(self):
        from autolab import budget
        passing_plan(self.cfg)
        log = write(self.cfg.layout.state / "pi_logs" / "t1.jsonl", "\n".join([
            json.dumps({"type": "assistant", "message": {"id": "m1", "model": "claude-opus", "usage": {"input_tokens": 10, "output_tokens": 5}}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 1.5,
                        "duration_ms": 61000, "num_turns": 7})]) + "\n")
        killed = write(self.cfg.layout.state / "pi_logs" / "t2.jsonl", json.dumps(
            {"type": "assistant", "message": {"id": "m2", "model": "claude-opus", "usage": {"input_tokens": 3}}}) + "\n")
        budget.record_turn(self.cfg, log)
        budget.record_turn(self.cfg, killed)
        calls = read_jsonl(self.cfg.layout.state / "calls.jsonl")
        self.assertEqual([c["status"] for c in calls], ["ok", "no_result"])
        self.assertEqual(calls[0]["duration_s"], 61.0)
        self.assertEqual(calls[0]["models"], ["claude-opus"])
        finish_jobs(self.cfg, "E001")
        research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "slope 0.5")
        rc, out = run_cli("--project", str(self.root), "report", "metrics", "--json")
        m = json.loads(out)
        self.assertEqual(m["experiments"]["valid_hypothesis_tests"], 1)
        self.assertEqual(m["calls"]["pi_turn"], {"ok": 1, "no_result": 1})
        self.assertEqual(m["cost"]["core_hours"], 2.0)
        self.assertTrue((self.cfg.layout.state / "METRICS.md").is_file())

    def test_candidate_cli(self):
        rc, out = run_cli("--project", str(self.root), "candidate", "new", "H7", "--statement", "Landmarks certify connectivity",
                          "--closest-work", "L0052 dynamic connectivity", "--mechanism-difference", "geometric landmarks",
                          "--key-uncertainty", "landmark density", "--discriminating-experiment", "replay E004 D",
                          "--continue-if", "skip rate > 50%", "--kill-if", "skip rate < 10%")
        self.assertEqual(rc, 0)
        self.assertIn("H7", (self.cfg.layout.state / "HYPOTHESES.md").read_text(encoding="utf-8"))
        rc, _ = run_cli("--project", str(self.root), "candidate", "check", "H7")
        self.assertEqual(rc, 0)
        rc, out = run_cli("--project", str(self.root), "candidate", "list")
        self.assertIn("H7 [open]", out)


class Autonomy(unittest.TestCase):
    """Blocking requests carry a recommendation and time out; reminders; wake behaviour."""

    def setUp(self):
        self.root, self.cfg = make_project()
        from autolab import autonomy
        self.autonomy = autonomy

    def _expire(self, aid):
        reg = self.autonomy.read_registry(self.cfg)
        reg[aid]["deadline"] = "2000-01-01T00:00:00+00:00"
        write_json(self.cfg.layout.state / "requests.json", reg)

    def test_blocking_request_needs_recommendation_unless_always_ask(self):
        rc, _ = run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "--blocking",
                        "--rule", "pilot", "Run the L8 pilot?")
        self.assertEqual(rc, 1)                                              # no --recommend
        rc, out = run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "--blocking",
                          "--rule", "pilot", "--recommend", "run the 8 core-hour pilot", "Run the L8 pilot?")
        self.assertEqual(rc, 0)
        aid = out.strip()
        meta = self.autonomy.read_registry(self.cfg)[aid]
        self.assertTrue(meta["deadline"])
        line = next(a for a in human.open_actions(self.cfg) if aid in a)
        self.assertIn("PI recommends: run the 8 core-hour pilot", line)
        rc, out = run_cli("--project", str(self.root), "human", "request", "--kind", "decision", "--blocking",
                          "--rule", "gpu", "Use two GPU nodes?")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.autonomy.read_registry(self.cfg)[out.strip()]["deadline"])

    def test_timeout_closes_request_records_decision_and_notifies(self):
        aid = human.request_action(self.cfg, "decision", "Draft the report now or after L8?", True,
                                   rule="report", recommend="draft now, add L8 later", enforce=True)
        forever = human.request_action(self.cfg, "decision", "Raise budget to 600 USD?", True,
                                       rule="budget_increase", enforce=True)
        self.assertEqual(self.autonomy.overdue(self.cfg), [])
        self._expire(aid)
        self.assertEqual(self.autonomy.overdue(self.cfg), [aid])
        res = tick.run(self.cfg)
        digest = Path(res["digest"]).read_text(encoding="utf-8")
        self.assertIn("timed out", digest)
        self.assertIn("draft now, add L8 later", digest)
        acts = human.open_actions(self.cfg)
        self.assertFalse(any(aid in a for a in acts))
        self.assertTrue(any(forever in a for a in acts))                    # always_ask: still waiting
        self.assertIn("— timeout", (self.cfg.layout.state / "DECISIONS.md").read_text(encoding="utf-8"))
        self.assertIn("超时", read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")[-1]["text"])
        self.assertEqual(self.autonomy.process(self.cfg), [])               # only once

    def test_legacy_requests_adopt_deadline_and_framework_requests_wait(self):
        with open(self.cfg.layout.action_needed, "a", encoding="utf-8") as f:
            f.write("- [ ] **A101** [BLOCKING] [decision] Approve the writing stage? PI recommends (a).\n")
            f.write("- [ ] **A102** [BLOCKING] [decision] Claim C001: write 00_human/verdicts/C001.toml\n")
        self.assertEqual(sorted(self.autonomy.adopt_legacy(self.cfg)), ["A101", "A102"])
        reg = self.autonomy.read_registry(self.cfg)
        self.assertTrue(reg["A101"]["deadline"])
        self.assertIsNone(reg["A102"]["deadline"])
        self.assertEqual(self.autonomy.adopt_legacy(self.cfg), [])

    def test_legacy_recommendation_extracted_and_notice_in_chinese(self):
        ex = self.autonomy.extract_recommendation
        self.assertEqual(ex("- [ ] **A027** [BLOCKING] [decision] Options (a) (b) (c). My recommendation: (a) now, "
                            "(b) later only if the report says continue. Track C waits.  _(added 2026-10-17)_"),
                         "(a) now, (b) later only if the report says continue")
        self.assertEqual(ex("Proposed verdict: MODIFY. Options: (a) draft; (b) L8. Recommendation: (a) plus (b) in parallel."),
                         "(a) plus (b) in parallel")
        self.assertEqual(ex("E011: the PI proposes to STOP this line (hypothesis supports). Reason: x."),
                         "STOP this line (hypothesis supports)")
        self.assertEqual(ex("Approve the writing stage?"), "")
        with open(self.cfg.layout.action_needed, "a", encoding="utf-8") as f:
            f.write("- [ ] **A101** [BLOCKING] [decision] Report now or after L8? Recommendation: draft now, L8 in parallel.\n")
        write_json(self.cfg.layout.state / "notification_copy.json", {
            "action:A101:abc": {"summary": "请决定现在写报告还是先跑 L8。", "background": "L8 试跑约 4–11 核时。"}})
        self.autonomy.adopt_legacy(self.cfg)
        self.assertEqual(self.autonomy.read_registry(self.cfg)["A101"]["recommend"], "draft now, L8 in parallel")
        self._expire("A101")
        self.autonomy.process(self.cfg)
        text = read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")[-1]["text"]
        self.assertIn("A101（请决定现在写报告还是先跑 L8。）超时未回复", text)
        self.assertIn("推荐方案：draft now, L8 in parallel", text)
        self.assertIn("L8 试跑约 4–11 核时", text)

    def test_wake_on_deadline_and_longer_idle_when_blocked(self):
        from autolab import wake
        human.check_changes(self.cfg, now=time.time() + 3600)                 # settle and acknowledge the setup
        human.ack(self.cfg, "read")
        wake.mark(self.cfg)
        snap = read_json(self.cfg.layout.state / "wake.json")
        snap["t"] -= 60 * 60                                                 # last turn an hour ago
        write_json(self.cfg.layout.state / "wake.json", snap)
        go, reasons = wake.check(self.cfg, 45)
        self.assertTrue(any("no PI turn" in r for r in reasons))
        aid = human.request_action(self.cfg, "decision", "x?", True, rule="r", recommend="do y", enforce=True)

        def last_turn(decided):
            if decided:
                tasks.decide(self.cfg, "move track B", "work available")
            wake.mark(self.cfg)
            s = read_json(self.cfg.layout.state / "wake.json")
            s["t"] -= 60 * 60
            write_json(self.cfg.layout.state / "wake.json", s)

        last_turn(decided=True)                                             # a productive turn: keep 45 min
        go, reasons = wake.check(self.cfg, 45)
        self.assertTrue(any("no PI turn" in r for r in reasons), reasons)
        last_turn(decided=False)                                            # nothing decided: only waiting
        go, reasons = wake.check(self.cfg, 45)
        self.assertFalse(go, reasons)                                       # 180 min apart
        self._expire(aid)
        go, reasons = wake.check(self.cfg, 45)
        self.assertTrue(any("past their deadline" in r for r in reasons))

    def test_agents_cannot_edit_the_deadline_registry(self):
        d = lambda tool, **inp: hooks.decide({"tool_name": tool, "tool_input": inp}, self.root)[0]
        self.assertFalse(d("Edit", file_path="state/requests.json"))
        self.assertFalse(d("Bash", command="echo {} > state/requests.json"))
        self.assertTrue(d("Bash", command="cat state/requests.json"))

    def test_reminders(self):
        aid = human.request_action(self.cfg, "decision", "x?", True, rule="r", recommend="do y", enforce=True)
        self.assertEqual(self.autonomy.remind(self.cfg), [])
        reg = self.autonomy.read_registry(self.cfg)
        reg[aid]["reminded"] = "2000-01-01T00:00:00+00:00"
        write_json(self.cfg.layout.state / "requests.json", reg)
        self.assertEqual(self.autonomy.remind(self.cfg), [aid])
        self.assertIn("提醒", read_jsonl(self.cfg.layout.state / "human_notifications.jsonl")[-1]["text"])
        self.assertEqual(self.autonomy.remind(self.cfg), [])

    def test_protocol_names_always_ask_and_timeout(self):
        text = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("budget_increase, gpu, new_data_source, research_goal", text)
        self.assertIn("times out after 4 h", text)
        self.assertNotIn("{{", text)


if __name__ == "__main__":
    unittest.main()
