"""PI execution seam. Adapters must preserve sandbox, budget and accounting contracts."""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Protocol
from autolab.config import Config
from autolab.util import now_iso, write_json

@dataclass(frozen=True)
class TurnRequest:
    prompt: str
    model: str
    max_turns: int
    budget_usd: float | None = None

class Backend(Protocol):
    def command(self, request: TurnRequest) -> list[str]: ...

class ClaudeCode:
    def command(self, request: TurnRequest) -> list[str]:
        if request.max_turns < 1:
            raise ValueError("max_turns must be positive")
        argv = ["claude", "-p", request.prompt, "--model", request.model,
                "--permission-mode", "acceptEdits", "--max-turns", str(request.max_turns),
                "--disallowedTools", "ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,RemoteTrigger",
                "--output-format", "stream-json", "--verbose"]
        if request.budget_usd is not None:
            import math
            if not math.isfinite(request.budget_usd) or request.budget_usd <= 0:
                raise ValueError("budget must be finite and positive")
            argv += ["--max-budget-usd", str(request.budget_usd)]
        return argv

BACKENDS: dict[str, Backend] = {"claude-code": ClaudeCode()}

def backend(cfg):
    name = cfg.data.get("execution", {}).get("backend", "claude-code")
    if name not in BACKENDS:
        raise ValueError(f"unsupported execution backend: {name}; available: claude-code")
    return name, BACKENDS[name]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["check", "launch", "finish"])
    p.add_argument("--prompt")
    p.add_argument("--model")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--budget", type=float)
    p.add_argument("--log")
    p.add_argument("--rc", type=int)
    p.add_argument("--seconds", type=int)
    a = p.parse_args()
    cfg = Config(Path.cwd())
    name, adapter = backend(cfg)
    if a.action == "check":
        return
    if not a.log:
        p.error("--log is required")
    log = Path(a.log).resolve()
    if not log.is_relative_to((cfg.layout.state / "pi_logs").resolve()):
        p.error("log must be inside state/pi_logs")
    if a.action == "launch":
        if a.prompt is None or not a.model:
            p.error("--prompt and --model are required")
        argv = adapter.command(TurnRequest(a.prompt, a.model, a.max_turns, a.budget))
        write_json(log.with_suffix(".execution.json"), {
            "schema": 1, "backend": name, "state": "running", "started": now_iso(),
            "model": a.model, "log": log.relative_to(cfg.layout.root).as_posix()})
        from autolab.events import sync
        sync(cfg)
        # Replace this process: GNU timeout retains ownership of the real agent
        # and its process group; no Python wrapper swallows signals or exit codes.
        os.execvp(argv[0], argv)
    else:
        if a.rc is None or a.seconds is None:
            p.error("--rc and --seconds are required")
        from autolab.util import read_json
        path = log.with_suffix(".execution.json")
        result = read_json(path, {})
        result.update(schema=1, backend=name, state="finished", finished=now_iso(),
                      exit_code=a.rc, seconds=a.seconds,
                      status="completed" if a.rc == 0 else "timeout" if a.rc == 124 else "failed")
        # Usage remains in the existing budget ledger; do not invent zero cost.
        result["accounting"] = "see state/budget.jsonl"
        write_json(path, result)
        from autolab.events import sync
        sync(cfg)

if __name__ == "__main__":
    main()
