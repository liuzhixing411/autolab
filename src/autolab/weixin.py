"""Human-operated Weixin bridge. Chat text is never executed; the only model call is the optional
read-only question answering (`qa_step`), capped per question and per day.

The listener keeps credentials/replies outside the project. The runner imports replies
between turns, before its integrity snapshot. See docs/WEIXIN.md for deployment limits.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from autolab import human
from autolab.util import FileLock, now_iso, read_json, read_jsonl, sha256_file, write_atomic, write_json

BASE = "https://ilinkai.weixin.qq.com"
VERSION = "2.4.8"  # protocol baseline, not the Autolab package version
MESSAGE_LIMIT = 2800
SEND_INTERVAL = 60
PROGRESS_INTERVAL = 1800
# an A-number anywhere in a message ("同意A030", "A029 选a"); Chinese letters count as \w, so no \b
AID = re.compile(r"(?<![A-Za-z0-9])[Aa](\d{3,})(?!\d)")
QUESTION = ("?", "？", "为什么", "为何", "怎么", "如何", "是否", "能不能", "能否", "有没有", "什么", "哪", "多少",
            "吗", "呢", "原因", "解释", "说明一下", "why", "how", "what")


class ILinkError(RuntimeError):
    def __init__(self, code, context_rejected=False):
        self.context_rejected = context_rejected
        self.code = code
        super().__init__(f"iLink code={code}" + ("; upstream rejected message preparation"
                         if context_rejected else "; re-login if code is -14"))


def directory(cfg):
    key = hashlib.sha256(str(cfg.layout.root.resolve()).encode()).hexdigest()[:20]
    return Path.home() / ".cache" / "autolab" / "weixin" / key


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_base(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme != "https" or not p.hostname or
            not (p.hostname == "ilinkai.weixin.qq.com" or p.hostname.endswith(".ilinkai.weixin.qq.com"))
            or p.username or p.password or p.port not in (None, 443)
            or p.path not in ("", "/") or p.query or p.fragment):
        raise ValueError("Unrecognized iLink host; review official routing before using this endpoint")
    return url.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never forward bearer tokens via HTTP redirects


class Client:
    def __init__(self, account=None):
        self.account = account or {}
        self.base = safe_base(self.account.get("base_url", BASE))

    def call(self, endpoint, body=None, authenticated=True):
        headers = {"Content-Type": "application/json", "iLink-App-Id": "bot",
                   "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 8)}
        if body is not None:
            headers.update({"AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN":
                            base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        if authenticated:
            headers["Authorization"] = "Bearer " + self.account["token"]
            body = {**(body or {}), "base_info": {"channel_version": VERSION,
                                                      "bot_agent": "Autolab/0.5.0"}}
        req = urllib.request.Request(self.base + "/ilink/bot/" + endpoint,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=40) as r:
                result = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"iLink HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError):
            raise RuntimeError("iLink network unavailable; delivery will be retried") from None
        if not isinstance(result, dict):
            raise RuntimeError("Invalid iLink response")
        for key in ("ret", "errcode"):
            if result.get(key, 0) != 0:
                reason = str(result.get("errmsg", "")).strip().lower()
                raise ILinkError(result[key], result[key] == -2 and reason in ("prepare failed", "unknown error"))
        return result

    def send(self, text, context, key):
        if not context:
            raise RuntimeError("Send a first message to the bot in Weixin to establish context")
        self.call("sendmessage", {"msg": {
            "from_user_id": "", "to_user_id": self.account["user_id"], "client_id": key,
            "message_type": 2, "message_state": 2, "context_token": context,
            "item_list": [{"type": 1, "text_item": {"text": text}}]}})
        # Match the official client: HTTP success with no nonzero business code is
        # accepted. A replay can have an empty/omitted message_id or ret field.


def login(cfg):
    d = directory(cfg)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    d.chmod(0o700)
    with FileLock(d / "listener"):
        client = Client()
        qr = client.call("get_bot_qrcode?bot_type=3", {"local_token_list": []}, False)
        print("Open this login link and scan/confirm with your own Weixin account:\n" + qr["qrcode_img_content"], flush=True)
        deadline, code = time.monotonic() + 480, ""
        while time.monotonic() < deadline:
            query = urllib.parse.urlencode({"qrcode": qr["qrcode"], **({"verify_code": code} if code else {})})
            result = client.call("get_qrcode_status?" + query, None, False)
            status = result.get("status")
            if status == "confirmed":
                account = {"token": result["bot_token"], "user_id": result["ilink_user_id"],
                           "bot_id": result["ilink_bot_id"],
                           "base_url": safe_base(result.get("baseurl") or client.base)}
                if not all(account.values()):
                    raise RuntimeError("Incomplete login; no unrestricted sender fallback allowed")
                write_json(d / "account.json", account)
                (d / "account.json").chmod(0o600)
                # Old context/cursors belong to the previous session.
                with FileLock(d / "state"):
                    state = read_json(d / "state.json", {})
                    state.update(context="", cursor="")
                    write_json(d / "state.json", state)
                print("Bound. Start `autolab weixin listen`, then send 状态 to the bot.")
                return
            if status == "need_verifycode":
                code = input("Enter the verification code shown on your phone: ").strip()
            elif status == "scaned_but_redirect":
                client.base = safe_base("https://" + result["redirect_host"])
            elif status in ("expired", "verify_code_blocked", "binded_redirect"):
                raise RuntimeError("Login could not issue fresh credentials; restart login or use existing binding")
            time.sleep(1)
        raise RuntimeError("Weixin login timed out")


def actions(cfg):
    import re
    result = {}
    for line in human.open_actions(cfg):
        m = re.match(r"- \[ \] \*\*(A\d+)\*\*", line)
        if m:
            result[m[1]] = line
    return result


def action_version(cfg, line):
    # The request itself (and, for a grade, the claim it grades). Binding every plan and config file
    # here voided the human's answers whenever the PI wrote any new plan (first-test, 2026-10-18:
    # six Weixin replies lost); the PI now judges whether an answer still fits a changed situation.
    paths = []
    claim = verdict_claim(line)
    fp = None
    if claim:
        from autolab import gates
        fp = gates.fingerprint(cfg, claim)["fingerprint"]
        paths += [cfg.layout.claims / claim / "claim.md", cfg.layout.claims / claim / "reviews.json"]
    return digest([line, fp, {str(p.relative_to(cfg.layout.root)): sha256_file(p)
                              for p in paths if p.is_file()}])


def verdict_claim(line):
    m = re.search(r"Claim (C\d+):.*00_human/verdicts/\1\.toml", line or "")
    return m[1] if m else None


def events(cfg):
    result = {}
    for rec in read_jsonl(cfg.layout.state / "human_notifications.jsonl"):
        result["update:" + digest(rec)] = {"kind": "PI update", "text": rec["text"], "created": time.time()}
    for aid, line in actions(cfg).items():
        version = action_version(cfg, line)
        key = "action:" + aid + ":" + version
        result[key] = {"aid": aid, "version": version, "text": line,
                       "kind": "action", "created": time.time()}
    for pattern, root, kind in (("F*/finding.json", cfg.layout.findings, "finding"),
                                ("*/outcome.json", cfg.layout.experiments, "outcome")):
        for p in sorted(root.glob(pattern)):
            rec = read_json(p, {})
            if kind == "outcome" and not (rec.get("engineering") == "valid" and
                                           rec.get("hypothesis") in ("supports", "refutes")):
                continue
            if kind == "finding" and rec.get("superseded_by"):
                continue
            key = kind + ":" + str(p.relative_to(cfg.layout.root)) + ":" + digest(rec)
            text = (f"{p.parent.name}: " + str(rec.get("statement", "")) if kind == "finding" else
                    f"{p.parent.name}: {rec.get('hypothesis')}; {rec.get('value')}; {rec.get('reason', '')}")
            result[key] = {"kind": kind, "text": text, "created": time.time()}
    copies = read_json(cfg.layout.state / "notification_copy.json", {})
    for key, event in result.items():
        if key in copies:
            event["copy"] = copies[key]
    return result


def summarize(cfg, target, summary, background):
    """PI-authored display copy, bound to an immutable source event/version."""
    for label, value, limit in (("summary", summary, 100), ("background", background, 360)):
        if not value or len(value) > limit or '\n' in value or not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError(f"{label} must be one Chinese line, at most {limit} characters")
    source = events(cfg)
    keys = [key for key, event in source.items() if key == target or event.get("aid") == target]
    if len(keys) != 1:
        raise ValueError("Choose one current event key or open action ID")
    path = cfg.layout.state / "notification_copy.json"
    with FileLock(path):
        copies = read_json(path, {})
        copies[keys[0]] = {"summary": summary, "background": background}
        write_json(path, copies)


def render_event(cfg, e):
    """Never dump an English research record into a phone notification."""
    action = e["kind"] == "action"
    informative = action and "[other]" in e["text"] and "[optional]" in e["text"]
    copy = e.get("copy")
    if copy:
        summary, background = copy["summary"], copy["background"]
    else:
        body = re.sub(r"^.*?\[(?:download|decision|data|approval|other)\]\s*", "", e["text"]) if action else e["text"]
        body = re.sub(r"\s*_\(added .*?\)_$", "", body).replace('**', '').strip()
        # New PI messages use the same structured Chinese source convention.
        first, sep, rest = body.partition("背景：")
        if re.search(r"[\u4e00-\u9fff]", first) and len(first) <= 100:
            summary = first.removeprefix("要做：").removeprefix("进展：").strip(' ；;')
            background = rest[:360] if sep else "完整证据保留在项目记录中。"
        else:
            refs = "、".join(dict.fromkeys(re.findall(r"\b(?:A|E|F|C|L)\d+\b", body)))[:100]
            summary = "有一项研究进展待整理，请查看项目记录。" if not action or informative else "请查看原始事项并决定下一步。"
            background = f"{refs or '该事项'}的中文说明尚待 PI 补充；请勿仅凭这条提示批准操作。"
    label = "进展" if not action or informative else "待你决定"
    ident = e.get("aid", "")
    text = f"【{label}{' ' + ident if ident else ''}】\n{summary}\n背景：{background}"
    if action and not informative:
        from autolab import autonomy
        deadline = autonomy.read_registry(cfg).get(e["aid"], {}).get("deadline")
        if deadline:
            text += f"\n{deadline[5:10]} {deadline[11:16]} 前不回复，PI 将按推荐方案执行。"
        text += f"\n回复：直接发“{e['aid']} 你的决定”（随时可发“更正 {e['aid']} …”）"
        if verdict_claim(e["text"]):
            text += "\n等级裁定请写：评级 A/B/C/D（选择一个）。"
    else:
        text += "\n无需回复；有建议可发送“留言 你的建议”。"
    return text



def is_progress_query(text):
    """Read-only intent; decision/note commands must retain their explicit semantics."""
    normalized = re.sub(r"[\s，。！？?！,.]+", "", text).lower()
    if normalized.startswith(("回复", "reply", "留言", "note", "更正")) or AID.search(text):
        return False
    if any(w in normalized for w in ("为什么", "为何", "原因", "怎么回事")):
        return False                              # a real question: answered from the records (qa)
    return (normalized in ("状态", "status", "progress", "update") or
            any(word in normalized for word in ("进展", "进度", "做到哪", "做得怎么样", "结果怎么样", "最新结果")))


def is_question(text):
    t = text.lower()
    return any(w in t for w in QUESTION)


def qa_settings(cfg):
    q = (cfg.data.get("weixin", {}) or {}).get("qa", {}) or {}
    return {"enabled": bool(q.get("enabled", True)), "model": str(q.get("model", "sonnet")),
            "usd_per_question": float(q.get("usd_per_question", 0.5)), "usd_per_day": float(q.get("usd_per_day", 5.0)),
            "timeout_s": float(q.get("timeout_s", 300)), "command": str(q.get("command", "claude"))}


QA_PROMPT = """你是 autolab 自主科研项目的只读问答助手。项目目录：{root}
只根据项目文件回答人类的问题。先看 state/STATE.md、state/DECISIONS.md（末尾最新）、state/TREE.md、
00_human/ACTION_NEEDED.md、findings/、experiments/*/outcome.json，需要时再看其它文件。
要求：中文，不超过 400 字；先给直接答案，再给依据（实验/结论/决定编号或文件名）；没有依据就说不确定，
不要编造数字；不要给出命令或代码；不要替 PI 做决定，需要人类拍板的事项直接说明编号和选项。
问题：{question}"""


def _qa_spent_today(cfg):
    today = now_iso()[:10]
    return sum(float(r.get("usd") or 0) for r in read_jsonl(cfg.layout.state / "budget.jsonl")
               if r.get("source") == "weixin_qa" and str(r.get("at", ""))[:10] == today)


def qa_step(cfg, state):
    """Start queued questions as read-only `claude -p` runs and collect finished answers.
    Runs in its own process group and directory: no project CLAUDE.md, no write or shell tools,
    credentials denied; each answer costs at most usd_per_question, the day at most usd_per_day."""
    import os
    import shutil
    import subprocess
    s = qa_settings(cfg)
    qdir = directory(cfg) / "qa"
    for key, q in state.get("questions", {}).items():
        out = qdir / f"{key}.json"
        if q["status"] == "queued":
            from autolab import budget
            if not s["enabled"] or shutil.which(s["command"]) is None:
                q["status"] = "skipped"
                why = "即时问答未启用"
            elif budget.status(cfg)["over"] or _qa_spent_today(cfg) + s["usd_per_question"] > s["usd_per_day"]:
                q["status"] = "skipped"
                why = "今天的即时问答额度已用完"
            else:
                qdir.mkdir(parents=True, exist_ok=True)
                settings = qdir / "settings.json"
                home = Path.home().as_posix().lstrip("/")
                write_json(settings, {"permissions": {"deny": [
                    f"Read(//{home}/.ssh/**)", f"Read(//{home}/.codex/**)", f"Read(//{home}/.claude/.credentials.json)",
                    f"Read(//{home}/.cache/autolab/**)", f"Read(//{home}/.config/**)", "Bash", "Write", "Edit",
                    "WebFetch", "WebSearch"]}})
                args = ([shutil.which("timeout"), f"{int(s['timeout_s'])}"] if shutil.which("timeout") else [])
                args += [s["command"], "-p", QA_PROMPT.format(root=cfg.layout.root.resolve(), question=q["text"][:1500]),
                        "--model", s["model"], "--output-format", "json", "--max-turns", "20",
                        "--max-budget-usd", f"{s['usd_per_question']:.2f}", "--add-dir", str(cfg.layout.root.resolve()),
                        "--allowedTools", "Read,Grep,Glob", "--settings", str(settings),
                        "--disallowedTools", "Bash,Write,Edit,MultiEdit,NotebookEdit,WebFetch,WebSearch,Agent,Task"]
                subprocess.Popen(["sh", "-c", 'out="$1"; shift; "$@" > "$out.part" 2> "$out.err"; mv "$out.part" "$out"',
                                  "sh", str(out), *args], cwd=qdir, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={**os.environ, "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"})
                q.update(status="running", started=time.time())
                continue
            state["received"]["qa-" + key] = {"text": f"{why}；问题已转给 PI，下一轮回复。", "sent": False,
                                              "progress_query": True}
        elif q["status"] == "running":
            if out.is_file():
                try:
                    res = json.loads(out.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    res = {"is_error": True, "result": ""}
                usd = res.get("total_cost_usd")
                from autolab import budget
                # a run without a cost report is charged its cap (an unknown charge would reserve a PI turn)
                budget.record_cost(cfg, "weixin_qa", usd if usd is not None else s["usd_per_question"],
                                   {"question": key, "estimated": usd is None})
                budget.record_call(cfg, "weixin_qa", "error" if res.get("is_error") else "ok", [s["model"]],
                                   res.get("usage") or {}, usd, round(time.time() - q["started"], 1), key)
                answer = str(res.get("result") or "").strip()
                text = ("【即时回答 · 只读查阅项目记录，可能不完整】\n" + answer[:MESSAGE_LIMIT - 200]
                        if answer and not res.get("is_error") else "这次没能从项目记录里查到答案；问题已转给 PI，下一轮回复。")
                state["received"]["qa-" + key] = {"text": text, "sent": False, "progress_query": True}
                q["status"] = "done"
            elif time.time() - q.get("started", 0) > s["timeout_s"]:
                q["status"] = "timeout"
                state["received"]["qa-" + key] = {"text": "查阅超时；问题已转给 PI，下一轮回复。", "sent": False,
                                                  "progress_query": True}


def progress_report(cfg):
    """Latest persisted evidence, not a canned response or an invented live result."""
    from datetime import datetime
    root = cfg.layout.root
    pieces = ["【最新科研进展】"]
    notifications = read_jsonl(cfg.layout.state / "human_notifications.jsonl")
    if notifications:
        rec = notifications[-1]
        text = str(rec.get("text", "")).strip()
        if re.search(r"[\u4e00-\u9fff]", text):
            pieces.append("最近汇报：" + text[:600])
        else:
            pieces.append("最近汇报尚未整理为中文；下面列出已记录的实验结果。")
        pieces.append("汇报时间：" + str(rec.get("at", "未记录")))
    outcomes = []
    for path in cfg.layout.experiments.glob("*/outcome.json"):
        if path.parent.name.startswith("_"):
            continue
        rec = read_json(path, {}) or {}
        outcomes.append((path.stat().st_mtime, path, rec))
    for stamp, path, rec in sorted(outcomes, key=lambda item: item[0], reverse=True)[:3]:
        engineering = {"valid": "运行有效", "invalid": "运行无效"}.get(rec.get("engineering"), "运行有效性未判定")
        hypothesis = {"supports": "支持假设", "refutes": "反驳假设", "inconclusive": "结论不明确"}.get(rec.get("hypothesis"), "假设未判定")
        value = {"continue": "继续", "refine": "完善验证", "pivot": "考虑转向", "stop": "建议停止"}.get(rec.get("value"), "待定")
        pieces.append(f"实验 {path.parent.name}：{engineering}；{hypothesis}；下一步：{value}。")
        reason = str(rec.get("reason", ""))
        if re.search(r"[\u4e00-\u9fff]", reason):
            pieces.append("背景：" + reason[:180])
        pieces.append("结果时间：" + str(rec.get("at") or datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")))
    if not notifications and not outcomes:
        pieces.append("尚无已记录的进展汇报或实验结论；不能据此判断任务没有运行。")
    status = read_json(cfg.layout.state / "pi_status.json", {}) or {}
    stopped = human.stopped(cfg)
    label = "已设置暂停标记" if stopped else {"running": "上次记录为运行中", "idle": "上次记录为空闲"}.get(status.get("state"), "暂无运行状态记录")
    pieces.append("PI：" + label + "。")
    stamp = status.get("finished") or status.get("started")
    if stamp:
        pieces.append("状态记录时间：" + str(stamp))
    state_file = cfg.layout.state / "STATE.md"
    if state_file.is_file():
        pieces.append("研究状态文件更新：" + datetime.fromtimestamp(state_file.stat().st_mtime).astimezone().isoformat(timespec="seconds"))
    pending = actions(cfg)
    pieces.append(f"待回复事项：{len(pending)} 项（含通知类事项）。")
    # Surface the current blockers using source-version-bound Chinese copy.
    decisions = [e for e in events(cfg).values() if e.get("kind") == "action"
                 and "[BLOCKING]" in e.get("text", "")]
    for event in sorted(decisions, key=lambda e: e.get("aid", ""), reverse=True)[:3]:
        copy = event.get("copy") or {}
        if copy:
            pieces.append(f"待拍板 {event['aid']}：{copy['summary']} 背景：{copy['background'][:160]}")
        else:
            pieces.append(f"待拍板 {event['aid']}：该事项仍未关闭，中文说明待补充。")
    pieces.append("以上来自项目已落盘记录；实验支持假设不等于已验收结论。")
    return "\n".join(pieces)


def receive(cfg, state, msg):
    """Only called after exact sender authentication. Persist before advancing cursor."""
    text = "\n".join(i.get("text_item", {}).get("text", "") for i in msg.get("item_list", [])
                     if i.get("type") == 1).strip()
    if not text:
        return
    mid = str(msg.get("message_id") or "")
    if not mid:
        return  # no stable identity => cannot safely deduplicate a human decision
    key = digest([msg.get("from_user_id"), mid])
    if key in state.setdefault("received", {}):
        return
    answer = None
    if is_progress_query(text):
        if text in ("状态", "status"):
            state["deliveries"] = {}
        answer = progress_report(cfg)
        # Explicit status request also resends outstanding decision cards.
        for e in state.get("events", {}).values():
            if text in ("状态", "status") and e.get("kind") == "action" and e.get("aid") in actions(cfg):
                e["sent"] = False
                e.pop("next_chunk", None)
    else:
        # Anything else is kept: a decision when it names an open request (anywhere in the text,
        # no code needed, later messages correct earlier ones), otherwise a note to the PI; a
        # question is also answered at once from the project records (read-only, `qa_step`).
        replies = state.setdefault("replies", {})
        open_ = actions(cfg)
        named = [f"A{m}" for m in AID.findall(text)]
        hit = next((a for a in named if a in open_), None)
        body = re.sub(r"^\s*(回复|reply|更正|留言|note)[\s:：]*", "", text, flags=re.I)
        if hit:
            stripped = re.sub(rf"^\s*[Aa]{hit[1:]}(?!\d)[\s:：,，]*", "", body)
            body = stripped if stripped != body else re.sub(rf"(?<![A-Za-z0-9])[Aa]{hit[1:]}(?!\d)", "", body, count=1)
            body = re.sub(r"^[0-9a-f]{8}\s+", "", body).strip()      # an old-style check code is ignored
            if not body:
                answer = f"请在 {hit} 后面写上你的决定，例如“{hit} 同意”或“{hit} 选 b，理由……”。"
            else:
                line = open_[hit]
                notified = [e for e in state.get("events", {}).values() if e.get("aid") == hit and e.get("sent")]
                seen = max(notified, key=lambda e: e.get("created", 0))["version"] if notified else None
                replies[key] = {"aid": hit, "version": action_version(cfg, line), "seen_version": seen, "body": body,
                                "sender": msg["from_user_id"], "at": now_iso(), "status": "queued",
                                "correction": text.lstrip().startswith("更正") or any(
                                    r.get("aid") == hit for r in replies.values()),
                                "unseen_version": bool(notified) and all(
                                    e.get("version") != action_version(cfg, line) for e in notified)}
                answer = (f"已收到：{hit} → “{body[:120]}”。下一轮（约 15 分钟内）交给 PI 执行；"
                          f"理解有误可发“更正 {hit} …”。预算和硬性规定仍然有效。")
        elif body.strip():
            replies[key] = {"aid": "NOTE", "version": None, "body": body.strip(),
                            "sender": msg["from_user_id"], "at": now_iso(), "status": "queued"}
            closed = [a for a in named if a not in open_]
            prefix = f"{'、'.join(closed)} 已关闭；" if closed else ""
            if is_question(text) and qa_settings(cfg)["enabled"]:
                state.setdefault("questions", {})[key] = {"text": body.strip(), "status": "queued", "at": now_iso()}
                answer = f"{prefix}收到问题，正在查项目记录，1–2 分钟内回复；同时已转给 PI。"
            else:
                answer = f"{prefix}已作为留言转给 PI，下一轮处理。"
    if answer is None:
        return
    state["received"][key] = {"text": answer, "sent": False,
                              "progress_query": is_progress_query(text) and text not in ("状态", "status")}


def cycle(cfg, client):
    d = directory(cfg)
    old = read_json(d / "state.json", {})
    batch = client.call("getupdates", {"get_updates_buf": old.get("cursor", "")})
    with FileLock(d / "state"):
        state = read_json(d / "state.json", {})
        discovered = events(cfg)
        existing = state.setdefault("events", {})
        # Bootstrap silently records historical successes, but sends all open requests.
        first = not state.get("initialized")
        for key, event in discovered.items():
            if key not in existing:
                event.update(token=secrets.token_hex(4), sent=first and event["kind"] != "action")
                existing[key] = event
            if event.get("copy") != existing[key].get("copy"):
                existing[key]["copy"] = event.get("copy")
                existing[key]["sent"] = False
            elif event["kind"] == "action" and time.time() - existing[key]["created"] > 7 * 86400:
                existing[key].update(token=secrets.token_hex(4), created=time.time(), sent=False)
        for msg in batch.get("msgs", []):
            if msg.get("from_user_id") != client.account["user_id"] or msg.get("message_type") != 1:
                continue
            if msg.get("context_token"):
                state["context"] = msg["context_token"]
                mid = str(msg.get("message_id") or "")
                if mid and digest([msg.get("from_user_id"), mid]) not in state.get("received", {}):
                    state["last_inbound_at"] = now_iso()
                    for bucket in ("events", "received"):
                        for pending in state.get(bucket, {}).values():
                            pending.pop("retry_at", None)
                            pending.pop("failures", None)
            receive(cfg, state, msg)
        if batch.get("get_updates_buf"):
            state["cursor"] = batch["get_updates_buf"]
        state["initialized"] = True
        qa_step(cfg, state)
        # Migrate the former permanent latch. A transient -2 must never require
        # another human message just to resume automatic attempts.
        state.pop("blocked_context", None)
        write_json(d / "state.json", state)
    # Never hold the state lock during network operations (runner also uses it).
    if not state.get("context"):
        return
    outgoing = []
    for key, e in state["events"].items():
        if e.get("sent") or key not in discovered:
            continue
        text = render_event(cfg, e)
        outgoing.append(("events", key, text))
    outgoing += [("received", k, e["text"]) for k, e in state.get("received", {}).items()
                 if not e["sent"]]
    outgoing = [(b, k, t) for b, k, t in outgoing
                if state[b][k].get("retry_at", 0) <= time.time()]
    if time.time() < state.get("next_send_at", 0):
        return
    def priority(item):
        b, k, _ = item
        event = state[b][k]
        if b == "received" and event.get("progress_query"):
            return -1
        is_decision = b == "events" and event["kind"] == "action" and not ("[optional]" in event["text"] and "[other]" in event["text"])
        return 0 if is_decision else (1 if b == "received" else 2)
    outgoing.sort(key=lambda item: (priority(item), state[item[0]][item[1]].get("last_attempt", 0)))
    if not outgoing:
        return
    rank = priority(outgoing[0])
    if rank == 2 and time.time() < state.get("next_progress_at", 0):
        return
    # Pack one priority class per delivery. Decisions retain their complete text
    # and individual approval codes; progress is explicitly a summary.
    members, parts = [], []
    for b, k, text in outgoing:
        if priority((b, k, text)) != rank:
            continue
        if parts and len("\n\n".join(parts + [text])) > MESSAGE_LIMIT - 120:
            break
        members.append([b, k])
        parts.append(text)
    text = "\n\n".join(parts)
    if rank == 2:
        text = f"【科研进展 · {len(members)} 项】\n" + text + "\n实验结果不等于已确认的研究结论。"
    key = digest([members, text])
    bucket = "deliveries"
    with FileLock(d / "state"):
        current = read_json(d / "state.json", {})
        entry = current.setdefault(bucket, {}).setdefault(key, {"sent": False})
        entry.setdefault("members", members)
        state[bucket] = current[bucket]
        write_json(d / "state.json", current)
    for bucket, key, text in [(bucket, key, text)]:
        chunks = [text[i:i + MESSAGE_LIMIT] for i in range(0, len(text), MESSAGE_LIMIT)]
        # Persist each acknowledged chunk so a later failure does not replay the
        # entire card. Scope checkpoints to the rendered text (codes can renew).
        payload = digest(text)
        entry = state[bucket][key]
        start = entry.get("next_chunk", 0) if entry.get("payload") == payload else 0
        for i in range(start, min(start + 1, len(chunks))):
            chunk = chunks[i]
            try:
                client.send(f"({i + 1}/{len(chunks)}) {chunk}" if len(chunks) > 1 else chunk,
                            state["context"], digest([bucket, key, payload, i]))
            except (RuntimeError, OSError) as e:
                with FileLock(d / "state"):
                    current = read_json(d / "state.json", {})
                    pending = current[bucket][key]
                    failures = pending.get("failures", 0) + 1
                    delay = min(3600, 60 * 2 ** min(failures - 1, 6))
                    pending.update(failures=failures, retry_at=time.time() + delay,
                                   last_attempt=time.time(),
                                   last_error=f"iLink code={e.code}" if isinstance(e, ILinkError)
                                   else "transport failure")
                    current["next_send_at"] = time.time() + delay
                    for b, k in members:
                        current[b][k].update(retry_at=time.time() + delay, last_attempt=time.time())
                    write_json(d / "state.json", current)
                print(f"Weixin send failed ({pending['last_error']}); pending item retries in {delay}s.", flush=True)
                break
            with FileLock(d / "state"):
                current = read_json(d / "state.json", {})
                current[bucket][key].update(payload=payload, next_chunk=i + 1,
                                          last_attempt=time.time())
                current["next_send_at"] = time.time() + SEND_INTERVAL
                write_json(d / "state.json", current)
        else:
            if start + 1 < len(chunks):
                return
            with FileLock(d / "state"):
                current = read_json(d / "state.json", {})
                current[bucket][key].update(sent=True, accepted_at=now_iso())
                for field in ("retry_at", "failures", "last_error"):
                    current[bucket][key].pop(field, None)
                for b, k in members:
                    current[b][k].update(sent=True, accepted_at=now_iso())
                    current[b][k].pop("retry_at", None)
                if rank == 2:
                    current["next_progress_at"] = time.time() + PROGRESS_INTERVAL
                write_json(d / "state.json", current)


def apply_replies(cfg):
    """Runner only: import at a turn boundary, BEFORE the next guard snapshot."""
    d = directory(cfg)
    if not (d / "state.json").is_file():
        return 0
    count = 0
    with FileLock(d / "state"):
        state = read_json(d / "state.json", {})
        for key, reply in state.get("replies", {}).items():
            if reply["status"] != "queued":
                continue
            target = cfg.layout.inbox / f"weixin-{reply['aid']}-{key}.md"
            line = actions(cfg).get(reply["aid"])
            notes = []
            if target.exists():  # recovery after file written but state not committed
                reply["status"] = "applied"
            else:
                if reply["aid"] != "NOTE" and not line:
                    # answered or timed out meanwhile: still deliver it; the PI reconciles
                    target = cfg.layout.inbox / f"weixin-NOTE-{key}.md"
                    notes.append(f"This was an answer to {reply['aid']}, which had already been closed "
                                 "(answered or timed out). If it differs from what the PI did, follow the human.")
                elif reply["aid"] != "NOTE" and action_version(cfg, line) != reply["version"]:
                    notes.append("The request text changed after the human answered: check that the answer still fits, "
                                 "and ask once more if it clearly does not.")
                claim = verdict_claim(line)
                grade = re.fullmatch(r"(?:评级|grade) ([ABCD])", reply["body"].strip())
                if claim and grade:
                    from autolab import gates
                    fp = gates.fingerprint(cfg, claim)["fingerprint"]
                    # the human must have been shown this exact claim version
                    if line and action_version(cfg, line) == reply["version"] == reply.get("seen_version", reply["version"]):
                        write_atomic(cfg.layout.human / "verdicts" / f"{claim}.toml",
                                     f'grade = "{grade[1]}"\nfingerprint = "{fp}"\n')
                    else:      # a grade is bound to the exact claim version it was given for
                        notes.append(f"The grade was NOT recorded: claim {claim} changed after the human graded it.")
                if reply.get("correction"):
                    notes.append("This corrects an earlier answer to the same request: the latest answer wins.")
                write_atomic(target, f"# Human Weixin reply: {reply['aid']}\n\n"
                             f"Received: {reply['at']}\nRequest version: {reply['version']}\n"
                             + "".join(f"Note: {n}\n" for n in notes) + f"\n{reply['body']}\n")
                reply["status"] = "applied"
                count += 1
            state.setdefault("received", {})["apply-" + key] = {
                "text": f"{reply['aid'] if reply['aid'] != 'NOTE' else '留言'}: 已交给 PI（写入人类 inbox）"
                        + ("；评级未记录，因为结论在你评级后改过" if any("NOT recorded" in n for n in notes) else "")
                        + "。", "sent": False}
        write_json(d / "state.json", state)
    return count


def run(cfg, action):
    if action == "login":
        return login(cfg)
    if action == "apply":
        print(f"Imported {apply_replies(cfg)} Weixin replies")
        from autolab.events import sync
        sync(cfg)
        return
    d = directory(cfg)
    account = read_json(d / "account.json")
    if not account or not account.get("user_id"):
        raise RuntimeError("Run `autolab weixin login` first")
    client = Client(account)
    stop_on_signals()
    lock = FileLock(d / "listener")
    try:
        lock.__enter__()
    except TimeoutError:
        owner = read_json(lock.dir / "owner.json", {}) or {}
        raise RuntimeError(f"another listener holds {lock.dir} (pid {owner.get('pid')}, since {owner.get('created')}). "
                           "If that process has exited, remove the directory and start again.") from None
    try:
        print("Listening for the bound Weixin user; Ctrl+C to stop.", flush=True)
        while True:
            try:
                cycle(cfg, client)
                from autolab.events import sync
                sync(cfg)
            except (OSError, RuntimeError, ValueError, KeyError) as e:
                # Never print response bodies, bearer tokens, or user message contents.
                detail = str(e) if isinstance(e, RuntimeError) else type(e).__name__
                print(f"Weixin cycle failed: {detail}; retrying in 30s. Re-login if session expired.", flush=True)
                time.sleep(30)
            else:
                time.sleep(1)
    finally:
        lock.__exit__(None, None, None)    # also on SIGHUP/SIGTERM (stop_on_signals)


def stop_on_signals():
    """Turn SIGTERM/SIGHUP into a normal exit so the listener lock is released. A listener stopped
    by `tmux kill-session` (SIGHUP) used to leave its lock behind; the next start then gave up
    after 60 s and Weixin stayed silent for seven hours (first-test, 2026-10-18)."""
    import signal

    def _stop(signum, frame):
        raise SystemExit(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _stop)
