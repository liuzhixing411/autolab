"""SLURM backend: render → validate (sbatch --test-only) → submit → status.

Agents never call sbatch/scancel directly (the hook blocks it); they go through
`autolab slurm ...`, which enforces resource caps from autolab.toml, refuses to submit
an experiment whose plan did not pass the knowledge gate, and records every job.
Site specifics (paths, conda activation, partitions) come from sites/<site>.toml.
"""

from __future__ import annotations

import math
import re
import os
import shutil
import subprocess
from pathlib import Path

from autolab.config import Config
from autolab.human import stopped
from autolab.knowledge import plan_gate_passed
from autolab.util import append_jsonl, now_iso, read_frontmatter, read_jsonl, write_atomic

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
            "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
RESOURCE_KEYS = {"partition", "qos", "cpus", "mem_gb", "hours", "gpus", "nodes", "ntasks", "array",
                 "mem_reason", "cpus_reason"}
# commands that use more than one core; a multi-core request without one of these (or a
# `cpus_reason`) gets a warning: jobs in the first long run used 3-11 % of their cores
_PARALLEL_HINT = re.compile(r"\b(srun|mpirun|mpiexec|OMP_NUM_THREADS|NUMBA_NUM_THREADS)\b|--threads|--workers|"
                            r"(?<!\S)-j\s*\d|(?<!\S)-np?\s+\d")
# script files a command runs (no shell variables or globs in the name)
_SCRIPT = re.compile(r"(?<![\w$*/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|sh|c|cpp|f90|jl|R))(?![\w/])")


def _exe(name: str) -> str:
    override = os.environ.get(f"AUTOLAB_{name.upper()}")
    path = override or shutil.which(name)
    if not path:
        raise RuntimeError(f"`{name}` not found; this command must run on a SLURM cluster")
    return path


def resources(cfg: Config, eid: str, part: str | None = None) -> dict:
    """Resources and command for an experiment, or for one named part of it.

    A plan may split its runs into parts that write separate outputs, e.g.
        [parts.B]
        run_command = "python -I run.py --suite B --out results/B"
        [parts.B.resources]
        hours = 2
    Each part is submitted and tracked on its own (one unfinished job per part)."""
    exp = cfg.layout.experiments / eid
    meta, _ = read_frontmatter(exp / "plan.md")
    res = dict(meta.get("resources", {}))
    command = meta.get("run_command", "")
    if part:
        parts_def = meta.get("parts", {})
        if part not in parts_def:
            raise ValueError(f"{eid}: plan.md has no [parts.{part}] (known: {sorted(parts_def)})")
        res.update(parts_def[part].get("resources", {}))
        command = parts_def[part].get("run_command", "")
    # Resources may change after the plan is locked (the science may not): resources_override.toml
    # with a `reason` and [resources] / [parts.<p>.resources] tables. Seen: a replay part asked for
    # 32 GB, failed validation, and the locked plan blocked it for hours.
    override_problems: list[str] = []
    override_reason = ""
    ovf = exp / "resources_override.toml"
    if ovf.is_file():
        import tomllib
        try:
            ov = tomllib.loads(ovf.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            ov = {}
            override_problems.append(f"resources_override.toml is not valid TOML: {e}")
        override_reason = str(ov.get("reason", "")).strip()
        layer = dict(ov.get("resources", {}))
        if part:
            layer.update(ov.get("parts", {}).get(part, {}).get("resources", {}))
        bad = sorted(k for k in layer if k not in RESOURCE_KEYS)
        if bad:
            override_problems.append(f"resources_override.toml may change resources only, not {bad}")
        elif layer and not override_reason:
            override_problems.append("resources_override.toml needs a `reason`")
        else:
            res.update(layer)
    s = cfg["slurm"]
    out = {"partition": res.get("partition", s["partition"]),
           "qos": res.get("qos", s["qos"]),
           "cpus": int(res.get("cpus", 1)), "mem_gb": int(res.get("mem_gb", 4)),
           "hours": float(res.get("hours", 1)), "gpus": int(res.get("gpus", 0)),
           # MPI: ntasks ranks over `nodes` nodes, `cpus` per rank; mem_gb is per node (SLURM --mem)
           "nodes": int(res.get("nodes", 1)), "ntasks": int(res.get("ntasks", 1)),
           # job array: "0-25" or "0-25%8" (at most 8 at once); the command reads $SLURM_ARRAY_TASK_ID
           "array": str(res.get("array", "")).strip(),
           "command": command, "part": part}
    out["total_cpus"] = out["ntasks"] * out["cpus"]
    problems = list(override_problems)
    if override_reason:
        out["override"] = override_reason
    out["array_size"] = 1
    if out["array"]:
        try:
            out["array_size"] = _array_size(out["array"])
        except ValueError:
            problems.append(f"array = '{out['array']}': use a range like \"0-25\" or \"0-25%8\" or a list \"1,3,5\"")
    qos_table = cfg.site.get("qos", {})
    if qos_table:
        if out["qos"] not in qos_table:
            problems.append(f"qos '{out['qos']}' not in site profile {sorted(qos_table)}")
        elif qos_table[out["qos"]].get("max_hours") and out["hours"] > qos_table[out["qos"]]["max_hours"]:
            problems.append(f"hours={out['hours']} exceeds qos {out['qos']} limit {qos_table[out['qos']]['max_hours']}")
    # max_cpus = 0: no project cap, the scheduler and the account decide (human choice, 2026-10-16)
    for key, cap in (("total_cpus", "max_cpus"), ("mem_gb", "max_mem_gb"), ("hours", "max_hours"),
                     ("gpus", "max_gpus")):
        if (s[cap] or cap != "max_cpus") and out[key] > s[cap]:
            problems.append(f"{key}={out[key]} exceeds cap {cap}={s[cap]}")
    parts = cfg.site.get("partitions", {})
    if parts and out["partition"] not in parts:
        problems.append(f"partition '{out['partition']}' not in site profile {sorted(parts)}")
    per_node = parts.get(out["partition"], {}).get("cores") if parts else None
    if out["nodes"] < 1 or out["ntasks"] < out["nodes"]:
        problems.append(f"nodes={out['nodes']}, ntasks={out['ntasks']}: need 1 <= nodes <= ntasks")
    elif per_node and math.ceil(out["ntasks"] / out["nodes"]) * out["cpus"] > per_node:
        problems.append(f"{math.ceil(out['ntasks'] / out['nodes'])} tasks x {out['cpus']} cpus per node exceeds "
                        f"the {per_node} cores of a {out['partition']} node; add nodes")
    if not out["command"]:
        problems.append(f"plan.md has no run_command{f' for part {part}' if part else ''}")
    if not s["account"]:
        problems.append("slurm.account is empty in autolab.toml (see `slist` on the cluster)")
    # Sites that bill memory as cores (Negishi: ~1.95 GB per core) charge max(cpus, mem/mem_per_core).
    # Seen in turn 8: 8 cpus + 64 GB was allocated and billed as 33 cores.
    per_core = float(cfg.site.get("site", {}).get("mem_gb_per_core") or 0)
    mem_cores = out["nodes"] * math.ceil(out["mem_gb"] / per_core - 1e-9) if per_core else 0
    out["billed_cpus"] = max(out["total_cpus"], mem_cores)
    if out["billed_cpus"] > out["total_cpus"]:
        out["warning"] = (f"mem_gb={out['mem_gb']} is billed as {out['billed_cpus']} cores on this site "
                          f"({per_core} GB/core); request only the memory you need")
        # turn 10 submitted two 8-cpu jobs with 64 GB each (66 billed cores) without seeing the
        # warning, so extra memory now needs a stated reason in [resources] mem_reason
        if out["billed_cpus"] > 2 * out["total_cpus"] and not str(res.get("mem_reason", "")).strip():
            problems.append(out["warning"] + "; lower mem_gb or give the reason in [resources] mem_reason")
    if s["max_cpus"] and out["billed_cpus"] > s["max_cpus"]:
        problems.append(f"memory request is billed as {out['billed_cpus']} cores, above max_cpus={s['max_cpus']}")
    # scripts the command runs must exist (a plan named an analysis script nobody had written)
    for script in _SCRIPT.findall(command) + _SCRIPT.findall(str(meta.get("analysis_command", ""))):
        if not ((exp / script).is_file() or (cfg.layout.root / script).is_file()):
            problems.append(f"script '{script}' named in the plan does not exist (write and commit it first)")
    if out["total_cpus"] > 1 and not _PARALLEL_HINT.search(command) and not str(res.get("cpus_reason", "")).strip():
        out["warning_cpus"] = (f"{out['total_cpus']} cpus requested but the command shows no parallel launcher or "
                               "thread setting; request 1 cpu, or state why in [resources] cpus_reason "
                               "(e.g. reserved cores for timing isolation)")
        effs = [u["cpu_efficiency"] for u in read_jsonl(exp / "jobs_usage.jsonl")
                if u.get("cpu_efficiency") is not None and u.get("part") == part]
        if effs:
            out["warning_cpus"] += f"; earlier jobs of this part used {max(effs):.0%} of their cores at most"
    out["problems"] = problems
    return out


def _array_size(spec: str) -> int:
    body = spec.split("%")[0]
    n = 0
    for item in body.split(","):
        item = item.split(":")[0]                       # "0-30:2" steps are rare; count the range
        if "-" in item:
            a, b = (int(x) for x in item.split("-"))
            if b < a:
                raise ValueError(spec)
            n += b - a + 1
        else:
            int(item)
            n += 1
    return n


def render(cfg: Config, eid: str, part: str | None = None) -> tuple[Path, dict]:
    r = resources(cfg, eid, part)
    if r["problems"]:
        raise ValueError("; ".join(r["problems"]))
    exp = cfg.layout.experiments / eid
    site = cfg.site
    s = cfg["slurm"]
    logs = exp / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    hh = int(r["hours"])
    mm = int(round((r["hours"] - hh) * 60))
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=al-{eid}{'-' + part if part else ''}",
        f"#SBATCH --account={s['account']}",
        f"#SBATCH --partition={r['partition']}",
        f"#SBATCH --qos={r['qos']}",
        f"#SBATCH --nodes={r['nodes']}",
        f"#SBATCH --ntasks={r['ntasks']}",
        f"#SBATCH --cpus-per-task={r['cpus']}",
        f"#SBATCH --mem={r['mem_gb']}G",
        f"#SBATCH --time={hh:02d}:{mm:02d}:00",
        f"#SBATCH --output={logs.as_posix()}/" + ("%x-%A_%a.out" if r["array"] else "%x-%j.out"),
    ]
    if r["array"]:
        lines.append(f"#SBATCH --array={r['array']}")
    if r["gpus"]:
        lines.append(f"#SBATCH --gres=gpu:{r['gpus']}")
    lines += ["", "set -eo pipefail"]
    env = site.get("env", {})
    if s.get("conda_env") and env.get("conda_sh"):
        # conda activate.d scripts can read unset variables, so never activate under `set -u`
        lines += ["set +u", f"source {env['conda_sh']}", f"conda activate {s['conda_env']}"]
    lines += [
        f"export OMP_NUM_THREADS={r['cpus']}",
        'export TMPDIR="${TMPDIR:-/tmp}/$SLURM_JOB_ID"; mkdir -p "$TMPDIR"',
        f"cd {exp.as_posix()}",
        'echo "autolab: job $SLURM_JOB_ID on $(hostname) at $(date -Is)"',
        'git rev-parse HEAD 2>/dev/null | sed "s/^/autolab: commit /" || true',
    ]
    lines += list(env.get("preamble", [])) + list(site.get("preamble", []))
    lines += [r["command"], ""]
    path = exp / "slurm" / (f"job-{part}.sbatch" if part else "job.sbatch")
    write_atomic(path, "\n".join(lines))
    return path, r


def validate(cfg: Config, eid: str, part: str | None = None) -> dict:
    path, r = render(cfg, eid, part)
    proc = subprocess.run([_exe("sbatch"), "--test-only", str(path)], capture_output=True, text=True)
    # sbatch --test-only reports on stderr and exits 0 when the job would be accepted
    out = {"ok": proc.returncode == 0, "script": str(path), "billed_cpus": r["billed_cpus"],
           "core_hours_max": round(r["billed_cpus"] * r["hours"] * r["array_size"], 1),
           "output": (proc.stdout + proc.stderr).strip()}
    for k in ("warning", "warning_cpus", "override"):
        if r.get(k):
            out[k] = r[k]
    return out



_OUTPUT_DIRS = {"results", "logs", "slurm", "run", "pilot", "out", "output", "__pycache__", "analysis_out"}
_CODE_EXT = {".py", ".sh", ".c", ".h", ".cpp", ".hpp", ".cu", ".f90", ".f", ".jl", ".r", ".toml", ".yaml", ".yml",
             ".mk", ".cfg", ".ini"}


def uncommitted_code(root: Path, paths: list[str]) -> list[str] | None:
    """Code/config files under `paths` that are modified or untracked (outputs ignored), or None
    when the project is not a git repository."""
    if not (root / ".git").exists() or shutil.which("git") is None:
        return None
    existing = [p for p in paths if (root / p).exists()]
    if not existing:
        return []
    proc = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all", "--", *existing],
                          capture_output=True, text=True)
    out = []
    for line in proc.stdout.splitlines():
        f = line[3:].strip().strip('"').split(" -> ")[-1]
        p = Path(f)
        if set(p.parts) & _OUTPUT_DIRS:
            continue
        name = p.name.lower()
        if (p.suffix.lower() in _CODE_EXT or name in ("makefile", "plan.md")
                or (p.suffix.lower() == ".json" and "config" in f.lower())):
            out.append(f)
    return out


def clean_paths(cfg: Config, eid: str) -> list[str]:
    return [p.format(eid=eid) for p in cfg["slurm"]["clean_paths"]]


def submit(cfg: Config, eid: str, part: str | None = None) -> dict:
    if stopped(cfg):
        raise RuntimeError("00_human/STOP exists; not submitting")
    if not cfg.layout.ready.exists():
        raise RuntimeError("00_human/READY missing: the human has not finished the initial input")
    if not plan_gate_passed(cfg, eid):
        raise RuntimeError(f"{eid}: plan gate not passed (run `autolab gate plan {eid}`)")
    # one writer per experiment: a queued job and a second run both writing results/ corrupted
    # metrics.json in the first real run (E002)
    # (per part: parts write separate outputs and may run side by side)
    busy = [s["job"] for s in status(cfg, eid) if s["state"] not in TERMINAL and s.get("part") == part]
    if busy:
        what = f"{eid} part {part}" if part else eid
        raise RuntimeError(f"{what} already has unfinished job(s) {busy}; wait, or cancel with "
                           f"`autolab slurm cancel {eid}` before resubmitting. Runs that write separate "
                           "outputs can be declared as [parts.<name>] in plan.md and submitted with --part")
    dirty = uncommitted_code(cfg.layout.root, clean_paths(cfg, eid)) if cfg["slurm"]["require_clean_git"] else None
    if dirty:
        shown = " ".join(dirty[:12]) + (" ..." if len(dirty) > 12 else "")
        raise RuntimeError(f"uncommitted code or config files: {shown}. The job must run committed code: the agent "
                           f"that wrote them commits them from the project root (`git add {shown} && git commit -m "
                           f"\"{eid}: <what changed>\"`), then submits again. Results, logs and pilot outputs are "
                           "not checked.")
    path, r = render(cfg, eid, part)
    cap = float(cfg["slurm"].get("total_core_hours") or 0)
    if cap:
        used = core_hours_used(cfg)
        request = r["billed_cpus"] * r["hours"] * r["array_size"]
        if used + request > cap:
            raise RuntimeError(f"core-hour cap: used {used:.1f} + this job up to {request:.1f} > {cap:.0f} "
                               "(slurm.total_core_hours); ask the human to raise it")
    proc = subprocess.run([_exe("sbatch"), "--parsable", str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"sbatch failed: {proc.stderr.strip()}")
    job = proc.stdout.strip().split(";")[0]
    rec = {"job": job, "eid": eid, "part": part, "submitted": now_iso(), "script": str(path),
           "resources": {k: r[k] for k in ("partition", "qos", "nodes", "ntasks", "cpus", "total_cpus", "billed_cpus",
                                           "mem_gb", "hours", "gpus", "array", "array_size")}}
    if r.get("override"):
        rec["override"] = r["override"]
    append_jsonl(cfg.layout.experiments / eid / "jobs.jsonl", rec)
    for k in ("warning", "warning_cpus"):
        if r.get(k):
            rec = {**rec, k: r[k]}
    return rec


def core_hours_used(cfg: Config) -> float:
    """CPU time of every job this project submitted, from sacct (running jobs count so far)."""
    jobs = [r["job"] for e in sorted(cfg.layout.experiments.glob("E*"))
            for r in read_jsonl(e / "jobs.jsonl") if "submitted" in r]
    if not jobs:
        return 0.0
    proc = subprocess.run([_exe("sacct"), "-j", ",".join(jobs), "-X", "-n", "-P", "-o", "JobID,CPUTimeRAW"],
                          capture_output=True, text=True)
    total = 0.0
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) >= 2 and parts[1].isdigit():
            total += int(parts[1]) / 3600
    return round(total, 2)


def cancel(cfg: Config, eid: str, job: str | None = None) -> list[str]:
    """Cancel this experiment's own recorded jobs (all unfinished ones, or one by id)."""
    recorded = [r["job"] for r in read_jsonl(cfg.layout.experiments / eid / "jobs.jsonl")]
    targets = [job] if job else [j for j in recorded if job_state(j) not in TERMINAL]
    for j in targets:
        if j not in recorded:
            raise ValueError(f"job {j} was not submitted for {eid}; refusing to cancel it")
        proc = subprocess.run([_exe("scancel"), j], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"scancel {j} failed: {proc.stderr.strip()}")
        append_jsonl(cfg.layout.experiments / eid / "jobs.jsonl",
                      {"job": j, "eid": eid, "cancelled": now_iso()})
    return targets


def job_state(job: str) -> str:
    """State of a job; for a job array, one state for the whole array: RUNNING/PENDING while any
    task is unfinished, COMPLETED when all completed, else the first failure state."""
    proc = subprocess.run([_exe("sacct"), "-j", job, "-n", "-P", "-X", "-o", "State"],
                          capture_output=True, text=True)
    states = [ln.split()[0] for ln in proc.stdout.splitlines() if ln.strip()]
    if not states:
        q = subprocess.run([_exe("squeue"), "-j", job, "-h", "-o", "%T"], capture_output=True, text=True)
        states = [ln.strip() for ln in q.stdout.splitlines() if ln.strip()] or ["UNKNOWN"]
    if len(states) == 1:
        return states[0]
    unfinished = [s for s in states if s not in TERMINAL]
    if unfinished:
        return "RUNNING" if "RUNNING" in unfinished else unfinished[0]
    failed = [s for s in states if s != "COMPLETED"]
    return failed[0] if failed else "COMPLETED"


def _secs(t: str) -> float:
    """sacct times: [DD-][HH:]MM:SS[.mmm]"""
    if not t:
        return 0.0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [float(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]


def _gb(mem: str) -> float:
    if not mem:
        return 0.0
    mem = mem.rstrip("nc")                       # ReqMem may end in n (per node) or c (per cpu)
    unit = mem[-1].upper() if mem[-1].isalpha() else "K"
    num = float(mem[:-1] if mem[-1].isalpha() else mem)
    return num * {"K": 1 / 2**20, "M": 1 / 1024, "G": 1, "T": 1024}.get(unit, 1 / 2**20)


def usage(job: str) -> dict:
    """What a finished job really used (like `seff`), from sacct: CPU efficiency and peak memory,
    so the next request can be sized from data instead of guesses."""
    proc = subprocess.run([_exe("sacct"), "-j", job, "-n", "-P", "-o",
                           "JobID,ElapsedRaw,TotalCPU,AllocCPUS,MaxRSS,ReqMem"], capture_output=True, text=True)
    cpu_used = cpu_alloc = elapsed = 0.0
    rss = req = 0.0
    tasks = 0
    for ln in proc.stdout.splitlines():
        f = ln.strip().split("|")
        if len(f) < 6:
            continue
        jid, el, tcpu, ncpu, maxrss, reqmem = f[:6]
        if "." not in jid:                       # allocation line (one per array task)
            tasks += 1
            e = float(el or 0)
            elapsed = max(elapsed, e)
            cpu_alloc += e * float(ncpu or 0)
            cpu_used += _secs(tcpu)
            req = max(req, _gb(reqmem))
        else:
            rss = max(rss, _gb(maxrss))
    return {"tasks": tasks, "elapsed_h": round(elapsed / 3600, 3),
            "cpu_efficiency": round(cpu_used / cpu_alloc, 3) if cpu_alloc else None,
            "max_rss_gb": round(rss, 2), "req_mem_gb": round(req, 1)}


def record_usage(cfg: Config) -> list[str]:
    """Record usage once for every finished job; returns digest lines for the newly recorded ones."""
    lines = []
    for e in sorted(cfg.layout.experiments.glob("E*")):
        done = {r["job"] for r in read_jsonl(e / "jobs_usage.jsonl")}
        for rec in read_jsonl(e / "jobs.jsonl"):
            if "submitted" not in rec or rec["job"] in done:
                continue
            st = job_state(rec["job"])
            if st not in TERMINAL:
                continue
            u = usage(rec["job"])
            res = rec.get("resources", {})
            append_jsonl(e / "jobs_usage.jsonl", {"job": rec["job"], "part": rec.get("part"), "state": st,
                                                   "requested": res, **u, "at": now_iso()})
            eff = f"{u['cpu_efficiency']:.0%}" if u["cpu_efficiency"] is not None else "n/a"
            lines.append(f"{e.name}{'/' + rec['part'] if rec.get('part') else ''} job {rec['job']}: {st}, "
                         f"CPU efficiency {eff} of {res.get('total_cpus', res.get('cpus', '?'))} cpus, peak memory "
                         f"{u['max_rss_gb']} GB of {res.get('mem_gb', '?')} GB, {u['elapsed_h']} h of "
                         f"{res.get('hours', '?')} h requested")
    return lines


def status(cfg: Config, eid: str | None = None) -> list[dict]:
    exps = [cfg.layout.experiments / eid] if eid else sorted(cfg.layout.experiments.glob("E*"))
    out = []
    for e in exps:
        for rec in read_jsonl(e / "jobs.jsonl"):
            if "submitted" not in rec:          # cancellation records
                continue
            out.append({"eid": e.name, "job": rec["job"], "part": rec.get("part"),
                        "state": job_state(rec["job"])})
    return out
