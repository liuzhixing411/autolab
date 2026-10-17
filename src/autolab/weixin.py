"""Human-operated Weixin bridge. No model calls and no execution of chat text.

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


class ILinkError(RuntimeError):
    def __init__(self, code, context_rejected=False):
        self.context_rejected = context_rejected
        super().__init__(f"iLink code={code}" + ("; send a fresh Weixin message to refresh context"
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
    # Conservative: any plan/config/human-rule change invalidates an old decision.
    paths = [cfg.layout.root / "autolab.toml", *cfg.layout.human_docs]
    paths += sorted(cfg.layout.experiments.glob("*/plan.md"))
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
    return result


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
    answer = "用法：回复 A编号 校验码 你的决定；留言 你的问题或建议；状态。回复会在下一轮交给 PI，不会直接执行命令。"
    if text in ("状态", "status"):
        answer = f"Autolab {cfg.layout.root.name}: {len(actions(cfg))} 项待回复；STOP={human.stopped(cfg)}。"
        # Explicit status request also resends outstanding decision cards.
        for e in state.get("events", {}).values():
            if e.get("kind") == "action" and e.get("aid") in actions(cfg):
                e["sent"] = False
    elif text.startswith(("留言 ", "note ")) and text.split(maxsplit=1)[-1].strip():
        state.setdefault("replies", {})[key] = {
            "aid": "NOTE", "version": None, "body": text.split(maxsplit=1)[1],
            "sender": msg["from_user_id"], "at": now_iso(), "status": "queued"}
        answer = "留言已收到，下一轮交给 PI；留言不会代替待拍板事项的明确回复。"
    else:
        parts = text.split(maxsplit=3)
        if len(parts) == 4 and parts[0] in ("回复", "reply"):
            _, aid, token, body = parts
            event = next((e for e in state.get("events", {}).values()
                          if e.get("aid") == aid and e.get("token") == token and e.get("sent")), None)
            line = actions(cfg).get(aid)
            if (not event or not line or action_version(cfg, line) != event["version"] or
                    time.time() - event["created"] > 7 * 86400):
                answer = "该事项已关闭、已变更或校验码过期，请使用最新通知。"
            elif any(r.get("aid") == aid and r.get("version") == event["version"]
                     for r in state.get("replies", {}).values()):
                answer = "该版本已有回复，不会重复应用。后续修改请写入人类 inbox。"
            else:
                state.setdefault("replies", {})[key] = {
                    "aid": aid, "version": event["version"], "body": body,
                    "sender": msg["from_user_id"], "at": now_iso(), "status": "queued"}
                answer = f"已收到 {aid} 的决定，等待下一轮接收。预算与实验验收规则仍然有效。"
    state["received"][key] = {"text": answer, "sent": False}


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
            elif event["kind"] == "action" and time.time() - existing[key]["created"] > 7 * 86400:
                existing[key].update(token=secrets.token_hex(4), created=time.time(), sent=False)
        for msg in batch.get("msgs", []):
            if msg.get("from_user_id") != client.account["user_id"] or msg.get("message_type") != 1:
                continue
            if msg.get("context_token"):
                state["context"] = msg["context_token"]
                mid = str(msg.get("message_id") or "")
                if mid and digest([msg.get("from_user_id"), mid]) not in state.get("received", {}):
                    state.pop("blocked_context", None)
            receive(cfg, state, msg)
        if batch.get("get_updates_buf"):
            state["cursor"] = batch["get_updates_buf"]
        state["initialized"] = True
        write_json(d / "state.json", state)
    # Never hold the state lock during network operations (runner also uses it).
    if not state.get("context") or state.get("blocked_context") == state["context"]:
        return
    outgoing = []
    for key, e in state["events"].items():
        if e.get("sent") or key not in discovered:
            continue
        if e["kind"] == "action":
            text = f"【Autolab 待拍板 · {cfg.layout.root.name}】\n{e['text']}\n回复格式：回复 {e['aid']} {e['token']} 你的决定"
            if verdict_claim(e["text"]):
                text += f"\n等级裁定可回复：回复 {e['aid']} {e['token']} 评级 A（或 B/C/D）"
        else:
            text = f"【Autolab 进展 · {cfg.layout.root.name} · {e['kind']}】\n{e['text'][:1800]}\n实验 outcome 不等于最终 finding。"
        outgoing.append(("events", key, text))
    outgoing = [("received", k, e["text"]) for k, e in state.get("received", {}).items()
                if not e["sent"]] + outgoing
    for bucket, key, text in outgoing[:5]:
        chunks = [text[i:i + 1000] for i in range(0, len(text), 1000)]
        for i, chunk in enumerate(chunks):
            try:
                client.send(f"({i + 1}/{len(chunks)}) {chunk}" if len(chunks) > 1 else chunk,
                            state["context"], digest([bucket, key, i]))
            except ILinkError as e:
                if not e.context_rejected:
                    raise
                with FileLock(d / "state"):
                    current = read_json(d / "state.json", {})
                    current["blocked_context"] = state["context"]
                    write_json(d / "state.json", current)
                print("Weixin rejected send context/parameters; pending messages retained. Send a fresh message to retry.", flush=True)
                return
        with FileLock(d / "state"):
            current = read_json(d / "state.json", {})
            current[bucket][key]["sent"] = True
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
            if target.exists():  # recovery after file written but state not committed
                reply["status"] = "applied"
            elif reply["aid"] != "NOTE" and (not line or action_version(cfg, line) != reply["version"]):
                reply["status"] = "stale"
            else:
                claim = verdict_claim(line)
                grade = re.fullmatch(r"(?:评级|grade) ([ABCD])", reply["body"].strip())
                if claim and grade:
                    from autolab import gates
                    fp = gates.fingerprint(cfg, claim)["fingerprint"]
                    write_atomic(cfg.layout.human / "verdicts" / f"{claim}.toml",
                                 f'grade = "{grade[1]}"\nfingerprint = "{fp}"\n')
                write_atomic(target, f"# Human Weixin reply: {reply['aid']}\n\n"
                             f"Received: {reply['at']}\nRequest version: {reply['version']}\n\n"
                             f"{reply['body']}\n")
                reply["status"] = "applied"
                count += 1
            state.setdefault("received", {})["apply-" + key] = {
                "text": f"{reply['aid']}: " + ("已写入人类 inbox，PI 下一轮读取。" if reply['status'] == 'applied'
                                             else "方案已变化，旧回复未应用，请重新决定。"), "sent": False}
        write_json(d / "state.json", state)
    return count


def run(cfg, action):
    if action == "login":
        return login(cfg)
    if action == "apply":
        print(f"Imported {apply_replies(cfg)} Weixin replies")
        return
    d = directory(cfg)
    account = read_json(d / "account.json")
    if not account or not account.get("user_id"):
        raise RuntimeError("Run `autolab weixin login` first")
    client = Client(account)
    with FileLock(d / "listener"):
        print("Listening for the bound Weixin user; Ctrl+C to stop.", flush=True)
        while True:
            try:
                cycle(cfg, client)
            except (OSError, RuntimeError, ValueError, KeyError) as e:
                # Never print response bodies, bearer tokens, or user message contents.
                detail = str(e) if isinstance(e, RuntimeError) else type(e).__name__
                print(f"Weixin cycle failed: {detail}; retrying in 30s. Re-login if session expired.", flush=True)
                time.sleep(30)
            else:
                time.sleep(1)
