"""Offline integration tests: no Weixin credentials or paid API calls."""
import shutil
import unittest
from unittest.mock import patch

from test_autolab import make_project, write
from autolab import guard, hooks, human, project, weixin
from autolab.util import read_json, write_json
REAL_RENDER = weixin.render_event


class FakeClient:
    account = {"user_id": "owner"}

    def __init__(self):
        self.messages = []
        self.sent = []
        self.fail_send = False

    def call(self, endpoint, body):
        messages, self.messages = self.messages, []
        return {"msgs": messages, "get_updates_buf": "cursor"}

    def send(self, text, context, key):
        if self.fail_send:
            raise RuntimeError("offline")
        self.sent.append((text, context, key))


class Weixin(unittest.TestCase):
    def setUp(self):
        self.root, self.cfg = make_project()
        self.d = self.root.parent / "private-weixin"
        self.d.mkdir()
        self.addCleanup(shutil.rmtree, self.root.parent)
        p = patch.object(weixin, "directory", return_value=self.d)
        p.start()
        self.addCleanup(p.stop)
        self.client = FakeClient()
        # Transport/approval tests exercise arbitrary long payloads independently
        # of the concise Chinese display layer; dedicated tests below use it.
        p = patch.object(weixin, "render_event", side_effect=lambda cfg, e: e["text"] + " " + e.get("token", ""))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(weixin, "qa_settings", return_value={"enabled": False, "model": "sonnet", "usd_per_question": 0.5,
                                                               "usd_per_day": 5.0, "timeout_s": 30, "command": "claude"})
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("SEND_INTERVAL", 0), ("PROGRESS_INTERVAL", 0), ("MESSAGE_LIMIT", 1000)):
            p = patch.object(weixin, name, value)
            p.start()
            self.addCleanup(p.stop)

    def message(self, text, mid="1", sender="owner"):
        return {"message_id": mid, "from_user_id": sender, "message_type": 1,
                "context_token": "context", "item_list": [{"type": 1, "text_item": {"text": text}}]}

    def request(self):
        aid = human.request_action(self.cfg, "decision", "Proceed with experiment?", True)
        self.client.messages = [self.message("状态")]
        weixin.cycle(self.cfg, self.client)
        state = read_json(self.d / "state.json")
        event = next(e for e in state["events"].values() if e.get("aid") == aid)
        weixin.cycle(self.cfg, self.client)  # drain the status acknowledgement
        return aid, event

    def reply(self, aid, event, mid="2"):
        msg = self.message(f"回复 {aid} {event['token']} 不同意，先补对照实验", mid)
        self.client.messages = [msg]
        weixin.cycle(self.cfg, self.client)
        return msg

    def test_roundtrip_and_duplicate_delivery(self):
        aid, event = self.request()
        msg = self.reply(aid, event)
        self.client.messages = [msg]
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(read_json(self.d / "state.json")["replies"]), 1)
        self.assertEqual(weixin.apply_replies(self.cfg), 1)
        self.assertEqual(weixin.apply_replies(self.cfg), 0)
        inbox = list(self.cfg.layout.inbox.glob("weixin-*.md"))
        self.assertEqual(len(inbox), 1)
        self.assertIn("不同意", inbox[0].read_text(encoding="utf-8"))
        self.assertEqual(human.resolve_from_inbox(self.cfg)[0][0], aid)
        self.assertFalse(human.open_actions(self.cfg))

    def test_untrusted_sender_cannot_reply_or_replace_context(self):
        aid, event = self.request()
        self.client.messages = [self.message(f"回复 {aid} {event['token']} yes", sender="intruder")]
        self.client.messages[0]["context_token"] = "evil"
        weixin.cycle(self.cfg, self.client)
        state = read_json(self.d / "state.json")
        self.assertFalse(state.get("replies"))
        self.assertEqual(state["context"], "context")

    def test_plan_change_does_not_void_a_reply(self):
        aid, event = self.request()
        write(self.cfg.layout.experiments / "E001/plan.md", "new plan")
        self.reply(aid, event)
        self.assertEqual(len(read_json(self.d / "state.json")["replies"]), 1)
        write(self.cfg.layout.experiments / "E002/plan.md", "another plan")
        self.assertEqual(weixin.apply_replies(self.cfg), 1)

    def test_reply_without_code_anywhere_in_text_and_corrections(self):
        aid, _ = self.request()
        self.client.messages = [self.message(f"同意{aid}，先做试跑", "2")]
        weixin.cycle(self.cfg, self.client)
        replies = list(read_json(self.d / "state.json")["replies"].values())
        self.assertEqual((replies[0]["aid"], replies[0]["body"]), (aid, "同意，先做试跑"))
        self.assertTrue(any(f"已收到：{aid}" in t for t, _, _ in self.client.sent))
        self.client.messages = [self.message(f"更正 {aid} 不做试跑，直接写报告", "3")]
        weixin.cycle(self.cfg, self.client)
        replies = list(read_json(self.d / "state.json")["replies"].values())
        self.assertTrue(replies[1]["correction"])
        self.assertEqual(weixin.apply_replies(self.cfg), 2)
        texts = [p.read_text(encoding="utf-8") for p in self.cfg.layout.inbox.glob("weixin-*.md")]
        self.assertTrue(any("latest answer wins" in t and "直接写报告" in t for t in texts))
        self.client.messages = [self.message(aid, "4")]                      # an A-number alone
        weixin.cycle(self.cfg, self.client)
        self.assertTrue(any(f"请在 {aid} 后面写上你的决定" in t for t, _, _ in self.client.sent))

    def test_free_text_is_a_note_and_closed_request_reply_is_delivered(self):
        aid, event = self.request()
        self.client.messages = [self.message("这个方向先放一放，优先真实算例", "2")]
        weixin.cycle(self.cfg, self.client)
        self.assertTrue(any("已作为留言转给 PI" in t for t, _, _ in self.client.sent))
        self.assertEqual(weixin.apply_replies(self.cfg), 1)
        human.resolve_actions(self.cfg, aid, "answered elsewhere")
        self.client.messages = [self.message(f"{aid} 同意", "3")]
        weixin.cycle(self.cfg, self.client)
        self.assertTrue(any(f"{aid} 已关闭" in t for t, _, _ in self.client.sent))
        self.assertEqual(weixin.apply_replies(self.cfg), 1)
        self.assertTrue(any(f"{aid} 同意" in p.read_text(encoding="utf-8")
                            for p in self.cfg.layout.inbox.glob("weixin-NOTE-*.md")))

    def test_question_answered_by_read_only_run(self):
        import os, stat, time as _t
        fake = self.root.parent / "fake-claude"
        fake.write_text("#!/bin/sh\necho '{\"result\": \"E013 已停止：盒子方法比基线慢。\", \"total_cost_usd\": 0.12}'\n",
                        encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        settings = {"enabled": True, "model": "sonnet", "usd_per_question": 0.5, "usd_per_day": 5.0,
                    "timeout_s": 30, "command": str(fake)}
        with patch.object(weixin, "qa_settings", return_value=settings):
            self.client.messages = [self.message("为什么 E013 停了？", "2")]
            weixin.cycle(self.cfg, self.client)
            self.assertTrue(any("正在查项目记录" in t for t, _, _ in self.client.sent))
            for _ in range(50):
                if list((self.d / "qa").glob("*.json")) and not list((self.d / "qa").glob("*.part")):
                    break
                _t.sleep(0.2)
            weixin.cycle(self.cfg, self.client)
            weixin.cycle(self.cfg, self.client)
        self.assertTrue(any("E013 已停止" in t for t, _, _ in self.client.sent))
        from autolab.util import read_jsonl
        spent = [r for r in read_jsonl(self.cfg.layout.state / "budget.jsonl") if r["source"] == "weixin_qa"]
        self.assertEqual(spent[0]["usd"], 0.12)
        self.assertEqual(weixin.apply_replies(self.cfg), 1)                  # the question also reaches the PI
        args = (self.d / "qa" / "settings.json").read_text(encoding="utf-8")
        self.assertIn(".ssh", args)

    def test_listener_lock_released_on_sighup(self):
        import signal, subprocess, sys, time as _t
        from pathlib import Path
        src = Path(weixin.__file__).resolve().parents[1]
        lock = self.root.parent / "listener"
        script = (f"import sys, time; sys.path.insert(0, {str(src)!r})\n"
                  "from pathlib import Path\nfrom autolab import weixin\nfrom autolab.util import FileLock\n"
                  "weixin.stop_on_signals()\nlock = FileLock(Path(sys.argv[1])); lock.__enter__()\n"
                  "try:\n    print('ready', flush=True); time.sleep(60)\nfinally:\n    lock.__exit__(None, None, None)\n")
        for sig in (signal.SIGHUP, signal.SIGTERM):
            p = subprocess.Popen([sys.executable, "-c", script, str(lock)], stdout=subprocess.PIPE, text=True)
            self.assertEqual(p.stdout.readline().strip(), "ready")
            self.assertTrue(Path(str(lock) + ".lock").is_dir())
            p.send_signal(sig)
            p.wait(timeout=10)
            p.stdout.close()
            self.assertFalse(Path(str(lock) + ".lock").exists(), f"lock left behind after {sig}")

    def test_question_budget_cap(self):
        from autolab import budget
        budget.record_cost(self.cfg, "weixin_qa", 4.8)
        settings = {"enabled": True, "model": "sonnet", "usd_per_question": 0.5, "usd_per_day": 5.0,
                    "timeout_s": 30, "command": "sh"}
        with patch.object(weixin, "qa_settings", return_value=settings):
            self.client.messages = [self.message("现在最大的风险是什么？", "2")]
            weixin.cycle(self.cfg, self.client)
            weixin.cycle(self.cfg, self.client)
        self.assertTrue(any("额度已用完" in t for t, _, _ in self.client.sent))

    def test_failed_send_retries_same_id_without_losing_request(self):
        human.request_action(self.cfg, "approval", "approve", True)
        self.client.messages = [self.message("hello")]
        self.client.fail_send = True
        with patch.object(weixin.time, "time", return_value=1000):
            weixin.cycle(self.cfg, self.client)
        self.assertFalse(next(iter(read_json(self.d / "state.json")["events"].values()))["sent"])
        self.client.fail_send = False
        weixin.cycle(self.cfg, self.client)
        weixin.cycle(self.cfg, self.client)
        count = len(self.client.sent)
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(self.client.sent), count)

    def test_new_progress_not_historical_or_invalid_outcomes(self):
        write_json(self.cfg.layout.findings / "F001/finding.json", {"statement": "old"})
        self.client.messages = [self.message("hello")]
        weixin.cycle(self.cfg, self.client)
        self.client.sent.clear()
        write_json(self.cfg.layout.findings / "F002/finding.json", {"statement": "new result"})
        write_json(self.cfg.layout.experiments / "E001/outcome.json", {
            "engineering": "invalid", "hypothesis": "n/a"})
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("new result", self.client.sent[0][0])
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_reply_does_not_execute_commands_or_change_budget(self):
        aid, event = self.request()
        original = (self.root / "autolab.toml").read_bytes()
        self.client.messages = [self.message(f"回复 {aid} {event['token']} $(touch hacked); raise budget", "2")]
        weixin.cycle(self.cfg, self.client)
        weixin.apply_replies(self.cfg)
        self.assertEqual(original, (self.root / "autolab.toml").read_bytes())
        self.assertFalse((self.root / "hacked").exists())

    def test_agent_cannot_invoke_bridge(self):
        for command in ("autolab weixin apply", "autolab weixin login", "autolab weixin listen"):
            self.assertFalse(hooks.decide({"tool_name": "Bash", "tool_input": {"command": command}}, self.root)[0])

    def test_credential_directory_excluded_from_agent_sandbox(self):
        self.cfg.data["agents"]["sandbox"] = True
        project._write_settings(self.cfg)
        settings = read_json(self.root / ".claude/settings.json")
        self.assertTrue(any(p.endswith("/autolab/weixin") for p in settings["sandbox"]["filesystem"]["denyRead"]))

    def test_reply_import_before_snapshot_passes_guard(self):
        aid, event = self.request()
        self.reply(aid, event)
        with patch.object(guard, "_dir", return_value=self.root.parent / "guard"):
            weixin.apply_replies(self.cfg)
            guard.snapshot(self.cfg)
            self.assertFalse(guard.check(self.cfg, None)["violations"])

    def test_reject_credential_exfiltration_endpoints(self):
        for url in ("http://ilinkai.weixin.qq.com", "https://evil.com", "https://ilinkai.weixin.qq.com.evil.com",
                    "https://user:pass@ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com/path"):
            with self.assertRaises(ValueError):
                weixin.safe_base(url)

    def test_followup_note_and_pi_response(self):
        self.request()
        self.client.messages = [self.message("留言 请解释 F002 的证据", "2")]
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(weixin.apply_replies(self.cfg), 1)
        self.assertEqual(len(human.open_actions(self.cfg)), 1)  # note doesn't approve requests
        human.notify(self.cfg, "F002 的证据在 experiments/E002/results.csv")
        weixin.cycle(self.cfg, self.client)
        weixin.cycle(self.cfg, self.client)
        self.assertTrue(any("results.csv" in text for text, _, _ in self.client.sent))

    def test_explicit_grade_creates_fingerprint_bound_verdict(self):
        write(self.cfg.layout.claims / "C001/claim.md", '+++\nstatement = "claim"\n+++\n')
        aid = human.request_action(self.cfg, "decision", "Claim C001: see 00_human/verdicts/C001.toml", True)
        self.client.messages = [self.message("状态")]
        weixin.cycle(self.cfg, self.client)
        event = next(iter(read_json(self.d / "state.json")["events"].values()))
        self.client.messages = [self.message(f"回复 {aid} {event['token']} 评级 B", "2")]
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(weixin.apply_replies(self.cfg), 1)
        from autolab import gates
        fp = gates.fingerprint(self.cfg, "C001")["fingerprint"]
        self.assertEqual(gates.human_verdict(self.cfg, "C001", fp)[0], "B")

    def test_claim_change_rejects_grade(self):
        claim = write(self.cfg.layout.claims / "C001/claim.md", '+++\nstatement = "claim"\n+++\n')
        aid = human.request_action(self.cfg, "decision", "Claim C001: see 00_human/verdicts/C001.toml", True)
        self.client.messages = [self.message("状态")]
        weixin.cycle(self.cfg, self.client)
        event = next(iter(read_json(self.d / "state.json")["events"].values()))
        write(claim, '+++\nstatement = "different claim"\n+++\n')
        self.client.messages = [self.message(f"回复 {aid} {event['token']} 评级 A", "2")]
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(weixin.apply_replies(self.cfg), 1)  # delivered as a note
        self.assertFalse((self.cfg.layout.human / "verdicts/C001.toml").exists())

    def test_long_decision_not_silently_truncated(self):
        human.request_action(self.cfg, "decision", "x" * 2500 + "important final condition", True)
        self.client.messages = [self.message("状态")]
        weixin.cycle(self.cfg, self.client)
        for _ in range(4):
            weixin.cycle(self.cfg, self.client)
        self.assertIn("important final condition", "".join(t for t, _, _ in self.client.sent))

    def test_client_checks_business_errors(self):
        import io
        client = weixin.Client({"token": "secret", "user_id": "owner"})
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"ret": 0, "errcode": -14}')
            with self.assertRaisesRegex(RuntimeError, "-14"):
                client.call("getupdates", {})

    def test_send_accepts_production_message_id_ack(self):
        client = weixin.Client({"token": "secret", "user_id": "owner"})
        with patch.object(client, "call", return_value={"message_id": "123"}):
            client.send("hello", "context", "stable-id")
        with patch.object(client, "call", return_value={}):
            client.send("hello", "context", "stable-id")
        with patch.object(client, "call", return_value={"message_id": ""}):
            client.send("hello", "context", "stable-id")

    def test_context_rejection_retries_without_new_inbound(self):
        human.request_action(self.cfg, "decision", "approve?", True)
        self.client.messages = [self.message("状态")]
        with patch.object(weixin.time, "time", return_value=1000):
            with patch.object(self.client, "send", side_effect=weixin.ILinkError(-2, True)) as send:
                weixin.cycle(self.cfg, self.client)
                first = send.call_count
                weixin.cycle(self.cfg, self.client)
                self.assertEqual(send.call_count, first)
        with patch.object(weixin.time, "time", return_value=1061):
            weixin.cycle(self.cfg, self.client)
        self.assertTrue(all(e["sent"] for e in read_json(self.d / "state.json")["events"].values()))

    def test_progress_is_combined(self):
        self.request()
        human.notify(self.cfg, "bad item")
        human.notify(self.cfg, "important progress")
        self.client.sent.clear()
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("bad item", self.client.sent[0][0])
        self.assertIn("important progress", self.client.sent[0][0])

    def test_partial_card_resumes_after_restart_without_replaying_chunks(self):
        self.request()
        state = read_json(self.d / "state.json")
        state.pop("next_send_at", None)
        write_json(self.d / "state.json", state)
        human.request_action(self.cfg, "decision", "x" * 2500, True)
        send = self.client.send
        def fail_second(text, context, key):
            if text.startswith("(2/"):
                raise weixin.ILinkError(-2, True)
            return send(text, context, key)
        with patch.object(weixin.time, "time", return_value=1000):
            with patch.object(self.client, "send", side_effect=fail_second):
                weixin.cycle(self.cfg, self.client)
        self.client.sent.clear()
        with patch.object(weixin.time, "time", return_value=1061):
            weixin.cycle(self.cfg, self.client)
        self.assertFalse(any(t.startswith("(1/") for t, _, _ in self.client.sent))
        self.assertTrue(any(t.startswith("(2/") for t, _, _ in self.client.sent))

    def test_migrate_permanent_block_and_notify_new_action_without_prompt(self):
        self.request()
        state = read_json(self.d / "state.json")
        state["blocked_context"] = "context"
        write_json(self.d / "state.json", state)
        aid = human.request_action(self.cfg, "decision", "new automatic request", True)
        self.client.sent.clear()
        weixin.cycle(self.cfg, self.client)
        self.assertTrue(any(aid in t for t, _, _ in self.client.sent))

    def test_decisions_precede_and_combine_before_progress(self):
        self.request()
        human.notify(self.cfg, "milestone")
        one = human.request_action(self.cfg, "decision", "first decision", True)
        two = human.request_action(self.cfg, "decision", "second decision", True)
        self.client.sent.clear()
        weixin.cycle(self.cfg, self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn(one, self.client.sent[0][0])
        self.assertIn(two, self.client.sent[0][0])
        self.assertNotIn("milestone", self.client.sent[0][0])
        weixin.cycle(self.cfg, self.client)
        self.assertIn("milestone", self.client.sent[-1][0])

    def test_progress_window_does_not_delay_decision(self):
        with patch.object(weixin.time, "time", return_value=1000):
            self.request()
            with patch.object(weixin, "SEND_INTERVAL", 60), patch.object(weixin, "PROGRESS_INTERVAL", 1800):
                human.notify(self.cfg, "milestone one")
                weixin.cycle(self.cfg, self.client)
                count = len(self.client.sent)
                human.notify(self.cfg, "milestone two")
                weixin.cycle(self.cfg, self.client)
                self.assertEqual(len(self.client.sent), count)
        with patch.object(weixin.time, "time", return_value=1061):
            aid = human.request_action(self.cfg, "decision", "urgent", True)
            weixin.cycle(self.cfg, self.client)
            self.assertIn(aid, self.client.sent[-1][0])
            weixin.cycle(self.cfg, self.client)
            self.assertFalse(any("milestone two" in t for t, _, _ in self.client.sent))
        with patch.object(weixin.time, "time", return_value=2801):
            weixin.cycle(self.cfg, self.client)
            self.assertIn("milestone two", self.client.sent[-1][0])

    def test_chinese_copy_bound_to_action_and_preserves_reply(self):
        aid = human.request_action(self.cfg, "decision", "Approve pilot or stop? Cost 4-11 core-h", True,
                                   rule="pilot", recommend="run the pilot")
        weixin.summarize(self.cfg, aid, "请决定先做试跑还是停止。", "试跑预计需要 4–11 核时，费用尚未实测。")
        with patch.object(weixin, "render_event", REAL_RENDER):
            self.client.messages = [self.message("状态")]
            weixin.cycle(self.cfg, self.client)
        text = self.client.sent[0][0]
        self.assertIn("请决定先做试跑还是停止", text)
        self.assertIn("背景：", text)
        self.assertNotIn("Approve pilot", text)
        event = next(e for e in read_json(self.d / "state.json")["events"].values() if e.get("aid") == aid)
        self.assertIn(f"直接发“{aid} 你的决定”", text)
        self.assertIn("前不回复，PI 将按推荐方案执行", text)
        write(self.cfg.layout.experiments / "E001/plan.md", "changed")            # copy stays bound to the request
        current = next(e for e in weixin.events(self.cfg).values() if e.get("aid") == aid)
        self.assertIn("copy", current)

    def test_chinese_progress_and_unknown_english_fallback(self):
        human.notify(self.cfg, "进展：完成小规模验证。背景：误差为零，但尚未测试真实数据。")
        event = next(iter(weixin.events(self.cfg).values()))
        text = REAL_RENDER(self.cfg, event)
        self.assertIn("完成小规模验证", text)
        self.assertIn("尚未测试真实数据", text)
        self.assertIn("无需回复", text)
        event["text"] = "An unreviewed result with an unsupported interpretation " * 100
        text = REAL_RENDER(self.cfg, event)
        self.assertNotIn("unsupported", text)
        self.assertIn("中文说明尚待", text)

    def test_copy_validation(self):
        aid = human.request_action(self.cfg, "decision", "test", True)
        for summary in ("English only", "中" * 101, "第一行\n第二行"):
            with self.assertRaises(ValueError):
                weixin.summarize(self.cfg, aid, summary, "必要背景。")

    def test_natural_progress_query_reads_latest_records_without_approval(self):
        human.notify(self.cfg, "进展：完成第一轮验证。背景：仍需扩大样本。")
        write_json(self.cfg.layout.experiments / "E009" / "outcome.json", {
            "at": "2026-10-18T09:00:00-04:00", "engineering": "valid",
            "hypothesis": "inconclusive", "value": "refine", "reason": "误差区间仍偏宽"})
        aid = human.request_action(self.cfg, "decision", "需要批准下一轮", True)
        self.client.messages = [self.message("请报告一下最新进展")]
        weixin.cycle(self.cfg, self.client)
        answer = self.client.sent[0][0]
        self.assertIn("完成第一轮验证", answer)
        self.assertIn("E009", answer)
        self.assertIn("结论不明确", answer)
        self.assertIn("2026-10-18", answer)
        self.assertNotIn("用法：", answer)
        state = read_json(self.d / "state.json")
        self.assertFalse(state.get("replies"))
        self.assertIn(aid, weixin.actions(self.cfg))
        human.notify(self.cfg, "进展：完成第二轮验证。背景：结果仍待审查。")
        self.client.messages = [self.message("现在做到哪了", "next")]
        weixin.cycle(self.cfg, self.client)
        self.assertIn("完成第二轮验证", self.client.sent[-1][0])

    def test_progress_intent_keeps_explicit_commands_and_empty_state_honest(self):
        for query in ("进度如何？", "最新结果", "progress", "状态", "请给我报告进展"):
            self.assertTrue(weixin.is_progress_query(query))
        for query in ("回复 A001 token 继续报告进展", "留言 请加快进展"):
            self.assertFalse(weixin.is_progress_query(query))
        answer = weixin.progress_report(self.cfg)
        self.assertIn("尚无已记录", answer)
        self.assertNotIn("已完成", answer)
