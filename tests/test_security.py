"""Regressions for the external review; no paid API calls or real scheduler jobs."""
import json
import os
import subprocess
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import test_autolab as fixtures
from test_autolab import write
from autolab import budget, gates, guard, hooks, knowledge, project, research, reviewer, slurm
from autolab.util import FileLock, read_json, read_jsonl, write_json, sha256_file, append_jsonl


class Security(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ClaimGate()
        self.case.setUp()
        self.root, self.cfg = self.case.root, self.case.cfg
        p = patch.object(guard, "_dir", side_effect=lambda cfg: cfg.layout.root.parent / "guard")
        p.start()
        self.addCleanup(p.stop)

    def allowed(self, tool, **data):
        return hooks.decide({"tool_name": tool, "tool_input": data}, self.root)[0]

    def test_records_protected_and_unreceipted_edits_detected(self):
        paths = ["state/reviews/R0001.json", "claims/C001/reviews.json", "experiments/E001/plan_gate.json",
                 "experiments/E001/jobs.jsonl", "experiments/E001/jobs_usage.jsonl"]
        for path in paths:
            self.assertFalse(self.allowed("Write", file_path=path), path)
            self.assertFalse(self.allowed("Edit", file_path=path), path)
        guard.snapshot(self.cfg)
        write(self.root / paths[0], '{"verdict":"yes"}')
        with self.assertRaisesRegex(RuntimeError, "untrusted"):
            guard.require_trusted_records(self.cfg)
        self.assertTrue(guard.check(self.cfg, None)["violations"])

    def test_receipt_does_not_authorize_later_edit(self):
        guard.snapshot(self.cfg)
        before = guard.protected(self.cfg)
        p = write(self.cfg.layout.reviews / "R0001.json", '{"verdict":"partial"}')
        guard.authorize_changes(self.cfg, before)
        guard.require_trusted_records(self.cfg)
        p.write_text('{"verdict":"yes"}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            guard.require_trusted_records(self.cfg)

    def test_forged_review_rejected_by_actual_gate(self):
        self.assertEqual(self.case.claim("C100", review={"verdict": "partial", "grade": "B"})["decision"], "needs_work")
        guard.snapshot(self.cfg)
        rid = read_json(self.cfg.layout.claims / "C100/reviews.json")[-1]
        p = self.cfg.layout.reviews / f"{rid}.json"
        rec = read_json(p)
        rec["verdict"] = "yes"
        write_json(p, rec)
        with self.assertRaisesRegex(RuntimeError, "untrusted"):
            gates.claim_gate(self.cfg, "C100")

    def test_chained_commands_and_runner_controls_rejected(self):
        for cmd in ('autolab --help; echo bad > autolab.toml', 'autolab --help && sbatch job.sh',
                    'autolab tick\npython worker.py', 'autolab tick $(echo bad)', 'autolab guard snapshot',
                    'autolab --project /other tick', 'autolab budget record fabricated.jsonl'):
            self.assertFalse(self.allowed("Bash", command=cmd), cmd)
        self.assertTrue(self.allowed("Bash", command='autolab decide "A; B" --why "Compare A and B"'))

    def test_guard_detects_indirect_script_mutation(self):
        guard.snapshot(self.cfg)
        p = self.root / "autolab.toml"
        original = p.read_text(encoding="utf-8")
        p.write_text(original + "\n# worker changed this\n", encoding="utf-8")
        log = write(self.root / "turn.jsonl", json.dumps({"message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "python worker.py"}}]}}))
        result = guard.check(self.cfg, log)
        self.assertIn("autolab.toml", result["restored"])
        self.assertEqual(p.read_text(encoding="utf-8"), original)

    def test_critical_and_unverified_reviews_cannot_accept(self):
        for i, extra in enumerate(({"issues": [{"severity": "critical", "text": "Invalid evidence"}]},
                                   {"unverified": ["novelty"]})):
            result = self.case.claim(f"C10{i}", review={"verdict": "yes", "grade": "B", **extra})
            self.assertEqual(result["decision"], "needs_work")
        self.assertEqual(list(self.cfg.layout.findings.glob("F*")), [])

    def test_invalid_and_stale_outcomes_block_claims(self):
        self.case.claim("C100")
        research.record_outcome(self.cfg, "E001", "invalid", "n/a", "refine", "Solver failure")
        self.assertEqual(gates.claim_gate(self.cfg, "C100")["decision"], "blocked")
        research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "Solver repaired")
        write(self.cfg.layout.experiments / "E001/results/metrics.json", '{"slope":999}')
        self.assertEqual(gates.claim_gate(self.cfg, "C100")["decision"], "blocked")

    def test_shared_code_invalidates_review_and_outcome(self):
        self.case.claim("C100")
        previous = gates.fingerprint(self.cfg, "C100")
        write(self.root / "src/solver.py", "VERSION = 2\n")
        self.assertNotEqual(previous, gates.fingerprint(self.cfg, "C100"))
        self.assertEqual(gates.claim_gate(self.cfg, "C100")["decision"], "blocked")

    def test_valid_outcome_requires_finished_latest_run_but_allows_repair(self):
        exp = self.cfg.layout.experiments / "E001"
        append_jsonl(exp / "jobs.jsonl", {"job": "1", "submitted": "now"})
        with self.assertRaisesRegex(ValueError, "completed"):
            research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "too early")
        append_jsonl(exp / "jobs_usage.jsonl", {"job": "1", "state": "FAILED"})
        append_jsonl(exp / "jobs.jsonl", {"job": "2", "submitted": "later"})
        append_jsonl(exp / "jobs_usage.jsonl", {"job": "2", "state": "COMPLETED"})
        rec = research.record_outcome(self.cfg, "E001", "valid", "supports", "continue", "repaired")
        self.assertEqual(rec["run_jobs"], ["2"])

    def test_amendment_only_covers_exact_evaluator_version(self):
        exp = self.cfg.layout.experiments / "E900"
        write(exp / "plan.md", '+++\nanalysis_command="python analysis.py"\n+++\n')
        script = write(exp / "analysis.py", "print(1)\n")
        old = research.freeze_evaluator(self.cfg, "E900")["files"]["analysis.py"]
        script.write_text("print(2)\n")
        write(exp / "plan_amendments.md", f"2026-10-17 analysis.py {old} {sha256_file(script)} reason: fix units\n")
        self.assertEqual(research.evaluator_problems(self.cfg, "E900")[0], [])
        script.write_text("print(999)\n")
        self.assertTrue(research.evaluator_problems(self.cfg, "E900")[0])

    def test_live_lock_never_stolen_due_to_age(self):
        lock = FileLock(self.root / "active")
        with lock:
            os.utime(lock.dir, (time.time() - 1000, time.time() - 1000))
            with self.assertRaises(TimeoutError):
                with FileLock(self.root / "active", timeout_s=.01, stale_s=.001):
                    self.fail("stole live lock")

    def test_old_owner_cannot_remove_replacement_lock(self):
        lock = FileLock(self.root / "active")
        lock.__enter__()
        write_json(lock.dir / "owner.json", {"token": "another-owner"})
        lock.__exit__()
        self.assertTrue(lock.dir.exists())

    def test_review_cost_reserved_before_timeout_and_failure(self):
        self.cfg.data["reviewer"]["backend"] = "codex"
        f = write(self.root / "input.md", "input")
        with patch.object(reviewer.shutil, "which", return_value="mock"), patch.object(
                reviewer.subprocess, "run", side_effect=subprocess.TimeoutExpired("mock", 1)):
            with self.assertRaises(RuntimeError):
                reviewer.run(self.cfg, "claim_support", [f])
        self.assertEqual(budget.status(self.cfg)["unknown_cost_turns"], 1)
        self.assertGreater(budget.status(self.cfg)["spent_usd"], 0)
        self.cfg.data["budget"]["total_usd"] = 1
        with patch.object(reviewer.shutil, "which", return_value="mock"), patch.object(reviewer.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "budget"):
                reviewer.run(self.cfg, "claim_support", [f])
            run.assert_not_called()

    def test_review_reservations_are_atomic(self):
        self.cfg.data["budget"]["total_usd"] = 1
        def reserve(i):
            try:
                budget.reserve_review(self.cfg, str(i), "mock")
                return True
            except RuntimeError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sum(pool.map(reserve, range(2))), 1)

    def test_successful_api_call_is_not_assumed_free(self):
        self.cfg.data["reviewer"]["backend"] = "codex"
        f = write(self.root / "input.md", "input")
        response = SimpleNamespace(returncode=0, stdout='```json\n{"verdict":"yes","grade":"B"}\n```', stderr='tokens used: 1000')
        with patch.object(reviewer.shutil, "which", return_value="mock"), patch.object(reviewer.subprocess, "run", return_value=response):
            reviewer.run(self.cfg, "claim_support", [f])
        self.assertEqual(budget.status(self.cfg)["unknown_cost_turns"], 1)
        self.assertEqual(len(read_jsonl(self.cfg.layout.state / "budget.jsonl")), 1)

    def test_uncertain_submission_keeps_reservation_and_blocks_retry(self):
        self.cfg.data["slurm"]["require_clean_git"] = False
        r = dict(partition="cpu", qos="normal", nodes=1, ntasks=1, cpus=4, total_cpus=4,
                 billed_cpus=4, mem_gb=1, hours=1.5, gpus=0, array="", array_size=1)
        with patch.object(slurm, "status", return_value=[]), patch.object(slurm, "render", return_value=(self.root / "job.sh", r)), \
             patch.object(slurm, "_exe", return_value="mock"), patch.object(slurm.subprocess, "run", side_effect=subprocess.TimeoutExpired("mock", 60)):
            with self.assertRaises(subprocess.TimeoutExpired):
                slurm.submit(self.cfg, "E001")
            self.assertEqual(slurm.committed_core_hours(self.cfg)["reserved"], 6)
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                slurm.submit(self.cfg, "E001")

    def test_concurrent_submit_only_submits_one_job(self):
        self.cfg.data["slurm"]["require_clean_git"] = False
        r = dict(partition="cpu", qos="normal", nodes=1, ntasks=1, cpus=1, total_cpus=1,
                 billed_cpus=1, mem_gb=1, hours=1, gpus=0, array="", array_size=1)
        jobs = self.cfg.layout.experiments / "E001/jobs.jsonl"
        def status(*args):
            return [{"job": j["job"], "state": "PENDING", "part": None} for j in read_jsonl(jobs)]
        def submit(i):
            try:
                return slurm.submit(self.cfg, "E001")
            except RuntimeError:
                return None
        with patch.object(slurm, "status", side_effect=status), patch.object(slurm, "render", return_value=(self.root / "job.sh", r)), \
             patch.object(slurm, "_exe", return_value="mock"), patch.object(slurm.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="123", stderr="")) as run:
            with ThreadPoolExecutor(2) as pool:
                result = list(pool.map(submit, range(2)))
            self.assertEqual(sum(x is not None for x in result), 1)
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
