"""Load project config (autolab.toml), the active domain pack and the cluster site profile.

Three layers, so the general-science core never hard-codes a field:

    framework defaults  <  domains/<domain>/domain.toml  <  project autolab.toml

Switching field = change `project.domain` and re-run `autolab skills mount`.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

from autolab.paths import Layout

DEFAULTS: dict[str, Any] = {
    "project": {"name": "unnamed", "domain": "generic", "site": "local"},
    # Claude Code aliases resolve to the newest model of each family on any CLI version.
    "models": {
        "pi": "opus",
        "student_literature": "haiku",
        "student_experiment": "sonnet",
        "student_analysis": "sonnet",
        "student_figure": "sonnet",
        "student_writing": "sonnet",
    },
    "reviewer": {
        "backend": "codex",
        "billing": "api",              # subscription must be explicitly selected
        "unknown_call_usd": 1.0,        # provisional estimate, not a provider-enforced cap
        # {model} {effort} {workdir} {out_file} are substituted; the prompt is fed on stdin.
        # The final message is read from {out_file} (falls back to stdout).
        "command": ["codex", "exec", "-m", "{model}", "-c", "model_reasoning_effort={effort}",
                    "--sandbox", "read-only", "--skip-git-repo-check", "-C", "{workdir}",
                    "-o", "{out_file}", "-"],
        "timeout_s": 3600,
        "tiers": {
            # light = non-verdict checks only (format, wording, citation syntax).
            # Models offered to Codex on a ChatGPT account as of 2026-10-15 (~/.codex/models_cache.json).
            "light": {"model": "gpt-6-luna", "effort": "high", "verdict_allowed": False},
            "regular": {"model": "gpt-6-astra", "effort": "xhigh", "verdict_allowed": True},
            "deep": {"model": "gpt-6-astra", "effort": "ultra", "verdict_allowed": True},
        },
        "fallback_models": ["gpt-5.6-sol"],
        "tasks": {
            "format_check": "light", "citation_format": "light", "wording": "light",
            "section_review": "regular", "review_loop_round": "regular",
            "citation_audit": "regular", "idea_review": "regular",
            "claim_support": "deep", "experiment_audit": "deep", "novelty": "deep",
            "kill_argument": "deep", "paper_claim_audit": "deep", "final_cold_read": "deep",
            "value_grade": "deep",
        },
        "escalate": {"central_claim": True, "prior_disagreement": True, "hard_topic": True},
    },
    "gates": {
        "human_debounce_minutes": 5,
        "min_grade_to_accept": "B",
        "max_review_rounds": 8,
        "stall_rounds": 2,
        "require_reading_receipts_tier": "A",
        "retro_every": 20,                 # ticks between retrospectives (0 = never)
        "require_candidates": True,        # plan gate: candidate cards + a Discrimination section
        "require_outcomes": True,          # finished experiments must get `autolab outcome` before new plans
        "context_pack_max_items": 12,
        "download_request_tiers": ["A"],      # only these tiers trigger automatic download requests
    },
    # total_usd = hard cap for the whole run (0 = no cap); enforced by scripts/run_pi.sh
    # how long the lab waits for the human (autonomy.py): blocking requests carry the PI's
    # recommendation and are carried out after the timeout, except rules listed in always_ask
    "autonomy": {"decision_timeout_hours": 4,
                 "always_ask": ["budget_increase", "gpu", "new_data_source", "research_goal"],
                 "reminder_hours": 4, "blocked_max_idle_min": 180},
    # Weixin questions answered at once by a read-only model run over the project files
    "weixin": {"qa": {"enabled": True, "model": "sonnet", "usd_per_question": 0.5, "usd_per_day": 5.0,
                      "timeout_s": 300, "command": "claude"}},
    "budget": {"total_usd": 0.0, "usd_per_day": 40.0, "usd_per_task": 5.0},
    # extra shell commands the human allows the agents (rendered into .claude/settings.json by
    # `autolab skills mount`), and extra environment variables for their sessions
    "agents": {"allow_bash": [], "env": {}, "sandbox": False},   # sandbox: OS-level write/network limits for Bash (Linux: bubblewrap)
    "slurm": {
        "account": "",
        "partition": "cpu",
        "qos": "normal",
        "max_cpus": 64,                    # per job, all ranks x cpus (0 = no cap: the scheduler decides)
        "max_mem_gb": 128,
        "max_hours": 24,
        "max_gpus": 0,
        "conda_env": "",
        "require_clean_git": True,
        "total_core_hours": 0,             # cap for all jobs of the project (0 = no cap)
        # what must be committed before a job runs ({eid} = experiment id)
        "clean_paths": ["experiments/{eid}", "src"],   # code and configs in these must be committed; results, logs and pilot outputs are not checked
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def framework_root(cfg: dict | None = None) -> Path:
    """Where core/, domains/, sites/, templates/ live."""
    if cfg and cfg.get("project", {}).get("framework_root"):
        return Path(cfg["project"]["framework_root"]).expanduser()
    env = os.environ.get("AUTOLAB_HOME")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[2]


def load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def load_domain(fw: Path, name: str) -> dict:
    path = fw / "domains" / name / "domain.toml"
    if not path.is_file():
        raise FileNotFoundError(f"domain pack '{name}' not found at {path}")
    data = load_toml(path)
    data.setdefault("domain", {})["path"] = str(path.parent)
    return data


def load_site(fw: Path, name: str) -> dict:
    path = fw / "sites" / f"{name}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"site profile '{name}' not found at {path}")
    return load_toml(path)


class Config:
    """Resolved configuration for one project."""

    def __init__(self, root: Path):
        self.layout = Layout(root)
        raw = load_toml(self.layout.config) if self.layout.config.is_file() else {}
        self.fw = framework_root(raw)
        self.domain = load_domain(self.fw, raw.get("project", {}).get("domain", "generic"))
        merged = deep_merge(DEFAULTS, self.domain.get("overrides", {}))
        self.data = deep_merge(merged, raw)
        self.site = load_site(self.fw, self.data["project"]["site"])

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    @property
    def domain_name(self) -> str:
        return self.data["project"]["domain"]

    @property
    def domain_dir(self) -> Path:
        return Path(self.domain["domain"]["path"])
