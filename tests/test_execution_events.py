import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from autolab import events, execution

class ExecutionEventsTests(unittest.TestCase):
    def test_backend_rejects_unknown_and_preserves_controls(self):
        cfg = SimpleNamespace(data={"execution": {"backend": "dsh"}})
        with self.assertRaises(ValueError): execution.backend(cfg)
        command = execution.ClaudeCode().command(execution.TurnRequest("literal $hello", "opus", 80, 2.5))
        self.assertEqual(command[:3], ["claude", "-p", "literal $hello"])
        self.assertIn("--disallowedTools", command)
        self.assertEqual(command[-2:], ["--max-budget-usd", "2.5"])
        for cap in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                execution.ClaudeCode().command(execution.TurnRequest("x", "opus", 80, cap))

    def test_sync_recovers_and_deduplicates_without_losing_return_transition(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = SimpleNamespace(layout=SimpleNamespace(state=Path(d)))
            records = [("human.action", "requests", "A001", {"resolved": False})]
            with patch.object(events, "collect", side_effect=lambda cfg: iter(records)):
                self.assertEqual(len(events.sync(cfg)), 1)
                self.assertEqual(events.sync(cfg), [])
                records[0] = ("human.action", "requests", "A001", {"resolved": True})
                self.assertEqual(len(events.sync(cfg)), 1)
                records[0] = ("human.action", "requests", "A001", {"resolved": False})
                self.assertEqual(len(events.sync(cfg)), 1)
                rows = events.read_jsonl(Path(d) / "events.jsonl")
                self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
                self.assertEqual(len({r["id"] for r in rows}), 3)
                # A malformed existing journal is never silently replaced.
                (Path(d) / "events.jsonl").write_text("broken", encoding="utf-8")
                with self.assertRaises(json.JSONDecodeError): events.sync(cfg)

    def test_weixin_credentials_are_not_indexed(self):
        from autolab.paths import Layout
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            private = root / "private"
            private.mkdir()
            (private / "state.json").write_text(json.dumps({"context": "SECRET", "replies": {
                "r1": {"aid": "A001", "status": "queued", "body": "PRIVATE", "sender": "SECRET"}}}))
            cfg = SimpleNamespace(layout=Layout(root))
            with patch("autolab.weixin.directory", return_value=private):
                rows = events.sync(cfg)
            text = json.dumps(rows)
            self.assertNotIn("SECRET", text)
            self.assertNotIn("PRIVATE", text)
            self.assertIn("A001", text)

    @unittest.skipUnless(__import__("os").name == "posix", "exec handoff requires POSIX runner")
    def test_real_exec_handoff_and_finish(self):
        import os
        import subprocess
        import sys
        from test_autolab import make_project
        root, cfg = make_project()
        binary = root / "fake-bin"
        binary.mkdir()
        fake = binary / "claude"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' '{\"type\":\"result\",\"total_cost_usd\":0.25}'\nexit 7\n")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        log = root / "state/pi_logs/test.jsonl"
        log.parent.mkdir(exist_ok=True)
        proc = subprocess.run([sys.executable, "-m", "autolab.execution", "launch",
                               "--prompt", "test", "--model", "opus", "--budget", "1",
                               "--log", str(log)], cwd=root, env=env, capture_output=True)
        self.assertEqual(proc.returncode, 7, proc.stderr)
        self.assertIn(b"total_cost_usd", proc.stdout)
        subprocess.run([sys.executable, "-m", "autolab.execution", "finish", "--log", str(log),
                        "--rc", "7", "--seconds", "1"], cwd=root, env=env, check=True)
        record = json.loads(log.with_suffix(".execution.json").read_text())
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["exit_code"], 7)
        rows = events.read_jsonl(root / "state/events.jsonl")
        self.assertEqual([r["data"]["state"] for r in rows if r["kind"] == "turn.execution"],
                         ["running", "finished"])
