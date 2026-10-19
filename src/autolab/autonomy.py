"""How long the lab waits for the human, and what the PI may decide alone.

First-test run, 2026-10-17/18: three tracks sat idle for 37 hours behind blocking requests that
had no deadline and no default ("an unanswered request is not permission"). Now every blocking
request carries the PI's recommendation and a deadline (`[autonomy] decision_timeout_hours`,
default 4). When the deadline passes without an answer, the tick closes the request as
"timed out", records the decision (`by timeout`), tells the human, and the PI carries out its
recommendation; if the human disagrees later, the PI reverses it. Requests under a rule listed in
`[autonomy] always_ask` (and framework integrity or claim-verdict requests) never time out.
While the lab waits, the human is reminded every `reminder_hours` with the one thing needed.

Registry: state/requests.json {A-id: {rule, recommend, deadline, created, reminded}}.
"""

from __future__ import annotations

import datetime as dt
import re

from autolab.config import Config
from autolab.util import FileLock, now_iso, read_json, write_json

ALWAYS_ASK_HELP = {
    "budget_increase": "raise the API budget or another hard limit",
    "gpu": "request GPUs",
    "new_data_source": "bring in a new data source",
    "research_goal": "change the research goal or research question",
    "mpi_code": "start writing the MPI implementation",
}
NO_TIMEOUT_RULES = ("claim_verdict", "integrity")      # framework requests: always wait


def _path(cfg: Config):
    return cfg.layout.state / "requests.json"


def settings(cfg: Config) -> dict:
    a = cfg.data.get("autonomy", {})
    return {"timeout_h": float(a.get("decision_timeout_hours", 4) or 0),
            "always_ask": list(a.get("always_ask", [])),
            "reminder_h": float(a.get("reminder_hours", 4) or 0),
            "blocked_max_idle_min": float(a.get("blocked_max_idle_min", 180) or 0)}


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _parse(ts: str | None) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(ts))
        return t if t.tzinfo else t.astimezone()
    except (TypeError, ValueError):
        return None


def read_registry(cfg: Config) -> dict:
    return read_json(_path(cfg), {}) or {}


def waits_forever(cfg: Config, rule: str | None) -> bool:
    return bool(rule) and (rule in settings(cfg)["always_ask"] or rule in NO_TIMEOUT_RULES)


def plan_request(cfg: Config, rule: str | None, recommend: str | None, deadline_hours: float | None) -> dict:
    """Metadata for a new blocking request; raises when the PI did not say what it would do."""
    s = settings(cfg)
    if waits_forever(cfg, rule) or not s["timeout_h"]:
        return {"rule": rule or "", "recommend": (recommend or "").strip(), "deadline": None}
    if not (recommend or "").strip():
        raise ValueError("a blocking request needs --recommend \"<what you will do if the human does not answer>\" "
                         f"(it is carried out after {s['timeout_h']:g} h without an answer); rules that always wait: "
                         f"{', '.join(s['always_ask'] + list(NO_TIMEOUT_RULES))}")
    hours = float(deadline_hours) if deadline_hours else s["timeout_h"]
    return {"rule": rule or "", "recommend": recommend.strip(),
            "deadline": (_now() + dt.timedelta(hours=hours)).isoformat(timespec="seconds")}


def suffix(meta: dict) -> str:
    """Visible part of the request line."""
    rule = f" Rule: {meta['rule']}." if meta.get("rule") else ""
    if meta.get("deadline"):
        return (f"{rule} PI recommends: {meta['recommend']} If there is no answer by {meta['deadline']}, the PI "
                "carries this out (and reverses it if you disagree later).")
    return f"{rule} Waits for your answer (no timeout)." + (f" PI recommends: {meta['recommend']}" if meta.get("recommend") else "")


def register(cfg: Config, aid: str, meta: dict) -> None:
    with FileLock(_path(cfg)):
        reg = read_json(_path(cfg), {}) or {}
        reg[aid] = {**meta, "created": now_iso(), "reminded": now_iso()}
        write_json(_path(cfg), reg)


def _open_blocking(cfg: Config) -> dict[str, str]:
    from autolab import human
    out = {}
    for line in human.open_actions(cfg):
        m = re.match(r"- \[ \] \*\*(A\d+)\*\* \[BLOCKING\]", line)
        if m:
            out[m[1]] = line
    return out


def blocked(cfg: Config) -> list[str]:
    return sorted(_open_blocking(cfg))


def overdue(cfg: Config) -> list[str]:
    reg = read_json(_path(cfg), {}) or {}
    now = _now()
    return [a for a in _open_blocking(cfg)
            if (d := _parse(reg.get(a, {}).get("deadline"))) is not None and d <= now]


_RECOMMEND = (re.compile(r"(?i)\b(?:my |pi )?recommend(?:ation|s|ed)?\b\s*(?:is\s*)?[:：]\s*(.+?)(?:[.;。；](?=\s|$)|$)"),
              re.compile(r"\bPI proposes to (.+?)(?:\.(?=\s|$)|$)"),
              re.compile(r"(?:推荐|建议)(?:方案)?[:：]\s*(.+?)(?:[。；]|$)"))


def extract_recommendation(line: str) -> str:
    """The recommendation a request states in its own words (requests filed before 0.6 carry it
    only in their text); '' when there is none."""
    body = re.sub(r"\s*_\(added .*?\)_\s*$", "", line).replace("**", "")
    for pat in _RECOMMEND:
        m = pat.search(body)
        if m and m.group(1).strip():
            return m.group(1).strip()[:200]
    return ""


def chinese_copy(cfg: Config, aid: str) -> dict:
    """The PI's latest concise Chinese copy of a request (state/notification_copy.json), if any."""
    copies = read_json(cfg.layout.state / "notification_copy.json", {}) or {}
    found = [v for k, v in copies.items() if k.startswith(f"action:{aid}:")]
    return found[-1] if found else {}


def adopt_legacy(cfg: Config) -> list[str]:
    """Blocking requests filed before deadlines existed get one now (from deployment), with the
    recommendation stated in their own text. Framework verdict/integrity requests keep waiting."""
    s = settings(cfg)
    adopted = []
    with FileLock(_path(cfg)):
        reg = read_json(_path(cfg), {}) or {}
        for aid, line in _open_blocking(cfg).items():
            if aid in reg:
                continue
            framework = "00_human/verdicts/" in line or "Integrity guard" in line
            deadline = None if framework or not s["timeout_h"] else \
                (_now() + dt.timedelta(hours=s["timeout_h"])).isoformat(timespec="seconds")
            reg[aid] = {"rule": "claim_verdict" if "00_human/verdicts/" in line else
                        ("integrity" if framework else ""),
                        "recommend": "" if framework else (extract_recommendation(line)
                                                           or "the option the request itself recommends"),
                        "deadline": deadline, "created": now_iso(), "reminded": now_iso(), "legacy": True}
            adopted.append(aid)
        if adopted:
            write_json(_path(cfg), reg)
    return adopted


def process(cfg: Config) -> list[dict]:
    """Close overdue requests: tick them off, record the decision, tell the human. Returns what the
    PI must now carry out."""
    from autolab import human
    from autolab.tasks import decide
    done = []
    reg = read_json(_path(cfg), {}) or {}
    for aid in overdue(cfg):
        meta = reg[aid]
        rec = meta.get("recommend") or "the recommended option"
        n = human.resolve_actions(cfg, f"**{aid}**", f"timed out (no answer by {meta['deadline']}); "
                                  f"the PI proceeds with: {rec}")
        if not n:
            continue
        did = decide(cfg, f"{aid} timed out without a human answer; proceed with the PI recommendation: {rec}",
                     f"[autonomy] decision_timeout_hours passed (deadline {meta['deadline']}); "
                     "reverse if the human disagrees later", aid, by="timeout")
        cn = chinese_copy(cfg, aid)
        what = f"（{cn['summary'][:60]}）" if cn.get("summary") else ""
        human.notify(cfg, f"{aid}{what}超时未回复，PI 已按推荐方案执行；如不同意请直接回复 {aid} 和你的决定。"
                          f"背景：推荐方案：{rec[:200]}" + (f"。{cn['background'][:150]}" if cn.get("background") else ""))
        with FileLock(_path(cfg)):
            r2 = read_json(_path(cfg), {}) or {}
            r2.setdefault(aid, meta)["timed_out"] = now_iso()
            r2[aid]["decision"] = did
            write_json(_path(cfg), r2)
        done.append({"aid": aid, "recommend": rec, "decision": did})
    return done


def remind(cfg: Config) -> list[str]:
    """Re-notify open blocking requests every `reminder_hours` (runner side, no API cost)."""
    from autolab import human
    s = settings(cfg)
    if not s["reminder_h"]:
        return []
    now = _now()
    sent = []
    with FileLock(_path(cfg)):
        reg = read_json(_path(cfg), {}) or {}
        for aid, line in _open_blocking(cfg).items():
            meta = reg.setdefault(aid, {"reminded": now_iso()})
            last = _parse(meta.get("reminded")) or now
            if (now - last).total_seconds() < s["reminder_h"] * 3600:
                continue
            when = f"，{meta['deadline'][11:16]} 前不回复将按 PI 推荐执行" if meta.get("deadline") else "，此事项会一直等你"
            cn = chinese_copy(cfg, aid)
            what = f"（{cn['summary'][:60]}）" if cn.get("summary") else ""
            rec = f"PI 推荐：{meta['recommend'][:150]}；" if meta.get("recommend") else ""
            human.notify(cfg, f"提醒：{aid}{what}仍在等你决定{when}。背景：{rec}可直接回复“{aid} 你的决定”；状态可发“进展”。")
            meta["reminded"] = now_iso()
            sent.append(aid)
        if sent:
            write_json(_path(cfg), reg)
    return sent
