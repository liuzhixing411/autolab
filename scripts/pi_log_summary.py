"""Summarize a PI turn log (claude -p --output-format stream-json).

    python pi_log_summary.py <log.jsonl> [--all]

Prints: tool calls in order (main agent and subagents), hook blocks, permission denials,
tool errors, the final message, and cost/turns. --all prints every call; default prints
the first 80 plus every error.
"""

import json
import sys
from collections import Counter


def main(path: str, show_all: bool = False) -> None:
    uses, rows, final = {}, [], None
    counts, sub_counts = Counter(), Counter()
    for line in open(path, encoding="utf-8", errors="ignore"):
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        parent = ev.get("parent_tool_use_id")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        for c in content:
            if c.get("type") == "tool_use":
                inp = c.get("input", {}) or {}
                detail = (inp.get("command") or inp.get("file_path") or inp.get("pattern")
                          or inp.get("subagent_type") or inp.get("description") or inp.get("url") or "")
                who = "sub" if parent else "PI "
                uses[c["id"]] = (who, c["name"], str(detail).replace("\n", " ")[:140])
                (sub_counts if parent else counts)[c["name"]] += 1
            elif c.get("type") == "tool_result":
                txt = c.get("content")
                if isinstance(txt, list):
                    txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
                rows.append((c.get("tool_use_id"), bool(c.get("is_error")), str(txt).replace("\n", " ")[:200]))
        if ev.get("type") == "result":
            final = ev

    blocked = [r for r in rows if r[1] and "hook" in r[2].lower()]
    denied = [r for r in rows if r[1] and ("permission" in r[2].lower() or "not allowed" in r[2].lower())]
    errors = [r for r in rows if r[1] and r not in blocked and r not in denied]
    print(f"tool calls: PI {sum(counts.values())} {dict(counts)}")
    print(f"            subagents {sum(sub_counts.values())} {dict(sub_counts)}")
    print(f"hook blocks {len(blocked)} | permission denials {len(denied)} | other errors {len(errors)}")
    print("--- calls")
    for i, (tid, err, txt) in enumerate(rows):
        if not show_all and i >= 80 and not err:
            continue
        who, name, detail = uses.get(tid, ("?", "?", ""))
        flag = "ERR" if err else "ok "
        print(f"{flag} {who} {name}: {detail}" + (f"  =>  {txt}" if err else ""))
    if final:
        print("--- final")
        print((final.get("result") or "")[:1500])
        print(f"cost ${final.get('total_cost_usd')} | turns {final.get('num_turns')} | "
              f"duration {round((final.get('duration_ms') or 0) / 1000)}s | subtype {final.get('subtype')} | "
              f"models {list((final.get('modelUsage') or {}).keys())}")
    else:
        print("--- turn still running (no result event yet)")


if __name__ == "__main__":
    main(sys.argv[1], "--all" in sys.argv)
