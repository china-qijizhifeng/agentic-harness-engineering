#!/usr/bin/env python3
"""
Agentic Harness Engineering: Automated Evolution Evaluation System

Main loop: evaluate -> collect logs -> NexAU analyze & improve -> next iteration

Supports multiple config modes:
  1. Single file: --config agentic_harness_engineering_config.yaml
  2. Inherited overlay: --config configs/experiments/exp-001-gpt54.yaml  (with _base field)
  3. Batch parallel: --batch configs/experiments/
"""

import argparse
import collections
import concurrent.futures
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = Path(__file__).resolve().parent
EVOLVE_AGENT_DIR = PROJECT_DIR / "evolve_agent"
EXPERIMENTS_DIR = PROJECT_DIR / "experiments"

load_dotenv(PROJECT_DIR / ".env", override=True)

_ENV_KEYS = [
    "GITHUB_TOKEN", "E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN",
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_HOST",
    "SERPER_API_KEY",
]
print("[env] Loaded .env, current environment variables:")
for k in _ENV_KEYS:
    v = os.environ.get(k, "")
    if v:
        masked = v[:4] + "***" + v[-4:] if len(v) > 10 else "***"
        print(f"  {k}={masked}")
    else:
        print(f"  {k}=(not set)")


# ---------------------------------------------------------------------------
# Feishu Webhook Notification
# ---------------------------------------------------------------------------

def send_feishu_notification(config: dict, title: str, content: str) -> None:
    """Send notification via Feishu custom bot Webhook.

    Config requires:
      notify:
        feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx"
        enabled: true  (default true)
    """
    notify_cfg = config.get("notify", {})
    if not notify_cfg.get("enabled", True):
        return
    webhook_url = notify_cfg.get("feishu_webhook", "")
    if not webhook_url:
        return

    meta_name = config.get("_meta", {}).get("_name", "unknown")

    body = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"[Agentic Harness Engineering] {title}"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**Experiment**: {meta_name}\n\n{content}",
                },
            ],
        },
    }

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            if result.get("code") != 0:
                print(f"[notify] Feishu send failed: {result}")
            else:
                print(f"[notify] Feishu notification sent: {title}")
    except Exception as exc:
        print(f"[notify] Feishu notification error (does not affect experiment): {exc}")


# ---------------------------------------------------------------------------
# Config Loading: _base Inheritance + Deep Merge
# ---------------------------------------------------------------------------

def resolve_env_vars(obj):
    """Recursively resolve ${VAR_NAME} in config values with environment variables. Unmatched ones are kept as-is."""
    if isinstance(obj, str):
        return re.sub(r'\$\{([^}]+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), obj)
    if isinstance(obj, dict):
        return {k: resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_env_vars(item) for item in obj]
    return obj


def deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge two dicts, overlay overrides base."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(config_path: str) -> dict:
    """Load config file with _base inheritance chain support."""
    config_path = Path(config_path).resolve()
    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if "_base" not in raw:
        return resolve_env_vars(raw)

    base_ref = raw.pop("_base")
    base_path = (config_path.parent / base_ref).resolve()
    base = load_config(str(base_path))

    meta = {}
    for key in list(raw.keys()):
        if key.startswith("_"):
            meta[key] = raw.pop(key)

    config = deep_merge(base, raw)
    config["_meta"] = meta
    return resolve_env_vars(config)


def resolve_source_dir(config: dict) -> Path:
    """Resolve source_config_dir, supporting absolute and relative paths (relative to PROJECT_DIR)."""
    raw = config["source_config_dir"]
    p = Path(raw)
    if p.is_absolute():
        return p
    return (PROJECT_DIR / raw).resolve()


def apply_agent_yaml_patch(yaml_path: Path, patch: dict, label: str = "patch") -> None:
    """Deep merge a patch dict into the specified agent yaml file."""
    if not patch:
        return
    if not yaml_path.exists():
        print(f"[{label}] Warning: {yaml_path} does not exist, skipping patch")
        return
    with open(yaml_path, encoding="utf-8") as f:
        agent_config = yaml.safe_load(f)
    patched = deep_merge(agent_config, patch)
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(patched, f, default_flow_style=False, allow_unicode=True)
    print(f"[{label}] Applied patch to {yaml_path.name}: {list(patch.keys())}")


def apply_code_agent_patch(workspace_dir: Path, agent_config_filename: str, patch: dict) -> None:
    """Apply code_agent_patch to the agent config in workspace."""
    apply_agent_yaml_patch(workspace_dir / agent_config_filename, patch, label="code_agent_patch")


def build_evolve_agent_patch(evolve_agent_cfg: dict) -> dict:
    """Extract fields from evolve_agent config to patch into evolve_agent.yaml.
    Same format as code_agent_patch, passed through directly as target yaml structure."""
    return dict(evolve_agent_cfg)


def build_explore_agent_patch(config: dict) -> dict:
    """Extract patch from explore_agent_patch. If not explicitly specified, inherits api_type/reasoning/tool_call_mode from evolve_agent."""
    ml_patch = dict(config.get("explore_agent_patch", {}))
    if ml_patch:
        return ml_patch

    evolve_cfg = config.get("evolve_agent", {})
    if not evolve_cfg:
        return {}

    derived: dict = {}
    if "tool_call_mode" in evolve_cfg:
        derived["tool_call_mode"] = evolve_cfg["tool_call_mode"]

    evolve_llm = evolve_cfg.get("llm_config", {})
    llm_keys: dict = {}
    for key in ("api_type", "reasoning"):
        if key in evolve_llm:
            llm_keys[key] = evolve_llm[key]
    if llm_keys:
        derived["llm_config"] = llm_keys

    return derived


def get_llm_config(config: dict, role: str = "agent") -> dict:
    """Get LLM config for the specified role.
    role='agent': read from config.llm
    role='evolve': fields in evolve_agent.llm_config take priority, fallback to config.llm"""
    base_llm = config["llm"]
    if role == "evolve":
        evolve_llm = config.get("evolve_agent", {}).get("llm_config", {})
        return {
            "api_key": evolve_llm.get("api_key", base_llm["api_key"]),
            "base_url": evolve_llm.get("base_url", base_llm["base_url"]),
            "model": evolve_llm.get("model", base_llm["model"]),
        }
    return {
        "api_key": base_llm["api_key"],
        "base_url": base_llm["base_url"],
        "model": base_llm["model"],
    }


def set_llm_env(llm_cfg: dict) -> None:
    """Write LLM config to environment variables for ${env.LLM_*} references."""
    for cfg_key, env_key in [("api_key", "LLM_API_KEY"), ("base_url", "LLM_BASE_URL"), ("model", "LLM_MODEL")]:
        val = llm_cfg.get(cfg_key, "")
        if val:
            os.environ[env_key] = val


# ---------------------------------------------------------------------------
# Phase 0: Create Experiment Directory + Initialize Workspace
# ---------------------------------------------------------------------------

def create_experiment_dir(config: dict, config_path: str, experiment_name: str | None = None) -> Path:
    """Create a new experiment directory, save config snapshot and evolve agent config, return experiment dir path."""
    if experiment_name:
        exp_dir = EXPERIMENTS_DIR / experiment_name
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d__%H-%M-%S")
        meta_name = config.get("_meta", {}).get("_name", "")
        dir_name = f"{timestamp}__{meta_name}" if meta_name else timestamp
        exp_dir = EXPERIMENTS_DIR / dir_name

    exp_dir.mkdir(parents=True, exist_ok=True)

    # Save the merged complete config snapshot (not raw file copy)
    snapshot_path = exp_dir / "config_snapshot.yaml"
    if not snapshot_path.exists():
        snapshot = {k: v for k, v in config.items() if k != "_meta"}
        with open(snapshot_path, "w", encoding="utf-8") as f:
            yaml.dump(snapshot, f, default_flow_style=False, allow_unicode=True)
        print(f"[exp] Config snapshot saved")

    # If overlay config, also save the original overlay file
    if config.get("_meta"):
        overlay_dst = exp_dir / "experiment_overlay.yaml"
        if not overlay_dst.exists() and Path(config_path).exists():
            shutil.copy2(config_path, overlay_dst)

    # Copy evolve agent directory into exp_dir/evolve_agent/
    evolve_dst = exp_dir / "evolve_agent"
    if EVOLVE_AGENT_DIR.is_dir():
        if not evolve_dst.exists():
            shutil.copytree(EVOLVE_AGENT_DIR, evolve_dst)
        else:
            for item in EVOLVE_AGENT_DIR.iterdir():
                dst = evolve_dst / item.name
                if dst.exists():
                    continue
                if item.is_file():
                    shutil.copy2(item, dst)
                elif item.is_dir():
                    shutil.copytree(item, dst)

    (exp_dir / "runs").mkdir(exist_ok=True)

    print(f"[exp] Experiment directory: {exp_dir}")
    return exp_dir


def init_workspace(source_dir: Path, workspace_dir: Path) -> bool:
    """Copy from source config directory to workspace and git init. Returns whether a new initialization was performed."""
    if workspace_dir.exists() and (workspace_dir / ".git").exists():
        print(f"[init] Workspace already exists with git history, skipping initialization")
        return False

    print(f"[init] Initializing workspace from {source_dir} to {workspace_dir}")
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)

    shutil.copytree(source_dir, workspace_dir)

    subprocess.run(["git", "init"], cwd=workspace_dir, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "v0: baseline from " + source_dir.name],
        cwd=workspace_dir, check=True, capture_output=True,
    )
    print(f"[init] Workspace initialization complete, baseline committed")
    return True


# ---------------------------------------------------------------------------
# Phase 1: Run Evaluation
# ---------------------------------------------------------------------------

def find_latest_job_dir(jobs_root: Path) -> Path | None:
    """Find the latest job directory."""
    if not jobs_root.exists():
        return None
    job_dirs = [
        d for d in jobs_root.iterdir()
        if d.is_dir() and re.match(r"\d{4}-\d{2}-\d{2}__\d{2}-\d{2}-\d{2}", d.name)
    ]
    if not job_dirs:
        return None
    return max(job_dirs, key=lambda d: d.name)


class HarborJobTimeoutError(Exception):
    """Single harbor evaluation timeout."""
    pass


class ExperimentTimeoutError(Exception):
    """Total experiment duration timeout."""
    pass


def wait_for_job(jobs_root: Path, started_after: str | None,
                 poll_interval: int = 30, timeout_minutes: int = 0) -> Path:
    """Poll and wait for harbor job to complete, return job_dir. timeout_minutes <= 0 means no limit."""
    timeout_sec = timeout_minutes * 60 if timeout_minutes > 0 else 0
    t0 = time.monotonic()

    timeout_msg = f", timeout {timeout_minutes} min" if timeout_sec else ""
    print(f"[eval] Waiting for evaluation to complete, checking every {poll_interval}s{timeout_msg}...")

    while True:
        if timeout_sec and (time.monotonic() - t0) > timeout_sec:
            elapsed_min = (time.monotonic() - t0) / 60
            raise HarborJobTimeoutError(
                f"Harbor evaluation timeout: waited {elapsed_min:.1f} min, exceeded limit of {timeout_minutes} min"
            )

        job_dir = find_latest_job_dir(jobs_root)
        if job_dir is None:
            time.sleep(poll_interval)
            continue

        if started_after and job_dir.name <= started_after:
            time.sleep(poll_interval)
            continue

        result_path = job_dir / "result.json"
        if not result_path.exists():
            time.sleep(poll_interval)
            continue

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if result.get("finished_at") is not None:
                print(f"[eval] Evaluation complete: {job_dir.name}")
                return job_dir
        except (json.JSONDecodeError, KeyError):
            pass

        time.sleep(poll_interval)


def _build_harbor_cmd(config: dict, workspace_dir: Path, agent_config_filename: str,
                      iteration_dir: Path, n_concurrent_override: int | None = None) -> list[str]:
    """Build the harbor CLI command list."""
    harbor_cfg = config["harbor"]
    dataset = config.get("dataset")
    task_path = config.get("path")
    llm_cfg = get_llm_config(config, role="agent")
    model = llm_cfg["model"]

    config_path = (workspace_dir / agent_config_filename).resolve()
    k = int(harbor_cfg.get("k", 1))
    n_concurrent = n_concurrent_override or harbor_cfg["n_concurrent"]

    cmd = [
        "harbor", "run",
        "--agent", harbor_cfg["agent"],
        "--env", harbor_cfg["env"],
        "--model", model,
        "--n-concurrent", str(n_concurrent),
        "--ak", f"config_path={config_path}",
        "--jobs-dir", str(iteration_dir),
    ]

    if k > 1:
        cmd.extend(["-k", str(k)])

    if task_path:
        resolved = Path(task_path)
        if not resolved.is_absolute():
            resolved = (PROJECT_DIR / resolved).resolve()
        cmd.extend(["-p", str(resolved)])
    elif dataset:
        cmd.extend(["--dataset", dataset])
    else:
        raise ValueError("Config must specify either 'dataset' or 'path'")

    for tn in config.get("task_names", []):
        cmd.extend(["-t", tn])
    for xn in config.get("exclude_task_names", []):
        cmd.extend(["-x", xn])

    if harbor_cfg.get("force_build"):
        cmd.append("--force-build")

    return cmd


def launch_harbor(config: dict, workspace_dir: Path, agent_config_filename: str,
                  iteration_dir: Path, label: str = "",
                  n_concurrent_override: int | None = None) -> tuple[subprocess.Popen, str]:
    """Launch harbor evaluation process without waiting. Returns (proc, started_after).

    Use wait_for_harbor() to wait for completion and get the job_dir.
    Thread-safe: uses env= to pass LLM vars to the subprocess without
    modifying the process-global environment.
    """
    llm_cfg = get_llm_config(config, role="agent")

    sub_env = os.environ.copy()
    for cfg_key, env_key in [("api_key", "LLM_API_KEY"),
                             ("base_url", "LLM_BASE_URL"),
                             ("model", "LLM_MODEL")]:
        val = llm_cfg.get(cfg_key, "")
        if val:
            sub_env[env_key] = val

    e2b_sandbox_timeout = config["harbor"].get("e2b_sandbox_timeout")
    if e2b_sandbox_timeout is not None:
        sub_env["E2B_SANDBOX_TIMEOUT"] = str(int(e2b_sandbox_timeout))

    prev_latest = find_latest_job_dir(iteration_dir)
    started_after = prev_latest.name if prev_latest else ""

    cmd = _build_harbor_cmd(config, workspace_dir, agent_config_filename,
                            iteration_dir, n_concurrent_override=n_concurrent_override)

    tag = f" [{label}]" if label else ""
    print(f"[eval{tag}] Starting evaluation: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT_DIR,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=sub_env,
    )
    return proc, started_after


def wait_for_harbor(proc: subprocess.Popen, iteration_dir: Path,
                    started_after: str, timeout_minutes: int = 0,
                    label: str = "") -> Path:
    """Wait for a previously launched harbor process to finish and return its job_dir."""
    tag = f" [{label}]" if label else ""
    try:
        job_dir = wait_for_job(iteration_dir, started_after or None, timeout_minutes=timeout_minutes)
    except HarborJobTimeoutError:
        print(f"[eval{tag}] Harbor evaluation timeout ({timeout_minutes} min), terminating...")
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        job_dir = find_latest_job_dir(iteration_dir)
        if job_dir is None:
            raise HarborJobTimeoutError(f"Harbor evaluation timeout with no completed job directory{tag}")
        print(f"[eval{tag}] Using existing results after timeout: {job_dir.name}")
        return job_dir

    if proc.poll() is None:
        print(f"[eval{tag}] Harbor process still running, waiting for exit...")
        proc.wait()

    return job_dir


def run_harbor(config: dict, workspace_dir: Path, agent_config_filename: str,
               iteration_dir: Path) -> Path:
    """Start harbor evaluation and wait for completion. Results are written directly to iteration_dir."""
    job_timeout = int(config.get("harbor_job_timeout_minutes") or 0)
    proc, started_after = launch_harbor(config, workspace_dir, agent_config_filename, iteration_dir)
    return wait_for_harbor(proc, iteration_dir, started_after, timeout_minutes=job_timeout)


# ---------------------------------------------------------------------------
# Phase 2: Compute Evaluation Results
# ---------------------------------------------------------------------------

_EXCEPTION_LINE_RE = re.compile(
    r"^([a-zA-Z_][\w.]*(?:Error|Exception|Timeout|Fault))\b"
)


def _extract_exception_type(exc_text: str) -> str:
    """Extract Python exception class name from exception.txt content.

    Strategy:
    1. Prefer regex matching standard exception names (ending with Error/Exception/Timeout/Fault)
    2. Fallback to "module.ClassName: message" format, requiring class name >= 4 chars (to exclude apt 'E:' etc.)
    Both passes scan from the last line upward.
    """
    lines = exc_text.strip().splitlines()
    # First pass: exact match for standard exception naming
    for line in reversed(lines):
        stripped = line.strip()
        m = _EXCEPTION_LINE_RE.match(stripped)
        if m:
            full_name = m.group(1)
            return full_name.rsplit(".", 1)[-1]
    # Second pass: loose match for "ClassName: message" format
    for line in reversed(lines):
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith(" "):
            candidate = stripped.split(":")[0].strip()
            parts = candidate.rsplit(".", 1)
            short = parts[-1] if len(parts) > 1 else parts[0]
            if short and len(short) >= 4 and short[0].isupper() and short.isidentifier():
                return short
    return "Unknown"


def pass_at_k_est(n: int, c: int, k: int) -> float:
    """Chen et al. unbiased estimator for single-task pass@k.
    n = total samples, c = number of correct samples."""
    if k > n or k < 0 or n == 0:
        return float("nan")
    num_wrong = n - c
    if num_wrong >= k:
        return 1.0 - math.comb(num_wrong, k) / math.comb(n, k)
    return 1.0 if c > 0 else 0.0


def compute_pass_at_k_metrics(per_task_rollouts: dict, k: int) -> dict:
    """Compute pass@1, pass@2, ..., pass@k aggregate metrics using Chen et al. estimator.

    Returns dict:
      pass_at: {1: rate, 2: rate, ..., k: rate}  - macro-averaged over eligible tasks (n>=i)
      per_task_pass_at: {task_name: {1: est, 2: est, ...}}  - per-task estimates
      eligible_counts: {1: n_tasks, 2: n_tasks, ...}  - tasks with n>=i for each i
    """
    pass_at: dict[int, float] = {}
    per_task_pass_at: dict[str, dict[int, float]] = {}
    eligible_counts: dict[int, int] = {}

    for i in range(1, k + 1):
        estimates = []
        for task_name, ro in sorted(per_task_rollouts.items()):
            n = ro["n_pass"] + ro["n_fail"] + ro.get("n_exception", 0)
            c = ro["n_pass"]
            if n >= i:
                est = pass_at_k_est(n, c, i)
                estimates.append(est)
                per_task_pass_at.setdefault(task_name, {})[i] = est
        eligible_counts[i] = len(estimates)
        pass_at[i] = sum(estimates) / len(estimates) if estimates else 0.0

    return {
        "pass_at": pass_at,
        "per_task_pass_at": per_task_pass_at,
        "eligible_counts": eligible_counts,
    }


def compute_stats(job_dir: Path, k: int = 1) -> dict:
    """Compute detailed evaluation statistics from harbor raw job_dir.

    When k>1, groups trials by task name: a task passes only if ALL k rollouts pass.

    Returns dict:
      pass_rate:       Pass rate over all tasks; when k>1, this is pass@1 (Chen et al.)
      n_pass:          Number of tasks that pass (all rollouts pass when k>1)
      n_fail:          Number of tasks that fail
      n_exception:     Number of tasks with all-exception results
      n_total:         Number of unique tasks
      k:               Rollout count per task
      exception_types: dict[str, int] - Exception type distribution
      task_results:    dict[task_name, "pass"|"fail"|"exception"] - Per-task results
      per_task_rollouts: dict[task_name, {n_pass, n_fail, n_exception, total}] - Per-task rollout detail (when k>1)
      trial_stats:     dict with raw per-trial counts (when k>1)
    """
    trial_dirs = [
        d for d in job_dir.iterdir()
        if d.is_dir() and (d / "result.json").exists()
    ]

    # Collect per-trial results grouped by task name
    task_trials: dict[str, list[str]] = {}
    exception_types: dict[str, int] = {}
    timeout_trial_counts: dict[str, int] = {}

    for trial_dir in sorted(trial_dirs):
        task_name = re.sub(r"__[A-Za-z0-9]{6,}$", "", trial_dir.name)
        reward_src = trial_dir / "verifier" / "reward.txt"
        exception_src = trial_dir / "exception.txt"

        if reward_src.exists():
            try:
                reward_val = float(reward_src.read_text().strip())
                result = "pass" if reward_val >= 1.0 else "fail"
            except ValueError:
                result = "fail"
        else:
            result = "exception"
            if exception_src.exists():
                exc_text = exception_src.read_text(errors="replace").strip()
                exc_type = _extract_exception_type(exc_text)
                exception_types[exc_type] = exception_types.get(exc_type, 0) + 1
                if "Timeout" in exc_type:
                    timeout_trial_counts[task_name] = timeout_trial_counts.get(task_name, 0) + 1
            else:
                exception_types["Unknown"] = exception_types.get("Unknown", 0) + 1

        task_trials.setdefault(task_name, []).append(result)

    # Aggregate per-task results (all rollouts must pass when k>1)
    task_results: dict[str, str] = {}
    per_task_rollouts: dict[str, dict] = {}
    trial_n_pass = 0
    trial_n_fail = 0
    trial_n_exception = 0

    for task_name, trials in sorted(task_trials.items()):
        tp = trials.count("pass")
        tf = trials.count("fail")
        te = trials.count("exception")
        trial_n_pass += tp
        trial_n_fail += tf
        trial_n_exception += te

        if tp == len(trials):
            task_results[task_name] = "pass"
        elif te == len(trials):
            task_results[task_name] = "exception"
        else:
            task_results[task_name] = "fail"

        if k > 1:
            per_task_rollouts[task_name] = {
                "n_pass": tp,
                "n_fail": tf,
                "n_exception": te,
                "total": len(trials),
            }

    n_pass = sum(1 for r in task_results.values() if r == "pass")
    n_fail = sum(1 for r in task_results.values() if r == "fail")
    n_exception = sum(1 for r in task_results.values() if r == "exception")
    n_total = len(task_results)

    pass_rate = n_pass / n_total if n_total > 0 else 0.0

    n_trials_total = trial_n_pass + trial_n_fail + trial_n_exception

    # Compute pass@k metrics (Chen et al. estimator)
    pass_at_k_metrics: dict | None = None
    if k > 1:
        pass_at_k_metrics = compute_pass_at_k_metrics(per_task_rollouts, k)

        # Use pass@1 as primary metric
        pass_rate = pass_at_k_metrics['pass_at'].get(1, 0.0)

        trial_pass_rate = trial_n_pass / n_trials_total if n_trials_total > 0 else 0.0
        pass_at_parts = " | ".join(f"pass@{i}={pass_at_k_metrics['pass_at'][i]:.1%}" for i in range(1, k + 1))
        print(f"[stats] {n_trials_total} trials across {n_total} tasks (k={k})")
        print(f"[stats] Per-trial: {trial_n_pass} pass, {trial_n_fail} fail, {trial_n_exception} exception (trial pass rate: {trial_pass_rate:.1%})")
        print(f"[stats] {pass_at_parts}")
    else:
        print(f"[stats] {n_total} tasks: {n_pass} pass, {n_fail} fail, {n_exception} exception")
        print(f"[stats] Pass rate: {pass_rate:.1%}")

    if exception_types:
        print(f"[stats] Exception types: {exception_types}")

    timeout_tasks = set(timeout_trial_counts.keys())
    if timeout_tasks:
        print(f"[stats] Timeout tasks ({len(timeout_tasks)}): {', '.join(sorted(timeout_tasks))}")

    result = {
        "pass_rate": pass_rate,
        "n_pass": n_pass,
        "n_fail": n_fail,
        "n_exception": n_exception,
        "n_total": n_total,
        "k": k,
        "exception_types": exception_types,
        "task_results": task_results,
        "timeout_tasks": timeout_tasks,
    }

    if k > 1:
        result["per_task_rollouts"] = per_task_rollouts
        result["pass_at_k"] = pass_at_k_metrics
        result["trial_stats"] = {
            "n_pass": trial_n_pass,
            "n_fail": trial_n_fail,
            "n_exception": trial_n_exception,
            "n_total": n_trials_total,
            "trial_pass_rate": trial_n_pass / n_trials_total if n_trials_total > 0 else 0.0,
        }

    return result


# ---------------------------------------------------------------------------
# Phase 2.4: Task Stability Tracking (Cross-Iteration)
# ---------------------------------------------------------------------------

def load_task_history(exp_dir: Path) -> dict:
    """Load cross-iteration task result history.

    Returns dict:
      task_name -> list[(iteration, "pass"|"fail"|"exception")]
    """
    history_path = exp_dir / "task_history.json"
    if history_path.exists():
        return json.loads(history_path.read_text(encoding="utf-8"))
    return {}


def save_task_history(exp_dir: Path, history: dict) -> None:
    """Persist task result history."""
    history_path = exp_dir / "task_history.json"
    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def update_task_history(exp_dir: Path, iteration: int, task_results: dict,
                        per_task_rollouts: dict | None = None) -> dict:
    """Update task history with current iteration results, return updated full history.

    Each entry is [iteration, result] or [iteration, result, rollout_info] when k>1.
    rollout_info = {n_pass, n_fail, n_exception, total}.
    """
    history = load_task_history(exp_dir)
    for task_name, result in task_results.items():
        if task_name not in history:
            history[task_name] = []
        existing_iters = {entry[0] for entry in history[task_name]}
        if iteration not in existing_iters:
            if per_task_rollouts and task_name in per_task_rollouts:
                history[task_name].append([iteration, result, per_task_rollouts[task_name]])
            else:
                history[task_name].append([iteration, result])
    save_task_history(exp_dir, history)
    return history


def compute_task_stability(task_history: dict, min_iterations: int = 3) -> dict:
    """Compute stability classification for each task based on historical data.

    Returns dict:
      stable_pass:      list[task_name] - Passed in all iterations that reached verifier
      stable_fail:      list[task_name] - Failed in all iterations that reached verifier
      unstable:         list[task_name] - Flipped between pass and fail (>=min_iterations data points)
      possibly_unstable: list[task_name] - Has both pass and fail but insufficient data (<min_iterations)
      infra_only:       list[task_name] - All exceptions, never reached verifier
    """
    stable_pass = []
    stable_fail = []
    unstable = []
    possibly_unstable = []
    infra_only = []

    for task_name, entries in task_history.items():
        verdicts = [e[1] for e in entries if e[1] in ("pass", "fail")]
        exceptions = [e[1] for e in entries if e[1] == "exception"]

        if not verdicts:
            if exceptions:
                infra_only.append(task_name)
            continue

        has_pass = "pass" in verdicts
        has_fail = "fail" in verdicts

        if has_pass and has_fail:
            if len(verdicts) >= min_iterations:
                unstable.append(task_name)
            else:
                possibly_unstable.append(task_name)
        elif has_pass and not has_fail:
            stable_pass.append(task_name)
        elif has_fail and not has_pass:
            stable_fail.append(task_name)

    return {
        "stable_pass": sorted(stable_pass),
        "stable_fail": sorted(stable_fail),
        "unstable": sorted(unstable),
        "possibly_unstable": sorted(possibly_unstable),
        "infra_only": sorted(infra_only),
    }


def compute_iteration_diff(current_results: dict, prev_results: dict | None,
                           current_rollouts: dict | None = None,
                           prev_rollouts: dict | None = None) -> dict | None:
    """Compare current and previous iteration task results, compute the full 9-state transition matrix.

    When rollout data is available (k>1), also subdivides stable_fail into
    rollout_improved / rollout_regressed / rollout_unchanged, and annotates
    each task with (prev_pass_count, cur_pass_count, k) in rollout_details.
    """
    if prev_results is None:
        return None

    flipped = []           # fail -> pass (real capability improvement)
    regressed = []         # pass -> fail (real capability regression)
    infra_recovered = []   # exception -> pass (infra recovery)
    infra_lost = []        # pass -> exception (infra failure)
    stable_pass = []       # pass -> pass (consistently passing)
    stable_fail = []       # fail -> fail (consistently failing)
    exception_to_fail = [] # exception -> fail (infra recovered but task still fails, new optimization target)
    fail_to_exception = [] # fail -> exception (infra failure coverage)
    exception_stable = []  # exception -> exception (persistent infra error)

    rollout_improved = []  # fail -> fail but more rollouts passing
    rollout_regressed = [] # fail -> fail but fewer rollouts passing
    rollout_unchanged = [] # fail -> fail with same rollout pass count
    rollout_details = {}   # task -> (prev_n_pass, cur_n_pass, total)

    has_rollouts = bool(current_rollouts and prev_rollouts)

    all_tasks = set(current_results) | set(prev_results)
    for task in sorted(all_tasks):
        cur = current_results.get(task, "exception")
        prev = prev_results.get(task, "exception")

        if has_rollouts and (task in current_rollouts or task in prev_rollouts):
            cur_ro = current_rollouts.get(task, {})
            prev_ro = prev_rollouts.get(task, {})
            cur_np = cur_ro.get("n_pass", 0)
            prev_np = prev_ro.get("n_pass", 0)
            total = cur_ro.get("total", prev_ro.get("total", 0))
            rollout_details[task] = (prev_np, cur_np, total)

        if prev == cur == "pass":
            stable_pass.append(task)
        elif prev == cur == "fail":
            if has_rollouts and task in rollout_details:
                prev_np, cur_np, _ = rollout_details[task]
                if cur_np > prev_np:
                    rollout_improved.append(task)
                elif cur_np < prev_np:
                    rollout_regressed.append(task)
                else:
                    rollout_unchanged.append(task)
            stable_fail.append(task)
        elif prev == cur == "exception":
            exception_stable.append(task)
        elif prev == "fail" and cur == "pass":
            flipped.append(task)
        elif prev == "pass" and cur == "fail":
            regressed.append(task)
        elif prev == "exception" and cur == "pass":
            infra_recovered.append(task)
        elif prev == "pass" and cur == "exception":
            infra_lost.append(task)
        elif prev == "exception" and cur == "fail":
            exception_to_fail.append(task)
        elif prev == "fail" and cur == "exception":
            fail_to_exception.append(task)

    return {
        "flipped": flipped,
        "regressed": regressed,
        "net": len(flipped) - len(regressed),
        "infra_recovered": infra_recovered,
        "infra_lost": infra_lost,
        "stable_pass": stable_pass,
        "stable_fail": stable_fail,
        "exception_to_fail": exception_to_fail,
        "fail_to_exception": fail_to_exception,
        "exception_stable": exception_stable,
        "rollout_improved": rollout_improved,
        "rollout_regressed": rollout_regressed,
        "rollout_unchanged": rollout_unchanged,
        "rollout_details": rollout_details,
    }


# ---------------------------------------------------------------------------
# Phase 2.3.5: Trajectory Info Pre-extraction (reduce evolve agent redundant file reads)
# ---------------------------------------------------------------------------

_TRIAL_SUFFIX_RE = re.compile(r"__[A-Za-z0-9]{6,}$")


def _find_trial_dir(job_dir: Path, task_name: str) -> Path | None:
    """Find the corresponding trial directory by task name."""
    for d in job_dir.iterdir():
        if d.is_dir() and _TRIAL_SUFFIX_RE.sub("", d.name) == task_name:
            return d
    return None


def _truncate(s: str, max_len: int = 200) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _extract_error_type_from_trace(trace: str) -> str:
    """Extract error type name from pytest traceback."""
    if not trace:
        return "Unknown"
    for line in reversed(trace.strip().splitlines()):
        stripped = line.strip()
        if stripped.startswith("E "):
            stripped = stripped[2:].strip()
        m = re.match(r"^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout))\b", stripped)
        if m:
            return m.group(1).rsplit(".", 1)[-1]
    return "Unknown"


def _extract_error_detail(trace: str, max_len: int = 300) -> str:
    """Extract key error lines from pytest trace (assertion details starting with E)."""
    if not trace:
        return ""
    e_lines = []
    for line in trace.strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("E "):
            e_lines.append(stripped[2:].strip())
    if e_lines:
        return _truncate("\n".join(e_lines[:3]), max_len)
    lines = trace.strip().splitlines()
    return _truncate(lines[-1].strip(), max_len) if lines else ""


def extract_verifier_failures(job_dir: Path, task_results: dict,
                              max_detail: int = 30) -> dict:
    """Extract verifier errors for failed tasks from ctrf.json and cluster by error type.

    Returns:
      per_task:  {task_name: {test_name, error_type, error_detail}}
      clusters:  {error_type: [task_names]}
    """
    failed_tasks = [t for t, r in task_results.items() if r == "fail"]
    if not failed_tasks:
        return {"per_task": {}, "clusters": {}}

    per_task: dict[str, dict] = {}
    clusters: dict[str, list[str]] = {}

    for task_name in sorted(failed_tasks)[:max_detail]:
        trial_dir = _find_trial_dir(job_dir, task_name)
        if not trial_dir:
            continue

        ctrf_path = trial_dir / "verifier" / "ctrf.json"
        if not ctrf_path.exists():
            continue

        try:
            ctrf = json.loads(ctrf_path.read_text(encoding="utf-8"))
            tests = ctrf.get("results", {}).get("tests", [])
            failed_tests = [t for t in tests if t.get("status") == "failed"]
            if not failed_tests:
                continue

            ft = failed_tests[0]
            trace = ft.get("trace", "")
            message = ft.get("message", "")
            error_type = _extract_error_type_from_trace(trace)
            error_detail = _extract_error_detail(trace) or _truncate(message, 200)

            per_task[task_name] = {
                "test_name": ft.get("name", ""),
                "error_type": error_type,
                "error_detail": error_detail,
            }
            clusters.setdefault(error_type, []).append(task_name)
        except (json.JSONDecodeError, KeyError, IndexError):
            continue

    return {"per_task": per_task, "clusters": clusters}


def extract_agent_behavior_stats(job_dir: Path, task_results: dict) -> dict:
    """Extract agent behavior statistics from nexau_in_memory_tracer.cleaned.json.

    Returns:
      per_task:              {task_name: {result, n_llm_calls, n_tool_calls, tool_usage, ...}}
      comparison:            Average comparison between pass vs fail groups
      possibly_hit_max_turns: List of tasks that possibly hit the max turns limit
    """
    per_task: dict[str, dict] = {}

    for task_name, result in sorted(task_results.items()):
        if result == "exception":
            continue
        trial_dir = _find_trial_dir(job_dir, task_name)
        if not trial_dir:
            continue

        tracer_path = trial_dir / "agent" / "nexau_in_memory_tracer.cleaned.json"
        if not tracer_path.exists():
            continue

        try:
            spans = json.loads(tracer_path.read_text(encoding="utf-8"))
            root = spans[0] if spans else {}

            n_llm = 0
            n_tool = 0
            tool_usage: dict[str, int] = {}
            n_tool_errors = 0
            last_tool = ""

            for child in root.get("children", []):
                ctype = child.get("type", "")
                if ctype == "LLM":
                    n_llm += 1
                elif ctype == "TOOL":
                    n_tool += 1
                    tname = child.get("name", "unknown")
                    tool_usage[tname] = tool_usage.get(tname, 0) + 1
                    if child.get("error"):
                        n_tool_errors += 1
                    last_tool = tname

            per_task[task_name] = {
                "result": result,
                "n_llm_calls": n_llm,
                "n_tool_calls": n_tool,
                "tool_usage": tool_usage,
                "n_tool_errors": n_tool_errors,
                "duration_s": round((root.get("duration_ms") or 0) / 1000, 1),
                "last_tool": last_tool,
            }
        except (json.JSONDecodeError, KeyError, IndexError):
            continue

    pass_stats = [s for s in per_task.values() if s["result"] == "pass"]
    fail_stats = [s for s in per_task.values() if s["result"] == "fail"]

    def _avg(items: list[dict], key: str) -> float:
        vals = [i[key] for i in items]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    fail_llm = [s["n_llm_calls"] for s in per_task.values() if s["result"] != "pass"]
    fail_max_llm = max(fail_llm) if fail_llm else 0
    hit_max = [t for t, s in per_task.items()
               if s["result"] != "pass" and s["n_llm_calls"] >= fail_max_llm and fail_max_llm > 5]

    return {
        "per_task": per_task,
        "comparison": {
            "pass_avg_llm_calls": _avg(pass_stats, "n_llm_calls"),
            "pass_avg_tool_calls": _avg(pass_stats, "n_tool_calls"),
            "pass_avg_duration_s": _avg(pass_stats, "duration_s"),
            "fail_avg_llm_calls": _avg(fail_stats, "n_llm_calls"),
            "fail_avg_tool_calls": _avg(fail_stats, "n_tool_calls"),
            "fail_avg_duration_s": _avg(fail_stats, "duration_s"),
            "n_pass": len(pass_stats),
            "n_fail": len(fail_stats),
        },
        "possibly_hit_max_turns": hit_max,
    }


# ---------------------------------------------------------------------------
# Phase 2.5a: Agent Debugger — parallel QA analysis via `adb ask`
# ---------------------------------------------------------------------------

DEFAULT_DEBUG_QUERY = (
    "This task has {n_total} rollouts: {n_pass} passed, {n_fail} failed.\n"
    "{trace_labels}\n"
    "All traces are provided. Analyze why the failing attempts failed.\n\n"
    "IMPORTANT: If verifier test output is provided below, it shows the REAL external test results "
    "that determined pass/fail. The agent never sees this output. Cross-reference the verifier's "
    "actual failure messages with the agent's trace to find the TRUE root cause.\n\n"
    "Identify:\n"
    "1. ROOT CAUSE: What is the fundamental reason for the failures? "
    "Cross-reference with verifier output if available.\n"
    "2. PASS vs FAIL: If both passing and failing traces exist, what did successful attempts do differently?\n"
    "3. CRITICAL MISTAKE: At which point did the failing attempts go wrong?\n"
    "4. GENERAL MECHANISM: What structural mechanism (NOT task-specific knowledge) would prevent this class of failure?\n\n"
    "Focus on general patterns. Keep concise (under 300 words)."
)

DEFAULT_SUMMARY_QUERY = (
    "This task passed all {n_total} rollouts.\n"
    "{trace_labels}\n"
    "Analyze one representative trace.\n\n"
    "Identify:\n"
    "1. KEY STRATEGY: What was the agent's approach and why did it work?\n"
    "2. REUSABLE PATTERN: What general behavioral pattern could benefit other tasks?\n"
    "3. FRAGILITY RISK: Anything that seems fragile or lucky?\n\n"
    "Keep concise (under 150 words)."
)

DEFAULT_DEBUG_QUERY_K1 = (
    "This task has a single rollout which FAILED.\n"
    "{trace_labels}\n"
    "Analyze the trace carefully and locate where the problem is.\n\n"
    "IMPORTANT: If verifier test output is provided below, it shows the REAL external test results "
    "that determined pass/fail. The agent never sees this output. Cross-reference the verifier's "
    "actual failure messages with the agent's trace to find the TRUE root cause — the agent may "
    "have believed it succeeded when the external test shows a different failure.\n\n"
    "Identify:\n"
    "1. FAILURE POINT: At which exact step did things start going wrong? "
    "Cross-reference with verifier output if available.\n"
    "2. ROOT CAUSE: What is the fundamental reason for the failure? "
    "Distinguish between 'agent thought it succeeded but verifier disagrees' vs 'agent encountered errors'.\n"
    "3. WHAT SHOULD HAVE BEEN DONE: What would the correct approach look like at the failure point?\n"
    "4. GENERAL LESSON: What structural mechanism (NOT task-specific knowledge) would prevent this class of failure?\n\n"
    "Focus on pinpointing the problem. Keep concise (under 300 words)."
)

DEFAULT_SUMMARY_QUERY_K1 = (
    "This task has a single rollout which PASSED.\n"
    "{trace_labels}\n"
    "Analyze the trace and summarize the success experience.\n\n"
    "Identify:\n"
    "1. KEY STRATEGY: What was the agent's approach and why did it succeed?\n"
    "2. SUCCESS FACTORS: What specific decisions or behaviors were critical to the success?\n"
    "3. REUSABLE PATTERN: What general behavioral pattern from this success could benefit other tasks?\n\n"
    "Focus on extracting reusable lessons. Keep concise (under 150 words)."
)


@dataclass
class TaskAnalysisJob:
    task_name: str
    trace_paths: list[Path] = field(default_factory=list)
    trace_rewards: list[float] = field(default_factory=list)
    trial_dirs: list[Path] = field(default_factory=list)
    verifier_outputs: list[str] = field(default_factory=list)
    n_pass: int = 0
    n_fail: int = 0
    n_timeout: int = 0
    is_timeout: bool = False
    mode: str = "debug"  # "debug" | "summary"
    trace_type: str | None = None  # None → default; "in_memory_tracer" for raw dumps


_adb_path: str | None = None


def _find_adb() -> str | None:
    """Locate the adb executable, checking common pip script directories."""
    found = shutil.which("adb")
    if found:
        return found
    for candidate_dir in [
        Path(sys.executable).parent,  # venv/bin or /usr/bin
        Path.home() / ".local" / "bin",
    ]:
        candidate = candidate_dir / "adb"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _ensure_adb_installed() -> bool:
    """Install adb wheel if not already available. Returns True on success."""
    global _adb_path
    _adb_path = _find_adb()
    if _adb_path:
        return True
    whl = EVOLVE_AGENT_DIR / "skills" / "agent-debugger-cli" / "agent_debugger_core-0.0.0-py3-none-any.whl"
    if not whl.exists():
        print(f"[adb] wheel not found: {whl}")
        return False
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", str(whl)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"[adb] pip install failed: {r.stderr[:300]}")
        return False
    _adb_path = _find_adb()
    if _adb_path:
        print(f"[adb] installed successfully: {_adb_path}")
    return _adb_path is not None


def _build_adb_jobs(
    task_results: dict[str, str],
    job_dir: Path,
    config: dict,
    timeout_tasks: set[str] | None = None,
) -> list[TaskAnalysisJob]:
    """Build one TaskAnalysisJob per task by scanning trial directories."""
    max_tasks = config.get("max_tasks", 30)
    jobs: list[TaskAnalysisJob] = []
    _timeout_tasks = timeout_tasks or set()

    for task_name, result in task_results.items():
        is_timeout_task = task_name in _timeout_tasks
        if result == "exception" and not is_timeout_task:
            continue

        trial_dirs: list[tuple[Path, float]] = []  # (dir, reward); reward=-1 for timeout

        for d in sorted(job_dir.iterdir()):
            if not d.is_dir():
                continue
            base = _TRIAL_SUFFIX_RE.sub("", d.name)
            if base != task_name:
                continue
            reward_path = d / "verifier" / "reward.txt"
            if reward_path.exists():
                try:
                    reward_val = float(reward_path.read_text().strip())
                except ValueError:
                    reward_val = 0.0
                trial_dirs.append((d, reward_val))
            elif is_timeout_task:
                trial_dirs.append((d, -1.0))

        if not trial_dirs:
            continue

        # Prefer .cleaned.json; if any trial lacks it, fall back to raw for all
        # (--trace-type is global per adb call, can't mix cleaned and raw).
        all_have_cleaned = all(
            (td / "agent" / "nexau_in_memory_tracer.cleaned.json").exists()
            for td, _ in trial_dirs
        )
        if all_have_cleaned:
            trace_filename = "nexau_in_memory_tracer.cleaned.json"
            trace_type = None
        else:
            trace_filename = "nexau_in_memory_tracer.json"
            trace_type = "in_memory_tracer"

        traces: list[Path] = []
        rewards: list[float] = []
        collected_trial_dirs: list[Path] = []
        verifier_outputs: list[str] = []
        n_pass = n_fail = n_timeout = 0
        for d, reward_val in trial_dirs:
            history = d / "agent" / trace_filename
            if not history.exists():
                continue
            traces.append(history)
            rewards.append(reward_val)
            collected_trial_dirs.append(d)
            # Collect verifier test output (truncated)
            test_stdout = d / "verifier" / "test-stdout.txt"
            if test_stdout.exists():
                try:
                    text = test_stdout.read_text(errors="replace").strip()
                    if len(text) > 4000:
                        text = "... (truncated) ...\n" + text[-4000:]
                    verifier_outputs.append(text)
                except OSError:
                    verifier_outputs.append("")
            else:
                verifier_outputs.append("")
            if reward_val >= 1.0:
                n_pass += 1
            elif reward_val < 0:
                n_timeout += 1
            else:
                n_fail += 1

        if not traces:
            continue

        jobs.append(TaskAnalysisJob(
            task_name=task_name,
            trace_paths=traces,
            trace_rewards=rewards,
            trial_dirs=collected_trial_dirs,
            verifier_outputs=verifier_outputs,
            n_pass=n_pass,
            n_fail=n_fail,
            n_timeout=n_timeout,
            is_timeout=is_timeout_task,
            mode="debug" if (n_fail > 0 or n_timeout > 0) else "summary",
            trace_type=trace_type,
        ))

    jobs.sort(key=lambda j: (j.mode == "summary", -(j.n_fail + j.n_timeout)))
    return jobs[:max_tasks]


def _build_verifier_context(job: TaskAnalysisJob) -> str:
    """Build a concise verifier output section to include in the debugger query."""
    parts: list[str] = []
    for i, (rv, vout) in enumerate(zip(job.trace_rewards, job.verifier_outputs), 1):
        if not vout:
            continue
        label = "TIMEOUT" if rv < 0 else ("PASS" if rv >= 1.0 else "FAIL")
        # For passing traces, skip verbose verifier output
        if rv >= 1.0:
            continue
        # Extract just the pytest summary / assertion failures (last ~60 lines)
        lines = vout.strip().splitlines()
        if len(lines) > 60:
            vout_truncated = "\n".join(lines[-60:])
        else:
            vout_truncated = vout.strip()
        parts.append(
            f"--- Verifier test output (rollout {i}, {label}) ---\n"
            f"{vout_truncated}\n"
            f"--- end verifier output ---"
        )
    if not parts:
        return ""
    return (
        "\n\n⚠️ VERIFIER TEST OUTPUT (this is what the EXTERNAL evaluator actually ran "
        "after the agent completed — the agent NEVER sees this):\n"
        + "\n\n".join(parts)
    )


def _extract_trace_timing(trace_path: Path) -> str:
    """Extract per-turn timing summary from a trace JSON for timeout analysis."""
    try:
        data = json.loads(trace_path.read_text(errors="replace"))
    except (json.JSONDecodeError, OSError):
        return ""

    messages = data.get("messages", [])
    total_latency = data.get("latency", 0)

    turns: list[str] = []
    turn_idx = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        latency_ms = msg.get("latency", 0)
        tool_names = [tc.get("name", "?").replace("Tool: ", "") for tc in msg.get("tool_calls", [])]
        latency_s = latency_ms / 1000.0
        turn_idx += 1
        tool_str = ", ".join(tool_names) if tool_names else "no tools"
        turns.append(f"  Turn {turn_idx}: {latency_s:.1f}s ({tool_str})")

    if not turns:
        return ""

    total_s = total_latency / 1000.0 if total_latency else sum(
        msg.get("latency", 0) for msg in messages if msg.get("role") == "assistant"
    ) / 1000.0

    header = f"⏱️ TIMING BREAKDOWN (total {total_s:.0f}s, {turn_idx} turns):\n"
    return header + "\n".join(turns)


def _invoke_adb_ask_once(cmd: list[str], env: dict[str, str], timeout: float) -> str:
    """单次执行 ``adb ask``：成功返回模型回复文本；失败则返回以 ``[adb`` 开头的错误摘要。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        if result.returncode == 0:
            try:
                parsed = json.loads(result.stdout)
                if parsed.get("status") == "failed":
                    return f"[adb failed] {parsed.get('error', 'unknown error')}"
                return parsed.get("response", result.stdout.strip())
            except json.JSONDecodeError:
                return result.stdout.strip()
        return f"[adb error] exit={result.returncode}: {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return f"[adb timeout] exceeded {timeout}s"
    except Exception as e:
        return f"[adb exception] {e}"


def _run_single_adb_ask(job: TaskAnalysisJob, config: dict, k: int = 1,
                        extra_query_prefix: str = "") -> dict:
    """Run `adb ask` for one task and return result dict."""
    n_total = job.n_pass + job.n_fail + job.n_timeout

    label_parts = []
    for i, rv in enumerate(job.trace_rewards, 1):
        if rv < 0:
            label_parts.append(f"trace{i:02d}=TIMEOUT")
        else:
            label_parts.append(f"trace{i:02d}={'PASS' if rv >= 1.0 else 'FAIL'}")
    trace_labels = "Trace verdict: " + ", ".join(label_parts) if label_parts else ""

    fmt_vars = {
        "n_total": n_total,
        "n_pass": job.n_pass,
        "n_fail": job.n_fail,
        "n_timeout": job.n_timeout,
        "trace_labels": trace_labels,
    }
    if job.mode == "debug":
        if k == 1:
            template = config.get("qa_query_debug_k1", DEFAULT_DEBUG_QUERY_K1)
        else:
            template = config.get("qa_query_debug", DEFAULT_DEBUG_QUERY)
    else:
        if k == 1:
            template = config.get("qa_query_summary_k1", DEFAULT_SUMMARY_QUERY_K1)
        else:
            template = config.get("qa_query_summary", DEFAULT_SUMMARY_QUERY)
    try:
        query = template.format_map(fmt_vars)
    except (KeyError, ValueError):
        query = template

    if job.n_timeout > 0:
        timeout_note = (
            f"\n⚠️ TIMEOUT INFO: {job.n_timeout} of {n_total} rollout(s) TIMED OUT "
            f"(agent exceeded the time limit and was terminated). "
            f"The timed-out trace(s) show what the agent did before being killed. "
            f"Pay special attention to why the agent ran out of time: "
            f"was it stuck in a loop, retrying endlessly, spending too long on a wrong approach, "
            f"or was the task inherently too complex for the time limit?\n"
        )
        query = timeout_note + query
        # Add per-turn timing breakdown for timeout traces
        for tp, rv in zip(job.trace_paths, job.trace_rewards):
            if rv < 0:
                timing = _extract_trace_timing(tp)
                if timing:
                    query += f"\n\n{timing}"

    # Inject verifier test output for failing/timeout rollouts
    verifier_ctx = _build_verifier_context(job)
    if verifier_ctx:
        query += verifier_ctx

    if extra_query_prefix:
        query = extra_query_prefix + "\n" + query

    adb = _adb_path or "adb"
    cmd = [adb, "ask", "-t"] + [str(p) for p in job.trace_paths]
    if job.trace_type:
        cmd += ["--trace-type", job.trace_type]
    cmd += ["-q", query, "--format", "json"]

    env = os.environ.copy()
    llm_cfg = config.get("llm", {})
    if llm_cfg.get("model"):
        env["QA_MODEL_NAME"] = llm_cfg["model"]
    if llm_cfg.get("base_url"):
        env["QA_BASE_URL"] = llm_cfg["base_url"]
    if llm_cfg.get("api_key"):
        env["QA_API_KEY"] = llm_cfg["api_key"]

    timeout = float(config.get("timeout_per_task", 180))
    # 按 task 重试：子进程失败 / JSON status=failed / 超时 / 非零退出码 等凡返回 [adb 前缀的均会重试
    retry_attempts = max(1, int(config.get("retry_attempts", 3)))
    backoff = float(config.get("retry_backoff_seconds", 2.0))

    response = ""
    for attempt in range(retry_attempts):
        response = _invoke_adb_ask_once(cmd, env, timeout)
        if not response.startswith("[adb"):
            break
        if attempt < retry_attempts - 1:
            msg = response[:200].replace("\n", " ")
            print(
                f"[adb] task={job.task_name} 第 {attempt + 1}/{retry_attempts} 次失败，"
                f"{backoff:.1f}s 后重试: {msg}",
                flush=True,
            )
            time.sleep(backoff)
            backoff *= 2.0

    # When adb itself timed out / failed, build a fallback with key info
    # so the evolve agent has something to work with.
    # Full verifier output goes into the ## Verifier Test Output section of
    # the detail file (written by _write_debugger_analyse), so we only include
    # a short verifier summary here to avoid duplication.
    if response.startswith("[adb"):
        reason = "timed out" if response.startswith("[adb timeout]") else "failed"
        fallback_parts: list[str] = [response, ""]
        # Extract short verifier failure summary (just FAILED lines)
        failed_lines: list[str] = []
        for vout in job.verifier_outputs:
            if not vout:
                continue
            for line in vout.strip().splitlines():
                stripped = line.strip()
                if stripped.startswith("FAILED ") or stripped.startswith("FAILED\t"):
                    failed_lines.append(stripped)
        if failed_lines:
            fallback_parts.append(f"The debugger analysis {reason}. Verifier test failures:")
            for fl in failed_lines:
                fallback_parts.append(f"  - {fl}")
            fallback_parts.append("(Full verifier output is in the Verifier Test Output section below.)")
        if job.is_timeout or job.n_timeout > 0:
            for tp, rv in zip(job.trace_paths, job.trace_rewards):
                if rv < 0:
                    timing = _extract_trace_timing(tp)
                    if timing:
                        fallback_parts.append(f"\n{timing}")
        fallback_parts.append(
            "\n**NOTE**: Debugger LLM analysis was not available for this task. "
            "The evolve agent should read the raw trace directly if deeper analysis is needed."
        )
        response = "\n".join(fallback_parts)

    return {
        "task_name": job.task_name,
        "mode": job.mode,
        "n_pass": job.n_pass,
        "n_fail": job.n_fail,
        "n_timeout": job.n_timeout,
        "is_timeout": job.is_timeout,
        "response": response,
        "trace_paths": [str(p) for p in job.trace_paths],
        "trace_rewards": list(job.trace_rewards),
        "verifier_outputs": list(job.verifier_outputs),
    }


def _extract_one_liner(qa_response: str) -> str:
    """Extract a short plain-text summary for overview lines (single line).

    adb replies are often Markdown (## 回答, numbered sections). The old
    `. ` split misfired on ``1. ...`` list markers and produced broken overviews.
    """
    s = qa_response.strip()
    if s.startswith("[adb"):
        # Try to extract a FAILED test line from verifier output in the fallback
        for line in s.splitlines():
            stripped = line.strip()
            # Handle both raw "FAILED ..." and list-prefixed "- FAILED ..."
            if stripped.startswith("- "):
                stripped = stripped[2:].strip()
            if stripped.startswith("FAILED "):
                summary = stripped[:100] if len(stripped) > 100 else stripped
                tag = s.split("]", 1)[0] + "]"
                return f"{tag} verifier: {summary}"
        return s.split("\n", 1)[0][:100]

    cleaned: list[str] = []
    for raw in s.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^[-*]\s+", line)
        if m:
            line = line[m.end() :].strip()
        m = re.match(r"^\d+[\.)]\s*", line)
        if m:
            line = line[m.end() :].strip()
        if line:
            cleaned.append(line)

    if not cleaned:
        flat = re.sub(r"\s+", " ", s).strip()
        if len(flat) > 100:
            return flat[:97] + "..."
        return flat

    primary = cleaned[0]
    # Section label only (e.g. "ROOT CAUSE") — use the next line of prose
    if len(primary) <= 40 and not re.search(r"[。．；;，,]", primary) and len(cleaned) > 1:
        if len(cleaned[1]) > len(primary) + 10:
            primary = cleaned[1]

    for sep in ("。", ". ", "！", "!", "？", "?"):
        if sep in primary:
            primary = primary.split(sep, 1)[0].strip()
            break

    primary = re.sub(r"\s+", " ", primary).strip()
    if len(primary) > 100:
        return primary[:97] + "..."
    return primary


def _write_debugger_analyse(
    results: list[dict],
    iteration_dir: Path,
    iteration: int,
    analyse_dir: Path | None = None,
) -> tuple[Path, str]:
    """Write overview.md + detail/{task}.md under ``analyse_dir`` (defaults to input/analysis/).

    Returns (analyse_dir, query_snippet) where query_snippet is the overview
    body suitable for embedding in the evolution query (no top-level heading).
    """
    if analyse_dir is None:
        analyse_dir = iteration_dir / "input" / "analysis"
    detail_dir = analyse_dir / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    debug_results = [r for r in results if r["mode"] == "debug" and not r.get("is_timeout")]
    timeout_results = [r for r in results if r.get("is_timeout")]
    summary_results = [r for r in results if r["mode"] == "summary"]

    n_debug = len(debug_results)
    n_timeout = len(timeout_results)
    n_summary = len(summary_results)
    parts = []
    if n_debug:
        parts.append(f"{n_debug} debug")
    if n_timeout:
        parts.append(f"{n_timeout} timeout")
    if n_summary:
        parts.append(f"{n_summary} summary")
    body_lines: list[str] = [
        f"Analyzed {len(results)} tasks ({' + '.join(parts)}).\n",
    ]

    if timeout_results:
        body_lines.append("### Timeout (agent exceeded time limit)\n")
        for r in timeout_results:
            n_to = r.get("n_timeout", 0)
            n_total = r["n_pass"] + r["n_fail"] + n_to
            one_liner = _extract_one_liner(r["response"])
            body_lines.append(
                f"- **{r['task_name']}** ({n_to}/{n_total} timed out): {one_liner}"
            )
        body_lines.append("")

    if debug_results:
        body_lines.append("### Debug (has failures)\n")
        for r in debug_results:
            n_total = r["n_pass"] + r["n_fail"] + r.get("n_timeout", 0)
            one_liner = _extract_one_liner(r["response"])
            body_lines.append(
                f"- **{r['task_name']}** ({r['n_pass']}/{n_total} pass): {one_liner}"
            )
        body_lines.append("")

    if summary_results:
        names = ", ".join(r["task_name"] for r in summary_results)
        body_lines.append(f"### Summary (all pass): {len(summary_results)} tasks\n")
        body_lines.append(f"{names}\n")
        body_lines.append("(All rollouts passed — no per-task analysis included. Read `detail/{{task}}.md` if needed.)\n")

    try:
        detail_rel = detail_dir.relative_to(iteration_dir.parent.parent)
    except ValueError:
        detail_rel = detail_dir
    body_lines.append(f"Detail per task: `{detail_rel}/`")
    query_snippet = "\n".join(body_lines)

    overview_lines = [f"# Debugger Analysis Overview — Iteration {iteration}\n", query_snippet]
    overview_path = analyse_dir / "overview.md"
    overview_path.write_text("\n".join(overview_lines), encoding="utf-8")

    for r in results:
        n_to = r.get("n_timeout", 0)
        n_total = r["n_pass"] + r["n_fail"] + n_to
        trace_rewards = r.get("trace_rewards", [])
        verifier_outputs = r.get("verifier_outputs", [])

        status_parts = [f"{r['n_pass']} pass", f"{r['n_fail']} fail"]
        if n_to > 0:
            status_parts.append(f"{n_to} timeout")
        status_str = ", ".join(status_parts)

        heading_extra = " ⏱️ TIMEOUT" if r.get("is_timeout") else ""
        detail_lines = [
            f"# {r['task_name']} ({r['n_pass']}/{n_total} pass){heading_extra}\n",
            f"Analyzed {n_total} traces ({status_str}).\n",
        ]

        # Trace Paths at the top so trace01/trace02 labels are visible first
        detail_lines.append("## Trace Paths\n")
        for idx, tp in enumerate(r["trace_paths"]):
            p = Path(tp)
            rv = trace_rewards[idx] if idx < len(trace_rewards) else None
            if rv is not None and rv < 0:
                reward_label = "TIMEOUT"
            else:
                reward_file = p.parent.parent / "verifier" / "reward.txt"
                try:
                    rv = float(reward_file.read_text().strip()) if reward_file.exists() else 0.0
                except (ValueError, OSError):
                    rv = 0.0
                reward_label = "PASS" if rv >= 1.0 else "FAIL"
            detail_lines.append(f"- **trace{idx+1:02d}** ({reward_label}): `{tp}`")
        detail_lines.append("")

        detail_lines.append("## QA Analysis\n")
        detail_lines.append(r["response"])

        # Add verifier test output section for failing tasks
        has_verifier = False
        for idx, vout in enumerate(verifier_outputs):
            rv = trace_rewards[idx] if idx < len(trace_rewards) else 0.0
            if not vout or rv >= 1.0:
                continue
            if not has_verifier:
                detail_lines.append("\n## Verifier Test Output\n")
                has_verifier = True
            label = "TIMEOUT" if rv < 0 else "FAIL"
            lines = vout.strip().splitlines()
            if len(lines) > 80:
                vout_show = "\n".join(lines[-80:])
            else:
                vout_show = vout.strip()
            detail_lines.append(f"### trace{idx+1:02d} ({label})\n")
            detail_lines.append(f"```\n{vout_show}\n```\n")

        task_path = detail_dir / f"{r['task_name']}.md"
        task_path.write_text("\n".join(detail_lines), encoding="utf-8")

    return analyse_dir, query_snippet


def run_parallel_adb_ask(
    config: dict,
    job_dir: Path,
    task_results: dict[str, str],
    iteration_dir: Path,
    iteration: int,
    timeout_tasks: set[str] | None = None,
    k: int = 1,
) -> str | None:
    """Run parallel `adb ask` for each task and produce input/analysis/.

    Returns the overview.md content (for injection into evolution query), or None on failure.
    """
    if not _ensure_adb_installed():
        print("[adb] skipping agent debugger analysis: adb not available")
        return None

    jobs = _build_adb_jobs(task_results, job_dir, config, timeout_tasks=timeout_tasks)
    if not jobs:
        print("[adb] no tasks to analyze")
        return None

    max_workers = config.get("max_concurrent", 10)
    n_debug = sum(1 for j in jobs if j.mode == "debug" and not j.is_timeout)
    n_timeout = sum(1 for j in jobs if j.is_timeout)
    n_summary = sum(1 for j in jobs if j.mode == "summary")
    parts = [f"{n_debug} debug"]
    if n_timeout:
        parts.append(f"{n_timeout} timeout")
    parts.append(f"{n_summary} summary")
    print(f"[adb] analyzing {len(jobs)} tasks ({', '.join(parts)}) "
          f"with {max_workers} workers")

    # Run the first job serially to warm up adb's internal venv (~/.adb/venvs/),
    # avoiding race conditions when multiple workers create it simultaneously.
    first_result = _run_single_adb_ask(jobs[0], config, k=k)
    status = "ok" if not first_result["response"].startswith("[adb") else "err"
    print(f"  [{status}] {jobs[0].task_name} ({jobs[0].mode}) [warmup]")
    results: list[dict] = [first_result]
    remaining_jobs = jobs[1:]

    if remaining_jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(_run_single_adb_ask, job, config, k=k): job
                for job in remaining_jobs
            }
            for future in concurrent.futures.as_completed(future_map):
                job = future_map[future]
                try:
                    r = future.result()
                    results.append(r)
                    status = "ok" if not r["response"].startswith("[adb") else "err"
                    print(f"  [{status}] {job.task_name} ({job.mode})")
                except Exception as e:
                    print(f"  [exc] {job.task_name}: {e}")
                    results.append({
                        "task_name": job.task_name,
                        "mode": job.mode,
                        "n_pass": job.n_pass,
                        "n_fail": job.n_fail,
                        "n_timeout": job.n_timeout,
                        "is_timeout": job.is_timeout,
                        "response": f"[adb exception] {e}",
                        "trace_paths": [str(p) for p in job.trace_paths],
                        "trace_rewards": list(job.trace_rewards),
                        "verifier_outputs": list(job.verifier_outputs),
                    })

    results.sort(key=lambda r: (r["mode"] == "summary", -r["n_fail"]))

    analyse_dir, query_snippet = _write_debugger_analyse(results, iteration_dir, iteration)

    print(f"[adb] wrote {len(results)} analyses to {analyse_dir.relative_to(iteration_dir.parent.parent)}/")
    return query_snippet


# ---------------------------------------------------------------------------
# Phase 2.5b: Update evolution_history.md
# ---------------------------------------------------------------------------

def update_history_before(exp_dir: Path, iteration: int, computed_stats: dict,
                          job_dir: Path, diff: dict | None = None) -> None:
    """Before evolve agent runs, append current iteration evaluation results to evolution_history.md.

    Includes per-task pass/fail, cross-iteration changes (flipped/regressed) and retention rate.
    """
    history_path = exp_dir / "evolution_history.md"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    exc_types = computed_stats.get("exception_types", {})
    exc_str = ", ".join(f"{ek}: {ev}" for ek, ev in exc_types.items()) if exc_types else "None"
    k = computed_stats.get("k", 1)

    if not history_path.exists():
        history_path.write_text("# Agentic Harness Engineering Evolution History\n\n", encoding="utf-8")

    task_results = computed_stats.get("task_results", {})

    lines = []
    lines.append(f"## Iteration {iteration} — {now}")
    if k > 1:
        trial_stats = computed_stats.get("trial_stats", {})
        pass_at_k_data = computed_stats.get("pass_at_k", {})
        pass_at_rates = pass_at_k_data.get("pass_at", {}) if pass_at_k_data else {}
        lines.append(f"- **Evaluation** (each task x{k} rollouts)")
        if pass_at_rates:
            pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates))
            lines.append(f"- {pak_str}")
        else:
            lines.append(f"- Pass rate (pass@1): {computed_stats['pass_rate']:.1%}")
        per_task_rollouts = computed_stats.get("per_task_rollouts", {})
        pass_count_dist = collections.Counter()
        for t, r in task_results.items():
            if r == "fail":
                np_ = per_task_rollouts.get(t, {}).get("n_pass", 0)
                pass_count_dist[np_] += 1
        breakdown_parts = []
        breakdown_parts.append(f"{computed_stats['n_pass']} tasks passed all {k} rollouts")
        for np_count in sorted(pass_count_dist.keys(), reverse=True):
            if np_count > 0:
                breakdown_parts.append(f"{pass_count_dist[np_count]} tasks passed {np_count} of {k} rollouts")
        n_all_fail = pass_count_dist.get(0, 0)
        breakdown_parts.append(f"{n_all_fail} tasks failed all {k} rollouts")
        breakdown_parts.append(f"{computed_stats['n_exception']} tasks hit exception")
        lines.append(f"- {computed_stats['n_total']} tasks breakdown: {'; '.join(breakdown_parts)}")
        if trial_stats:
            lines.append(f"- Per-trial pass rate: {trial_stats['trial_pass_rate']:.1%} ({trial_stats['n_pass']}/{trial_stats['n_total']} trials)")
    else:
        lines.append(f"- Pass rate: {computed_stats['pass_rate']:.1%} ({computed_stats['n_pass']}/{computed_stats['n_total']})")

    if diff is not None:
        n_regressed = len(diff.get("regressed", []))
        n_stable_pass = len(diff.get("stable_pass", []))
        n_prev_pass_tested = n_stable_pass + n_regressed
        if n_prev_pass_tested > 0:
            retention_rate = n_stable_pass / n_prev_pass_tested
            lines.append(f"- Regressions: {n_regressed} (passed last iteration -> failed this iteration)")
            lines.append(f"- Retention rate: {retention_rate:.1%} ({n_stable_pass}/{n_prev_pass_tested} previously passed and still passing)")

    lines.append(f"- Exception stats: {exc_str}")
    lines.append(f"- Job directory: `{job_dir.name}`")

    pass_tasks = sorted(t for t, r in task_results.items() if r == "pass")
    fail_tasks = sorted(t for t, r in task_results.items() if r == "fail")
    exc_tasks = sorted(t for t, r in task_results.items() if r == "exception")

    lines.append(f"\n### Task Details")
    if k > 1:
        per_task_rollouts = computed_stats.get("per_task_rollouts", {})
        partial_pass_tasks = sorted(t for t in fail_tasks if per_task_rollouts.get(t, {}).get("n_pass", 0) > 0)
        all_fail_tasks = sorted(t for t in fail_tasks if per_task_rollouts.get(t, {}).get("n_pass", 0) == 0)

        lines.append(f"- ✅ All passed ({len(pass_tasks)}): {', '.join(pass_tasks) if pass_tasks else 'None'}")
        if partial_pass_tasks:
            partial_strs = [f"{t} ({per_task_rollouts[t]['n_pass']}/{per_task_rollouts[t]['total']})" for t in partial_pass_tasks]
            lines.append(f"- 🔶 Partial pass ({len(partial_pass_tasks)}): {', '.join(partial_strs)}")
        lines.append(f"- ❌ All failed ({len(all_fail_tasks)}): {', '.join(all_fail_tasks) if all_fail_tasks else 'None'}")
        lines.append(f"- ⚠️ Exception ({len(exc_tasks)}): {', '.join(exc_tasks[:15]) if exc_tasks else 'None'}")
    else:
        lines.append(f"- ✅ Passed ({len(pass_tasks)}): {', '.join(pass_tasks) if pass_tasks else 'None'}")
        lines.append(f"- ❌ Failed ({len(fail_tasks)}): {', '.join(fail_tasks) if fail_tasks else 'None'}")
        lines.append(f"- ⚠️ Exception ({len(exc_tasks)}): {', '.join(exc_tasks[:15]) if exc_tasks else 'None'}")

    if diff is not None:
        rollout_details = diff.get("rollout_details", {})

        def _fmt_task(t: str) -> str:
            """Format task name, appending rollout change annotation if available."""
            if t not in rollout_details:
                return t
            prev_np, cur_np, total = rollout_details[t]
            return f"{t} ({prev_np}/{total}->{cur_np}/{total})"

        def _fmt_tasks(tasks: list) -> str:
            return ", ".join(_fmt_task(t) for t in tasks) if tasks else "None"

        lines.append(f"\n### Cross-Iteration Changes (iteration {iteration-1} -> {iteration})")
        if k > 1 and rollout_details:
            if diff.get("flipped"):
                lines.append(f"- 🎉 fail->all-pass ({len(diff['flipped'])}): {_fmt_tasks(diff['flipped'])}")
            if diff.get("regressed"):
                lines.append(f"- 🔴 all-pass->fail ({len(diff['regressed'])}): {_fmt_tasks(diff['regressed'])}")
            if diff.get("stable_pass"):
                lines.append(f"- 🛡️ Stable all-pass ({len(diff['stable_pass'])}): {', '.join(diff['stable_pass'])}")
            if diff.get("rollout_improved"):
                lines.append(f"- 📈 Rollout improved, still not all-pass ({len(diff['rollout_improved'])}): {_fmt_tasks(diff['rollout_improved'])}")
            if diff.get("rollout_regressed"):
                lines.append(f"- 📉 Rollout regressed, still not all-pass ({len(diff['rollout_regressed'])}): {_fmt_tasks(diff['rollout_regressed'])}")
            if diff.get("rollout_unchanged"):
                lines.append(f"- 📌 Stable fail, same rollout results ({len(diff['rollout_unchanged'])}): {_fmt_tasks(diff['rollout_unchanged'])}")
            if diff.get("exception_to_fail"):
                lines.append(f"- 🆕 exception->fail ({len(diff['exception_to_fail'])}): {_fmt_tasks(diff['exception_to_fail'])}")
            if diff.get("infra_recovered"):
                lines.append(f"- exception->pass ({len(diff['infra_recovered'])}): {_fmt_tasks(diff['infra_recovered'])}")
            if diff.get("infra_lost"):
                lines.append(f"- pass->exception ({len(diff['infra_lost'])}): {_fmt_tasks(diff['infra_lost'])}")
            if diff.get("fail_to_exception"):
                lines.append(f"- fail->exception ({len(diff['fail_to_exception'])}): {_fmt_tasks(diff['fail_to_exception'])}")
        else:
            if diff.get("flipped"):
                lines.append(f"- 🎉 fail->pass ({len(diff['flipped'])}): {', '.join(diff['flipped'])}")
            if diff.get("regressed"):
                lines.append(f"- 🔴 pass->fail ({len(diff['regressed'])}): {', '.join(diff['regressed'])}")
            if diff.get("stable_pass"):
                lines.append(f"- 🛡️ Stable pass ({len(diff['stable_pass'])}): {', '.join(diff['stable_pass'])}")
            if diff.get("stable_fail"):
                lines.append(f"- 📌 Stable fail ({len(diff['stable_fail'])}): {', '.join(diff['stable_fail'])}")
            if diff.get("exception_to_fail"):
                lines.append(f"- 🆕 exception->fail ({len(diff['exception_to_fail'])}): {', '.join(diff['exception_to_fail'])}")
            if diff.get("infra_recovered"):
                lines.append(f"- exception->pass ({len(diff['infra_recovered'])}): {', '.join(diff['infra_recovered'])}")
            if diff.get("infra_lost"):
                lines.append(f"- pass->exception ({len(diff['infra_lost'])}): {', '.join(diff['infra_lost'])}")
            if diff.get("fail_to_exception"):
                lines.append(f"- fail->exception ({len(diff['fail_to_exception'])}): {', '.join(diff['fail_to_exception'])}")

    lines.append("")
    entry = "\n".join(lines) + "\n"

    with open(history_path, "a", encoding="utf-8") as f:
        f.write(entry)

    print(f"[history] Appended iteration {iteration} evaluation results")


def save_evolve_summary(iteration_dir: Path, iteration: int, evolve_result: str) -> None:
    """Save the evolve agent's final output to the iteration directory."""
    evolve_dir = iteration_dir / "evolve"
    evolve_dir.mkdir(parents=True, exist_ok=True)
    summary_path = evolve_dir / "evolve_summary.md"
    content = evolve_result.strip() if evolve_result else "(no output)"
    header = f"# Iteration {iteration} — Evolve Agent Output\n\n"
    summary_path.write_text(header + content + "\n", encoding="utf-8")
    print(f"[summary] Saved evolve output -> {summary_path.relative_to(iteration_dir.parent.parent)}")


def update_history_after(exp_dir: Path, iteration: int, evolve_result: str) -> None:
    """After evolve agent runs, append change summary to evolution_history.md."""
    history_path = exp_dir / "evolution_history.md"

    summary = evolve_result.strip() if evolve_result else "(no output)"

    with open(history_path, "a", encoding="utf-8") as f:
        f.write(f"**Evolve Agent Output:**\n\n{summary}\n\n---\n\n")

    print(f"[history] Appended iteration {iteration} evolution results")


# ---------------------------------------------------------------------------
# Phase 2.6: Update iteration_scores
# ---------------------------------------------------------------------------

def update_iteration_scores(exp_dir: Path, config: dict, iteration: int,
                            pass_rate: float, n_pass: int, n_total: int,
                            job_dir: Path, *,
                            n_exception: int = 0,
                            stats: dict | None = None,
                            timing: dict | None = None,
                            bon_variants: list[dict] | None = None) -> None:
    """Append current iteration scores to iteration_scores.yaml and regenerate .md."""
    scores_path = exp_dir / "iteration_scores.yaml"
    k = (stats or {}).get("k", 1)

    if scores_path.exists():
        with open(scores_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        meta_name = config.get("_meta", {}).get("_name", exp_dir.name)
        data = {
            "experiment": meta_name,
            "model": config.get("harbor", {}).get("model", ""),
            "scores": [],
        }

    n_fail = n_total - n_pass - n_exception

    entry = {
        "iteration": iteration,
        "pass_rate": round(pass_rate, 4),
        "k": k,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "job_dir": str(job_dir.relative_to(exp_dir)),
    }

    if timing:
        entry["timing"] = timing

    if bon_variants:
        entry["bon_variants"] = bon_variants

    if k > 1 and stats:
        per_task_rollouts = stats.get("per_task_rollouts", {})
        task_results = stats.get("task_results", {})
        pass_count_dist = collections.Counter()
        for t, r in task_results.items():
            if r == "pass":
                pass_count_dist[k] += 1
            elif r == "exception":
                pass_count_dist["exception"] += 1
            else:
                np_ = per_task_rollouts.get(t, {}).get("n_pass", 0)
                pass_count_dist[np_] += 1
        tasks_entry = {"total": n_total}
        for i in range(k, -1, -1):
            tasks_entry[f"passed_{i}_of_{k}"] = pass_count_dist.get(i, 0)
        tasks_entry["all_exception"] = pass_count_dist.get("exception", 0)
        entry["tasks"] = tasks_entry
        trial_stats = stats.get("trial_stats", {})
        if trial_stats:
            entry["trials"] = {
                "total": trial_stats["n_total"],
                "pass": trial_stats["n_pass"],
                "fail": trial_stats["n_fail"],
                "exception": trial_stats["n_exception"],
                "pass_rate": round(trial_stats["trial_pass_rate"], 4),
            }
        pass_at_k_data = stats.get("pass_at_k", {})
        if pass_at_k_data:
            entry["pass_at"] = {i: round(v, 4) for i, v in pass_at_k_data["pass_at"].items()}
    else:
        entry["tasks"] = {
            "total": n_total,
            "pass": n_pass,
            "fail": n_fail,
            "exception": n_exception,
        }

    data["scores"].append(entry)

    with open(scores_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)

    regenerate_scores_md(exp_dir, data)
    print(f"[scores] Updated iteration_scores (iteration {iteration})")


def regenerate_scores_md(exp_dir: Path, data: dict | None = None) -> None:
    """Regenerate iteration_scores.md from iteration_scores.yaml (or provided data dict)."""
    if data is None:
        scores_path = exp_dir / "iteration_scores.yaml"
        if not scores_path.exists():
            return
        with open(scores_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    scores = data.get("scores", [])
    if not scores:
        md_path = exp_dir / "iteration_scores.md"
        md_path.write_text(f"# {data.get('experiment', '')} Iteration Scores\n\n(no data)\n", encoding="utf-8")
        return

    k = scores[0].get("k", 1)
    md_path = exp_dir / "iteration_scores.md"
    if k > 1:
        task_col_headers = " | ".join(f"{i}/{k}" for i in range(k, -1, -1)) + " | Exc"
        task_col_seps = " | ".join("---" for _ in range(k + 1)) + " | ---"
        lines = [
            f"# {data['experiment']} Iteration Scores (k={k})\n",
            f"| Iter | " + " | ".join(f"pass@{i}" for i in range(1, k + 1))
            + f" | Tasks | {task_col_headers}"
            + f" | Trials | Trial P | Trial F | Trial E | Time |",
            f"|------|" + " | ".join("------" for _ in range(1, k + 1))
            + f" | ----- | {task_col_seps}"
            + f" | ------ | ------ | ------ | ------ | ------|",
        ]
        for s in scores:
            ts = s.get("timestamp", "")[:16].replace("T", " ")
            s_pass_at = s.get("pass_at", {})
            tasks = s.get("tasks", {})
            trials = s.get("trials", {})

            pak_cells = ""
            for i in range(1, k + 1):
                val = s_pass_at.get(i, s_pass_at.get(str(i), 0))
                pak_cells += f" {val:.1%} |"

            t_total = tasks.get("total", s.get("n_total", 0))
            task_dist_cells = ""
            for i in range(k, -1, -1):
                key = f"passed_{i}_of_{k}"
                if key in tasks:
                    task_dist_cells += f" {tasks[key]} |"
                elif i == k:
                    task_dist_cells += f" {tasks.get('all_pass', s.get('n_pass', 0))} |"
                elif i == 0:
                    task_dist_cells += f" {tasks.get('all_fail', s.get('n_fail', 0))} |"
                else:
                    task_dist_cells += " ? |"
            t_exc = tasks.get("all_exception", tasks.get("exception", s.get("n_exception", 0)))
            task_dist_cells += f" {t_exc} |"

            if trials:
                tr_total = trials.get("total", s.get("n_trials", 0))
                tr_pass = trials.get("pass", "?")
                tr_fail = trials.get("fail", "?")
                tr_exc = trials.get("exception", "?")
            else:
                tr_total = s.get("n_trials", 0)
                tr_pass = "?"
                tr_fail = "?"
                tr_exc = "?"

            row = (
                f"| {s['iteration']} |{pak_cells}"
                f" {t_total} |{task_dist_cells}"
                f" {tr_total} | {tr_pass} | {tr_fail} | {tr_exc} | {ts} |"
            )
            lines.append(row)
    else:
        has_timing = any(s.get("timing") for s in scores)
        has_variants = any(s.get("bon_variants") for s in scores)

        header = "| Iter | Pass Rate | Pass | Fail | Exc | Total"
        sep = "|------|-----------|------|------|-----|------"
        if has_variants:
            header += " | Variants"
            sep += " | --------"
        if has_timing:
            header += " | Duration | Eval | Anlys | Evolve"
            sep += " | -------- | ---- | ----- | ------"
        header += " | Time |"
        sep += " | ------|"

        lines = [
            f"# {data['experiment']} Iteration Scores\n",
            header,
            sep,
        ]
        for s in scores:
            ts = s.get("timestamp", "")[:16].replace("T", " ")
            rate = s.get("pass_rate", 0)
            tasks = s.get("tasks", {})
            t_pass = tasks.get("pass", s.get("n_pass", 0))
            t_fail = tasks.get("fail", s.get("n_fail", 0))
            t_exception = tasks.get("exception", s.get("n_exception", 0))
            t_total = tasks.get("total", s.get("n_total", 0))
            row = f"| {s['iteration']} | {rate:.1%} | {t_pass} | {t_fail} | {t_exception} | {t_total}"

            if has_variants:
                bv = s.get("bon_variants")
                if bv:
                    parts = []
                    for v in bv:
                        vr = v.get("pass_rate", 0)
                        marker = "*" if v.get("winner") else ""
                        parts.append(f"v{v['idx']}={vr:.1%}{marker}")
                    row += f" | {', '.join(parts)}"
                else:
                    row += " | -"

            if has_timing:
                t = s.get("timing", {})
                total = t.get("total_min", "")
                ev = t.get("eval_min", "")
                an = t.get("analysis_min", "")
                evo = t.get("evolve_min", "")
                row += f" | {total}m | {ev}m | {an}m | {evo}m" if total else " | - | - | - | -"

            row += f" | {ts} |"
            lines.append(row)

    if len(scores) >= 2:
        first_rate = scores[0].get("pass_rate", 0)
        last_rate = scores[-1].get("pass_rate", 0)
        diff_val = last_rate - first_rate
        lines.append(
            f"\nPass rate trend: {first_rate:.1%} -> {last_rate:.1%} "
            f"({'+' if diff_val >= 0 else ''}{diff_val*100:.1f}pp over {len(scores)} iterations)"
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 2.7: Change Attribution Evaluation
# ---------------------------------------------------------------------------

def load_change_manifest(exp_dir: Path, iteration: int) -> dict | None:
    """Load change_manifest.json for the specified iteration.

    Manifest may be in the experiment root directory, the iteration evolve directory,
    or the legacy iteration directory.
    """
    iter_dir = exp_dir / "runs" / f"iteration_{iteration:03d}"
    for candidate in [
        exp_dir / "change_manifest.json",
        iter_dir / "evolve" / "change_manifest.json",
        iter_dir / "change_manifest.json",
    ]:
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if data.get("iteration") == iteration:
                    return data
            except (json.JSONDecodeError, KeyError):
                pass
    return None


def archive_change_manifest(exp_dir: Path, iteration: int) -> None:
    """Archive change_manifest.json from experiment root to the iteration evolve directory."""
    src = exp_dir / "change_manifest.json"
    if not src.exists():
        return
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        if data.get("iteration") == iteration:
            evolve_dir = exp_dir / "runs" / f"iteration_{iteration:03d}" / "evolve"
            evolve_dir.mkdir(parents=True, exist_ok=True)
            dst = evolve_dir / "change_manifest.json"
            shutil.copy2(src, dst)
            print(f"[manifest] Archived change_manifest.json -> {dst.relative_to(exp_dir)}")
    except (json.JSONDecodeError, KeyError):
        pass


def evaluate_changes(
    manifest: dict,
    diff: dict,
    current_task_results: dict,
) -> dict:
    """Generate attribution evaluation for each change based on change_manifest and iteration diff.

    Returns:
      {
        "evaluating_iteration": int,
        "change_evaluations": [
          {
            "change_id": str,
            "description": str,
            "files": list[str],
            "predicted_fixes": list[str],
            "actually_fixed": list[str],
            "still_failed": list[str],
            "predicted_risks": list[str],
            "risk_realized": list[str],
            "hit_rate": str,
            "verdict": "EFFECTIVE" | "PARTIALLY_EFFECTIVE" | "MIXED" | "INEFFECTIVE" | "HARMFUL"
          }
        ],
        "unattributed_regressions": list[str],
        "summary": str
      }
    """
    changes = manifest.get("changes", [])
    flipped_set = set(diff.get("flipped", []))
    regressed_set = set(diff.get("regressed", []))

    all_predicted = set()
    all_risk = set()
    evaluations = []

    for chg in changes:
        chg_id = chg.get("id", "unknown")
        predicted = chg.get("predicted_fixes", [])
        risks = chg.get("risk_tasks", [])

        all_predicted.update(predicted)
        all_risk.update(risks)

        actually_fixed = [t for t in predicted if t in flipped_set]
        still_failed = [t for t in predicted if t not in flipped_set]
        risk_realized = [t for t in risks if t in regressed_set]

        # Additional check: whether regressions have tasks related to this change's files but not in predicted/risk
        # (This requires knowing which file affects which task - we cannot determine this precisely, so we only use declared risk_tasks)

        n_fixed = len(actually_fixed)
        n_predicted = len(predicted)
        n_risk_hit = len(risk_realized)

        if n_risk_hit > 0 and n_fixed == 0:
            verdict = "HARMFUL"
        elif n_risk_hit > 0 and n_fixed > 0:
            verdict = "MIXED"
        elif n_fixed == n_predicted and n_predicted > 0:
            verdict = "EFFECTIVE"
        elif n_fixed > 0:
            verdict = "PARTIALLY_EFFECTIVE"
        else:
            verdict = "INEFFECTIVE"

        evaluations.append({
            "change_id": chg_id,
            "description": chg.get("description", ""),
            "files": chg.get("files", []),
            "predicted_fixes": predicted,
            "actually_fixed": actually_fixed,
            "still_failed": still_failed,
            "predicted_risks": risks,
            "risk_realized": risk_realized,
            "hit_rate": f"{n_fixed}/{n_predicted}" if n_predicted > 0 else "0/0",
            "verdict": verdict,
        })

    # Find unattributed regressions (not in any change's predicted or risk lists)
    attributed_tasks = all_predicted | all_risk
    unattributed = [t for t in regressed_set if t not in attributed_tasks]

    summary_parts = [f"{e['change_id']}: {e['verdict']}" for e in evaluations]

    return {
        "evaluating_iteration": manifest.get("iteration", 0),
        "change_evaluations": evaluations,
        "unattributed_regressions": sorted(unattributed),
        "summary": ", ".join(summary_parts),
    }


def save_change_evaluation(exp_dir: Path, iteration: int, evaluation: dict) -> None:
    """Save change attribution evaluation results to the iteration's input directory."""
    input_dir = exp_dir / "runs" / f"iteration_{iteration:03d}" / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    eval_path = input_dir / "change_evaluation.json"
    with open(eval_path, "w", encoding="utf-8") as f:
        json.dump(evaluation, f, ensure_ascii=False, indent=2)
    print(f"[eval] Saved change attribution -> {eval_path.relative_to(exp_dir)}")


# ---------------------------------------------------------------------------
# Phase 2.9: Auto Rollback Mechanism (Full Rollback - Fallback)
# ---------------------------------------------------------------------------

def load_best_ever(exp_dir: Path) -> dict | None:
    """Load best-ever iteration info."""
    best_path = exp_dir / "best_ever.json"
    if best_path.exists():
        return json.loads(best_path.read_text(encoding="utf-8"))
    return None


def save_best_ever(exp_dir: Path, best: dict) -> None:
    """Persist best-ever iteration info."""
    best_path = exp_dir / "best_ever.json"
    with open(best_path, "w", encoding="utf-8") as f:
        json.dump(best, f, ensure_ascii=False, indent=2)


def update_best_ever(exp_dir: Path, iteration: int, stats: dict) -> dict:
    """Update best-ever with current iteration, return latest best-ever info."""
    current = load_best_ever(exp_dir)
    pass_rate = stats["pass_rate"]

    if current is None or pass_rate > current.get("pass_rate", current.get("capability_rate", 0)):
        k = stats.get("k", 1)
        n_total = stats["n_pass"] + stats["n_fail"] + stats["n_exception"]
        best = {
            "iteration": iteration,
            "pass_rate": stats["pass_rate"],
            "k": k,
        }
        if k > 1:
            per_task_rollouts = stats.get("per_task_rollouts", {})
            task_results = stats.get("task_results", {})
            pass_count_dist = collections.Counter()
            for t, r in task_results.items():
                if r == "pass":
                    pass_count_dist[k] += 1
                elif r == "exception":
                    pass_count_dist["exception"] += 1
                else:
                    pass_count_dist[per_task_rollouts.get(t, {}).get("n_pass", 0)] += 1
            tasks_entry = {"total": n_total}
            for i in range(k, -1, -1):
                tasks_entry[f"passed_{i}_of_{k}"] = pass_count_dist.get(i, 0)
            tasks_entry["all_exception"] = pass_count_dist.get("exception", 0)
            best["tasks"] = tasks_entry
            trial_stats = stats.get("trial_stats", {})
            if trial_stats:
                best["trials"] = {
                    "total": trial_stats["n_total"],
                    "pass": trial_stats["n_pass"],
                    "fail": trial_stats["n_fail"],
                    "exception": trial_stats["n_exception"],
                    "pass_rate": round(trial_stats["trial_pass_rate"], 4),
                }
            pass_at_k_data = stats.get("pass_at_k", {})
            if pass_at_k_data:
                best["pass_at"] = {i: round(v, 4) for i, v in pass_at_k_data["pass_at"].items()}
        else:
            best["tasks"] = {
                "total": n_total,
                "pass": stats["n_pass"],
                "fail": stats["n_fail"],
                "exception": stats["n_exception"],
            }
        save_best_ever(exp_dir, best)
        print(f"[best] Updated best-ever: iteration {iteration}, pass rate {pass_rate:.1%}")
        return best

    return current




def perform_auto_rollback(exp_dir: Path, workspace_dir: Path, best_iteration: int) -> bool:
    """Auto-rollback workspace to best-ever iteration snapshot."""
    snapshot_dir = exp_dir / "runs" / f"iteration_{best_iteration:03d}" / "input" / "workspace"
    if not snapshot_dir.exists():
        snapshot_dir = exp_dir / "runs" / f"iteration_{best_iteration:03d}" / "workspace_snapshot"
    if not snapshot_dir.exists():
        print(f"[rollback] Warning: cannot find iteration {best_iteration} workspace snapshot, skipping auto-rollback")
        return False

    git_dir = workspace_dir / ".git"
    git_backup = workspace_dir.parent / ".git_backup_rollback"

    if git_dir.exists():
        if git_backup.exists():
            shutil.rmtree(git_backup)
        shutil.move(str(git_dir), str(git_backup))

    for item in workspace_dir.iterdir():
        if item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    for item in snapshot_dir.iterdir():
        dst = workspace_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst)
        else:
            shutil.copy2(item, dst)

    if git_backup.exists():
        shutil.move(str(git_backup), str(git_dir))

    print(f"[rollback] Auto-rolled back workspace to iteration {best_iteration} snapshot")
    return True


def rollback_experiment_metadata(exp_dir: Path, start_iteration: int) -> None:
    """Roll back experiment-level metadata files so they only contain entries
    for iterations *before* ``start_iteration``.

    Affected files:
      - iteration_scores.yaml / iteration_scores.md
      - task_history.json
      - evolution_history.md
      - change_manifest.json  (restored from the prior iteration's archive)
      - best_ever.json        (recalculated from remaining scores)
    """
    import re as _re

    # -- iteration_scores.yaml / .md ------------------------------------------
    scores_path = exp_dir / "iteration_scores.yaml"
    if scores_path.exists():
        with open(scores_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        original_len = len(data.get("scores", []))
        data["scores"] = [s for s in data.get("scores", []) if s.get("iteration", 0) < start_iteration]
        with open(scores_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        regenerate_scores_md(exp_dir, data)
        print(f"[resume] Rolled back iteration_scores: {original_len} -> {len(data['scores'])} entries (kept iterations < {start_iteration})")

    # -- task_history.json ----------------------------------------------------
    th_path = exp_dir / "task_history.json"
    if th_path.exists():
        with open(th_path, encoding="utf-8") as f:
            history = json.load(f)
        for task_name in list(history.keys()):
            history[task_name] = [e for e in history[task_name] if e[0] < start_iteration]
            if not history[task_name]:
                del history[task_name]
        with open(th_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
        print(f"[resume] Rolled back task_history.json (kept iterations < {start_iteration})")

    # -- evolution_history.md -------------------------------------------------
    eh_path = exp_dir / "evolution_history.md"
    if eh_path.exists():
        content = eh_path.read_text(encoding="utf-8")
        earliest_cut = None
        for m in _re.finditer(r"^## Iteration (\d+)\b", content, _re.MULTILINE):
            if int(m.group(1)) >= start_iteration:
                earliest_cut = m.start()
                break
        if earliest_cut is not None:
            truncated = content[:earliest_cut].rstrip() + "\n"
            eh_path.write_text(truncated, encoding="utf-8")
            print(f"[resume] Truncated evolution_history.md (removed entries for iterations >= {start_iteration})")
        else:
            print(f"[resume] evolution_history.md has no entries for iterations >= {start_iteration}, left as-is")

    # -- change_manifest.json -------------------------------------------------
    manifest_path = exp_dir / "change_manifest.json"
    if start_iteration > 1:
        prev_iter = start_iteration - 1
        archived = exp_dir / "runs" / f"iteration_{prev_iter:03d}" / "evolve" / "change_manifest.json"
        if archived.exists():
            shutil.copy2(archived, manifest_path)
            print(f"[resume] Restored change_manifest.json from iteration {prev_iter} archive")
        elif manifest_path.exists():
            manifest_path.unlink()
            print(f"[resume] Removed stale change_manifest.json (no archive for iteration {prev_iter})")
    elif manifest_path.exists():
        manifest_path.unlink()
        print(f"[resume] Removed change_manifest.json (resuming from iteration 1)")

    # -- best_ever.json -------------------------------------------------------
    best_path = exp_dir / "best_ever.json"
    if scores_path.exists():
        with open(scores_path, encoding="utf-8") as f:
            remaining = yaml.safe_load(f) or {}
        remaining_scores = remaining.get("scores", [])
        if remaining_scores:
            best_score = max(remaining_scores, key=lambda s: s.get("pass_rate", 0))
            best = {
                "iteration": best_score["iteration"],
                "pass_rate": best_score["pass_rate"],
            }
            if "k" in best_score:
                best["k"] = best_score["k"]
            if "tasks" in best_score:
                best["tasks"] = best_score["tasks"]
            if "trials" in best_score:
                best["trials"] = best_score["trials"]
            if "pass_at" in best_score:
                best["pass_at"] = best_score["pass_at"]
            with open(best_path, "w", encoding="utf-8") as f:
                json.dump(best, f, ensure_ascii=False, indent=2)
            print(f"[resume] Recalculated best_ever.json -> iteration {best['iteration']} ({best['pass_rate']:.1%})")
        elif best_path.exists():
            best_path.unlink()
            print(f"[resume] Removed best_ever.json (no scores remaining)")
    elif best_path.exists():
        best_path.unlink()
        print(f"[resume] Removed best_ever.json (no scores file)")

    print(f"[resume] Experiment metadata rolled back to before iteration {start_iteration}")


# ---------------------------------------------------------------------------
# Phase 3: Invoke NexAU Evolve Agent
# ---------------------------------------------------------------------------

def build_evolution_query(
    iteration: int,
    stats: dict,
    job_dir: Path,
    iteration_dir: Path,
    prev_stats: dict | None,
    diff: dict | None,
    stability: dict | None,
    best_ever: dict | None,
    scores_trend: list[dict] | None,
    change_evaluation: dict | None = None,
    adb_overview: str | None = None,
    strategy_hint: str | None = None,
    prev_variant_comparison: dict | None = None,
    workspace_path: str | None = None,
) -> str:
    """Build the evolution agent query.

    Provides concise structured info: evaluation overview, task classification, cross-iteration changes,
    debugger analysis, historical trends, stability analysis, change attribution.
    Deep trajectory analysis is left for the evolve agent to explore.
    """
    k = stats.get("k", 1)
    lines = [f"Iteration {iteration} evaluation completed.\n"]

    # -- 1. Current Iteration Overview --
    lines.append("## 1. Current Iteration Overview")
    try:
        runs_dir = iteration_dir.parent
        results_rel = job_dir.relative_to(runs_dir.parent)
    except ValueError:
        results_rel = job_dir.name
    lines.append(f"- Results path: `{results_rel}/`")

    if k > 1:
        trial_stats = stats.get("trial_stats", {})
        pass_at_k_data = stats.get("pass_at_k", {})
        pass_at_rates = pass_at_k_data.get("pass_at", {})
        lines.append(f"- **Evaluation**: each task runs {k} times")
        lines.append(f"- Pass rate: **{stats['pass_rate']:.1%}**")
        if pass_at_rates:
            pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates))
            lines.append(f"- **Pass@k**: {pak_str}")
        lines.append(f"- Tasks: {stats['n_pass']} all-pass | {stats['n_fail']} has-failure | {stats['n_exception']} exception")
        if trial_stats:
            lines.append(f"- Per-trial stats: {trial_stats['n_pass']} pass, {trial_stats['n_fail']} fail, {trial_stats['n_exception']} exception out of {trial_stats['n_total']} trials (trial pass rate: {trial_stats['trial_pass_rate']:.1%})")
    else:
        lines.append(f"- Pass rate: **{stats['pass_rate']:.1%}** ({stats['n_pass']}/{stats['n_total']})")
        lines.append(f"- Pass: {stats['n_pass']} | Fail: {stats['n_fail']} | Exception: {stats['n_exception']}")

    if stats["exception_types"]:
        exc_str = ", ".join(f"{ek}: {ev}" for ek, ev in stats["exception_types"].items())
        lines.append(f"- Exception types: {exc_str}")
    n_timeout_tasks = len(stats.get("timeout_tasks", set()))
    n_pure_infra = stats["n_exception"] - n_timeout_tasks
    if n_timeout_tasks > 0:
        lines.append(f"- ⏱️ {n_timeout_tasks} tasks TIMED OUT (agent ran out of time, traces available for analysis)")
    if n_pure_infra > 0:
        lines.append(f"- Warning: {n_pure_infra} tasks had infra errors (NOT agent capability issues)")

    if diff is not None:
        n_regressed = len(diff.get("regressed", []))
        n_stable_pass = len(diff.get("stable_pass", []))
        n_prev_pass_tested = n_stable_pass + n_regressed
        if n_prev_pass_tested > 0:
            retention_rate = n_stable_pass / n_prev_pass_tested
            lines.append(f"- Regressions: **{n_regressed}** (passed last iteration -> failed this iteration)")
            lines.append(f"- Retention rate: **{retention_rate:.1%}** ({n_stable_pass}/{n_prev_pass_tested} previously passed and still passing)")

    # -- 2. Task Classification Details --
    lines.append("\n## 2. Task Classification Details")
    task_results = stats["task_results"]
    per_task_rollouts = stats.get("per_task_rollouts", {})
    pass_tasks = sorted(t for t, r in task_results.items() if r == "pass")
    fail_tasks = sorted(t for t, r in task_results.items() if r == "fail")
    exc_tasks = sorted(t for t, r in task_results.items() if r == "exception")

    if k > 1:
        lines.append(f"\n### ✅ Passed ({len(pass_tasks)}) — all {k} rollouts passed")
    else:
        lines.append(f"\n### ✅ Passed ({len(pass_tasks)})")
    if pass_tasks:
        lines.append(", ".join(pass_tasks))

    if k > 1:
        per_task_pass_at = pass_at_k_data.get("per_task_pass_at", {}) if pass_at_k_data else {}
        lines.append(f"\n### ❌ Failed ({len(fail_tasks)}) - Primary optimization target")
        if fail_tasks and per_task_rollouts:
            partial_pass = []
            zero_pass = []
            for t in fail_tasks:
                ro = per_task_rollouts.get(t, {})
                tp = ro.get("n_pass", 0)
                if tp > 0:
                    partial_pass.append((t, tp, ro.get("total", k)))
                else:
                    zero_pass.append(t)
            if partial_pass:
                lines.append(f"\n#### Partial pass ({len(partial_pass)}) — high-priority targets")
                lines.append(f"**Action required**: For each partial-pass task, read BOTH a passing and a failing rollout's `nexau_in_memory_tracer.cleaned.json`, compare where they diverge, and identify why one succeeded and the other failed.")
                for t, tp, total in partial_pass:
                    task_pass_at = per_task_pass_at.get(t, {})
                    pak_str = ", ".join(f"pass@{i}={task_pass_at[i]:.0%}" for i in sorted(task_pass_at)) if task_pass_at else ""
                    lines.append(f"- {t}: {tp}/{total} passed ({pak_str})")
            if zero_pass:
                lines.append(f"\n#### Zero pass ({len(zero_pass)}) — failed all {k} rollouts")
                lines.append(", ".join(zero_pass))
        elif fail_tasks:
            lines.append(", ".join(fail_tasks))
    else:
        lines.append(f"\n### ❌ Failed ({len(fail_tasks)}) - Primary optimization target")
        if fail_tasks:
            lines.append(", ".join(fail_tasks))

    timeout_tasks_set = stats.get("timeout_tasks", set())
    timeout_exc_tasks = sorted(t for t in exc_tasks if t in timeout_tasks_set)
    pure_exc_tasks = sorted(t for t in exc_tasks if t not in timeout_tasks_set)

    if timeout_exc_tasks:
        lines.append(f"\n### ⏱️ Timed Out ({len(timeout_exc_tasks)}) - Agent exceeded time limit, analyze traces")
        lines.append("These tasks timed out (agent ran out of time). Their traces show what the agent did before being terminated.")
        lines.append("Analyze these traces to understand why the agent was too slow and how to improve efficiency.")
        lines.append(", ".join(timeout_exc_tasks))

    if pure_exc_tasks:
        lines.append(f"\n### ⚠️ Infrastructure Exceptions ({len(pure_exc_tasks)}, not agent issues, please ignore)")
        exc_by_type: dict[str, list[str]] = {}
        for tn in pure_exc_tasks:
            et = "Unknown"
            try:
                td = _find_trial_dir(job_dir, tn)
                if td:
                    ep = td / "exception.txt"
                    if ep.exists():
                        et = _extract_exception_type(ep.read_text(errors="replace"))
            except OSError:
                pass
            exc_by_type.setdefault(et, []).append(tn)
        for et, tasks in exc_by_type.items():
            lines.append(f"- {et} ({len(tasks)}): {', '.join(tasks[:10])}")

    # -- 3. Cross-Iteration Change Matrix --
    if diff is not None:
        rollout_details = diff.get("rollout_details", {})
        lines.append(f"\n## 3. Detailed Cross-Iteration Analysis (iteration {iteration-1} -> {iteration})")

        if k > 1 and rollout_details:
            def _eq_fmt(t):
                if t in rollout_details:
                    pn, cn, tot = rollout_details[t]
                    return f"{t} ({pn}/{tot}->{cn}/{tot})"
                return t
            def _eq_fmt_list(tasks):
                return ", ".join(_eq_fmt(t) for t in tasks)

            if diff["flipped"]:
                lines.append(f"\n### 🎉 Improved fail->all-pass ({len(diff['flipped'])}) - Your changes worked on these tasks")
                lines.append(_eq_fmt_list(diff["flipped"]))
            if diff["regressed"]:
                lines.append(f"\n### 🔴 Regressed all-pass->fail ({len(diff['regressed'])}) - Must be addressed first")
                lines.append(_eq_fmt_list(diff["regressed"]))
            if diff.get("rollout_improved"):
                lines.append(f"\n### 📈 Rollout improved, still not all-pass ({len(diff['rollout_improved'])}) - Partial improvement")
                lines.append(_eq_fmt_list(diff["rollout_improved"]))
            if diff.get("rollout_regressed"):
                lines.append(f"\n### 📉 Rollout regressed, still not all-pass ({len(diff['rollout_regressed'])}) - Partial regression")
                lines.append(_eq_fmt_list(diff["rollout_regressed"]))
            if diff.get("rollout_unchanged"):
                lines.append(f"\n### 📌 Stable fail, same rollout results ({len(diff['rollout_unchanged'])}) - Needs new strategy")
                lines.append(_eq_fmt_list(diff["rollout_unchanged"]))
            if diff.get("stable_pass"):
                lines.append(f"\n### 🛡️ Stable all-pass ({len(diff['stable_pass'])}) - Protect, avoid breaking")
                lines.append(", ".join(diff["stable_pass"]))
            if diff.get("exception_to_fail"):
                lines.append(f"\n### 🆕 New optimization targets exception->fail ({len(diff['exception_to_fail'])}) - Previously blocked by infra errors, now optimizable")
                lines.append(_eq_fmt_list(diff["exception_to_fail"]))
            if diff.get("infra_recovered"):
                lines.append(f"\n### Infra recovered exception->pass ({len(diff['infra_recovered'])}, for reference only)")
            if diff.get("infra_lost"):
                lines.append(f"\n### Infra failure pass->exception ({len(diff['infra_lost'])}, for reference only)")
            if diff.get("fail_to_exception"):
                lines.append(f"\n### fail->exception ({len(diff['fail_to_exception'])}, for reference only)")
        else:
            if diff["flipped"]:
                lines.append(f"\n### 🎉 Improved fail->pass ({len(diff['flipped'])}) - Your changes worked on these tasks")
                lines.append(", ".join(diff["flipped"]))
            if diff["regressed"]:
                lines.append(f"\n### 🔴 Regressed pass->fail ({len(diff['regressed'])}) - Must be addressed first")
                lines.append(", ".join(diff["regressed"]))
            if diff.get("stable_fail"):
                lines.append(f"\n### 📌 Stable fail fail->fail ({len(diff['stable_fail'])}) - Needs new strategy")
                lines.append(", ".join(diff["stable_fail"]))
            if diff.get("stable_pass"):
                lines.append(f"\n### 🛡️ Stable pass pass->pass ({len(diff['stable_pass'])}) - Protect, avoid breaking")
                lines.append(", ".join(diff["stable_pass"]))
            if diff.get("exception_to_fail"):
                lines.append(f"\n### 🆕 New optimization targets exception->fail ({len(diff['exception_to_fail'])}) - Previously blocked by infra errors, now optimizable")
                lines.append(", ".join(diff["exception_to_fail"]))
            if diff.get("infra_recovered"):
                lines.append(f"\n### Infra recovered exception->pass ({len(diff['infra_recovered'])}, for reference only)")
            if diff.get("infra_lost"):
                lines.append(f"\n### Infra failure pass->exception ({len(diff['infra_lost'])}, for reference only)")
            if diff.get("fail_to_exception"):
                lines.append(f"\n### fail->exception ({len(diff['fail_to_exception'])}, for reference only)")

        lines.append(f"\n- Net change: {diff['net']:+d}")
        if prev_stats:
            prev_rate = prev_stats.get("pass_rate", 0)
            cur_rate = stats["pass_rate"]
            delta = cur_rate - prev_rate
            lines.append(f"- Pass rate change: {prev_rate:.1%} -> {cur_rate:.1%} ({delta:+.1%})")
        if diff["net"] < 0:
            lines.append(f"- ⚠️ **Net regression**: Previous iteration changes may be harmful")

    # -- 4. Agent Debugger Analysis --
    if adb_overview:
        lines.append(f"\n## 4. Agent Debugger Analysis (LLM-powered root cause analysis)")
        lines.append(adb_overview)
        analyse_rel = f"runs/iteration_{iteration:03d}/input/analysis"
        lines.append(f"\nFor full per-task analysis: `read_file {analyse_rel}/detail/{{task_name}}.md`")
        lines.append("For raw traces: see trace paths listed in each detail file.")

    # -- 5. Historical Trends --
    if scores_trend and len(scores_trend) >= 2:
        if k > 1:
            lines.append(f"\n## 5. Historical Trends")
            for i in range(1, k + 1):
                pak_parts = []
                for s in scores_trend:
                    s_pass_at = s.get("pass_at", {})
                    val = s_pass_at.get(i, s_pass_at.get(str(i)))
                    if val is not None:
                        pak_parts.append(f"iter{s['iteration']}: {val:.1%}")
                if pak_parts:
                    lines.append(f"- pass@{i}: {' -> '.join(pak_parts)}")
        else:
            lines.append(f"\n## 5. Historical Trends (Pass Rate)")
            trend_parts = []
            for s in scores_trend:
                rate = s.get("pass_rate", 0)
                trend_parts.append(f"iter{s['iteration']}: {rate:.1%}")
            lines.append(f"- {' -> '.join(trend_parts)}")

    # -- 6. Best Ever --
    if best_ever:
        lines.append(f"\n## 6. Best Ever")
        lines.append(f"- Best pass rate: **{best_ever.get('pass_rate', best_ever.get('capability_rate', 0)):.1%}** (iteration {best_ever['iteration']})")
        if best_ever["iteration"] != iteration:
            lines.append(f"- Best version snapshot: `runs/iteration_{best_ever['iteration']:03d}/input/workspace/`")

    # -- 7. Task Stability --
    if stability:
        lines.append(f"\n## 7. Task Stability Analysis (Across All Historical Iterations)")
        if stability["unstable"]:
            lines.append(f"- ⚠️ Unstable tasks ({len(stability['unstable'])}, do NOT optimize for these): {stability['unstable'][:20]}")
        if stability.get("possibly_unstable"):
            lines.append(f"- Possibly unstable ({len(stability['possibly_unstable'])}): {stability['possibly_unstable'][:15]}")
        lines.append(f"- Stable pass: {len(stability['stable_pass'])}")
        lines.append(f"- Stable fail: {len(stability['stable_fail'])} - These are the primary optimization targets")
        if stability["infra_only"]:
            lines.append(f"- Infra-only errors: {len(stability['infra_only'])} (ignore)")

    # -- 8. Change Attribution Report --
    if change_evaluation:
        evals = change_evaluation.get("change_evaluations", [])
        if evals:
            lines.append(f"\n## 8. Previous Iteration Change Attribution Report (Auto-Generated)")
            lines.append(f"You must use this report to decide whether to rollback previous changes. HARMFUL and INEFFECTIVE changes should be prioritized for rollback.")
            lines.append(f"\n| Change | Predicted Fixes | Actually Fixed | Regressions | Verdict | Suggested Action |")
            lines.append(f"|--------|----------------|----------------|-------------|---------|-----------------|")
            for e in evals:
                n_pred = len(e["predicted_fixes"])
                n_fixed = len(e["actually_fixed"])
                verdict = e["verdict"]
                suggestion = {
                    "EFFECTIVE": "✅ Keep",
                    "PARTIALLY_EFFECTIVE": "⚠️ Keep, continue monitoring",
                    "MIXED": "⚠️ Keep effective parts, rollback harmful parts",
                    "INEFFECTIVE": "🔸 Rollback or redesign",
                    "HARMFUL": "❌ Must rollback",
                }.get(verdict, "?")
                lines.append(
                    f"| {e['change_id']}: {e['description'][:40]} "
                    f"| {n_fixed}/{n_pred} | {', '.join(e['actually_fixed'][:5]) or '-'} "
                    f"| {', '.join(e.get('risk_realized', [])[:5]) or '-'} "
                    f"| {verdict} | {suggestion} |"
                )

            for e in evals:
                if e["still_failed"]:
                    lines.append(f"- {e['change_id']} predicted but not fixed: {e['still_failed']}")

            unattr = change_evaluation.get("unattributed_regressions", [])
            if unattr:
                lines.append(f"\n⚠️ Unattributed regression tasks: {unattr}")
                lines.append(f"Please analyze the causes of these regressions, may be interaction effects of multiple changes")

            lines.append(f"\n**Rollback method**: Compare with `runs/iteration_{change_evaluation['evaluating_iteration']:03d}/input/workspace/`, "
                         f"restore files that need rollback to that version.")

    # -- Previous Iteration Variant Experiment Results --
    if prev_variant_comparison:
        variants_data = prev_variant_comparison.get("variants", [])
        winner_idx = prev_variant_comparison.get("winner_idx", 0)
        if variants_data:
            lines.append(f"\n## Previous Iteration Variant Experiment Results")
            lines.append(f"Last iteration tested {len(variants_data)} parallel architecture variants:\n")
            for v in variants_data:
                vidx = v.get("idx", 0)
                is_winner = v.get("is_winner", vidx == winner_idx)
                marker = " — **SELECTED as winner**" if is_winner else " — NOT selected"
                lines.append(f"### Variant {vidx}{marker}")
                if v.get("hint"):
                    lines.append(f"- Strategy constraint: \"{v['hint'][:200]}\"")
                lines.append(f"- Pass rate: {v.get('pass_rate', 0):.1%}")
                lines.append(f"- Tasks: {v.get('n_pass', 0)} pass, {v.get('n_fail', 0)} fail, {v.get('n_exception', 0)} exception")
                if v.get("changes_summary"):
                    lines.append(f"- Changes: {v['changes_summary'][:300]}")
                lines.append("")

            variant_adb = prev_variant_comparison.get("variant_adb_overview", "")
            if variant_adb:
                lines.append("### Cross-Variant Debugger Analysis\n")
                lines.append(variant_adb[:3000])
                lines.append("")

            lines.append("### Lessons from Variant Comparison")
            lines.append("- Learn from BOTH variants: the losing variant may have solved tasks the winner did not")
            lines.append("- Consider combining effective parts of both approaches")
            lines.append("- Do NOT retry an approach that clearly failed in a previous variant")
            lines.append("")

    # -- Execution Instructions --
    lines.append("\n## Execution Instructions")
    lines.append("Analyze failures → group into pattern classes → design general mechanisms → implement and commit.")
    if k > 1:
        lines.append(f"**Important**: This experiment uses {k} rollouts per task. The fundamental goal is to **maximize pass@1** — the single-attempt success rate.")
        lines.append(f"**Important**: 'Partial pass' tasks are your highest-leverage targets. Compare passing vs failing rollouts of the same task to find why one succeeded and the other failed, then make the successful strategy the reliable default. This is the fastest path to higher pass@1.")
    else:
        lines.append("**Important**: Use pass rate as the optimization target. Timed-out tasks should be analyzed — understand why the agent ran out of time.")
    lines.append("**Important**: Task classification and basic diagnostics are provided above. For deeper failed task analysis, read the corresponding `agent/nexau_in_memory_tracer.cleaned.json` in the trial directory.")
    if stability and stability.get("unstable"):
        lines.append(f"**Important**: The following {len(stability['unstable'])} unstable tasks should NOT be optimization targets: {stability['unstable'][:10]}...")

    # -- Workspace Path Override (Best-of-N) --
    if workspace_path and workspace_path != "workspace":
        lines.append(f"\n## Workspace Path")
        lines.append(f"**Your workspace**: `{workspace_path}/` (use this path for ALL file operations on workspace files, git commands, and validation)")

    # -- Strategy Hint (Best-of-N) --
    if strategy_hint:
        lines.append(f"\n## ⚠️ MANDATORY Strategy Constraint for This Variant")
        lines.append(f"You are one of multiple parallel evolve agents. Each agent is assigned a different strategy direction.")
        lines.append(f"**Your assigned constraint**: {strategy_hint}")
        lines.append(f"You MUST follow this constraint. Violations will waste this variant's exploration budget.")

    return "\n".join(lines)


EVOLVE_TRACER_FLUSH_INTERVAL_SEC = 60

_evolve_tracer_states: dict[str, dict] = {}
_evolve_tracer_flush_locks: dict[str, threading.Lock] = {}
_evolve_tracer_registry_lock = threading.Lock()


def _register_tracer(key: str, agent, log_dir: Path) -> None:
    with _evolve_tracer_registry_lock:
        _evolve_tracer_states[key] = {"agent": agent, "log_dir": log_dir}
        _evolve_tracer_flush_locks.setdefault(key, threading.Lock())


def _unregister_tracer(key: str) -> None:
    with _evolve_tracer_registry_lock:
        _evolve_tracer_states.pop(key, None)
        _evolve_tracer_flush_locks.pop(key, None)


def _dump_evolve_tracer_to_disk(tracer_key: str = "default") -> None:
    """Write current InMemoryTracer snapshot to disk in cleaned format (atomic via tmp+rename)."""
    from nexau.archs.tracer.adapters.in_memory import InMemoryTracer
    from trace_converter import extract_trace_data_from_inmemory_dump

    state = _evolve_tracer_states.get(tracer_key)
    if state is None:
        return
    agent = state.get("agent")
    log_dir = state.get("log_dir")
    if agent is None or log_dir is None:
        return
    lock = _evolve_tracer_flush_locks.get(tracer_key)
    if lock is None or not lock.acquire(blocking=False):
        return
    try:
        for tracer in agent.config.tracers:
            if isinstance(tracer, InMemoryTracer):
                raw_spans = tracer.dump_traces()
                cleaned = extract_trace_data_from_inmemory_dump(
                    raw_spans,
                    include_system_prompt_message=True,
                    include_user_message=True,
                    capture_errors=True,
                    jsonable_output=True,
                )
                path = os.path.join(str(log_dir), "nexau_in_memory_tracer.cleaned.json")
                tmp_path = path + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(cleaned, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
                break
    except Exception as e:
        sys.stderr.write(f"[evolve-trace] failed to flush tracer: {e}\n")
    finally:
        lock.release()


def _periodic_evolve_tracer_flush(stop_event: threading.Event, tracer_key: str = "default") -> None:
    """Background thread: flush evolve agent tracer to disk periodically."""
    while not stop_event.wait(EVOLVE_TRACER_FLUSH_INTERVAL_SEC):
        _dump_evolve_tracer_to_disk(tracer_key)


def save_evolve_trace(agent, log_dir: Path, iteration: int,
                      tracer_key: str = "default") -> None:
    """Save the evolve agent's full conversation history and cleaned tracer to the log directory."""
    log_dir.mkdir(parents=True, exist_ok=True)
    trace_path = log_dir / "evolve_trace.json"
    try:
        trace = getattr(agent, "full_trace", None) or list(agent.history)
        messages = []
        for msg in trace:
            try:
                messages.append(msg.model_dump(mode="json"))
            except Exception:
                messages.append({"role": str(getattr(msg, "role", "unknown")),
                                 "content": str(getattr(msg, "content", ""))})
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2, default=str)
        print(f"[trace] Saved evolve agent trace ({len(messages)} messages) -> {trace_path.name}")
    except Exception as e:
        print(f"[trace] Failed to save trace: {e}")

    _dump_evolve_tracer_to_disk(tracer_key)


def run_evolve_agent(config: dict, exp_dir: Path, iteration: int,
                     query: str,
                     job_dir: Path, iteration_dir: Path) -> str:
    """Invoke NexAU evolve agent to analyze and improve workspace."""
    evolve_config_path = (exp_dir / "evolve_agent" / "evolve_agent.yaml").resolve()
    evolve_agent_dir = str((exp_dir / "evolve_agent").resolve())

    os.environ["EVOLVE_WORK_DIR"] = str(exp_dir)

    if evolve_agent_dir not in sys.path:
        sys.path.insert(0, evolve_agent_dir)

    evolve_llm = get_llm_config(config, role="evolve")
    print(
        f"[evolve] Starting evolve agent (iteration {iteration}, model={evolve_llm['model']}, "
        f"job_dir={job_dir.name})...",
        flush=True,
    )
    set_llm_env(evolve_llm)

    adb_llm = config.get("agent_debugger", {}).get("llm", {})
    if adb_llm and _ensure_adb_installed():
        adb = _adb_path or "adb"
        adb_cfg_json = json.dumps({"llm": adb_llm})
        r = subprocess.run([adb, "config", adb_cfg_json], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[adb] pre-configured LLM: model={adb_llm.get('model')}", flush=True)
        else:
            print(f"[adb] config failed: {r.stderr[:200]}", flush=True)

    from nexau import Agent

    agent = Agent.from_yaml(config_path=evolve_config_path)

    evolve_log_dir = iteration_dir / "evolve"
    evolve_log_dir.mkdir(parents=True, exist_ok=True)

    tracer_key = "default"
    _register_tracer(tracer_key, agent, evolve_log_dir)

    flush_stop = threading.Event()
    flush_thread = threading.Thread(
        target=_periodic_evolve_tracer_flush, args=(flush_stop, tracer_key), daemon=True,
    )
    flush_thread.start()

    try:
        raw_result = agent.run(
            message=query,
            context={
                "date": datetime.now().strftime("%Y-%m-%d"),
                "username": "agentic-harness-engineering",
                "working_directory": str(exp_dir),
                "workspace_path": "workspace",
                "iteration": iteration,
                "adb_llm": adb_llm if adb_llm else None,
            },
        )
    finally:
        flush_stop.set()
        flush_thread.join(timeout=2)

    if isinstance(raw_result, tuple):
        result = raw_result[0]
    else:
        result = raw_result

    save_evolve_trace(agent, evolve_log_dir, iteration, tracer_key=tracer_key)
    _unregister_tracer(tracer_key)

    print(f"[evolve] Evolve agent completed", flush=True)
    return result or ""


# ---------------------------------------------------------------------------
# Phase 3.5: Git Commit Workspace Changes
# ---------------------------------------------------------------------------

def git_tag_and_commit(workspace_dir: Path, iteration: int, evolve_result: str) -> None:
    """Tag and commit changes in workspace."""
    subprocess.run(["git", "add", "-A"], cwd=workspace_dir, check=True, capture_output=True)

    diff_result = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        cwd=workspace_dir, capture_output=True, text=True,
    )

    if not diff_result.stdout.strip():
        print(f"[git] iteration {iteration}: no file changes")
        subprocess.run(
            ["git", "tag", f"iteration_{iteration}"],
            cwd=workspace_dir, capture_output=True,
        )
        return

    summary = evolve_result[:200].replace("\n", " ").strip() if evolve_result else "auto evolution"
    commit_msg = f"iteration_{iteration}: {summary}"

    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=workspace_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "tag", f"iteration_{iteration}"],
        cwd=workspace_dir, capture_output=True,
    )

    print(f"[git] iteration {iteration}: committed and tagged")
    print(f"[git] Changes: {diff_result.stdout.strip()}")


# ---------------------------------------------------------------------------
# Phase 3-BoN: Best-of-N Parallel Exploration
# ---------------------------------------------------------------------------

def setup_variant_workspace(exp_dir: Path, iteration: int, variant_idx: int) -> Path:
    """Create a git worktree for one variant under the iteration directory.

    Returns the worktree path (which IS the variant's workspace).
    The main workspace must be in a clean git state before calling this.
    """
    main_workspace = exp_dir / "workspace"
    branch_name = f"iter{iteration}_v{variant_idx}"
    worktree_path = (
        exp_dir / "runs" / f"iteration_{iteration:03d}" / f"workspace_variant_{variant_idx}"
    )

    if worktree_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path), "--force"],
            cwd=main_workspace, capture_output=True,
        )
        subprocess.run(["git", "branch", "-D", branch_name],
                        cwd=main_workspace, capture_output=True)

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=main_workspace, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")

    print(f"[bon] Created worktree: workspace_variant_{variant_idx} (branch: {branch_name})")
    return worktree_path


def adopt_variant_winner(exp_dir: Path, iteration: int, n_variants: int,
                         winner_idx: int, winner_evolve_result: str = "") -> None:
    """Remove all worktrees, merge the winner branch into main, tag losers."""
    main_workspace = exp_dir / "workspace"
    iter_dir = exp_dir / "runs" / f"iteration_{iteration:03d}"

    for i in range(n_variants):
        wt = iter_dir / f"workspace_variant_{i}"
        if wt.exists():
            subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
            diff = subprocess.run(
                ["git", "diff", "--cached", "--quiet"], cwd=wt, capture_output=True,
            )
            if diff.returncode != 0:
                branch = f"iter{iteration}_v{i}"
                subprocess.run(
                    ["git", "commit", "-m",
                     f"auto-commit: uncommitted changes for {branch}"],
                    cwd=wt, capture_output=True,
                )

    for i in range(n_variants):
        wt = iter_dir / f"workspace_variant_{i}"
        if wt.exists():
            subprocess.run(
                ["git", "worktree", "remove", str(wt), "--force"],
                cwd=main_workspace, capture_output=True, text=True,
            )
    subprocess.run(["git", "worktree", "prune"],
                    cwd=main_workspace, capture_output=True)

    winner_branch = f"iter{iteration}_v{winner_idx}"
    summary = (winner_evolve_result[:200].replace("\n", " ").strip()
               if winner_evolve_result else "bon winner")
    merge_msg = f"iteration_{iteration}_bon_winner: {summary}"
    merge_result = subprocess.run(
        ["git", "merge", winner_branch, "-m", merge_msg],
        cwd=main_workspace, capture_output=True, text=True,
    )
    if merge_result.returncode != 0:
        subprocess.run(["git", "merge", "--abort"],
                        cwd=main_workspace, capture_output=True)
        retry = subprocess.run(
            ["git", "merge", winner_branch, "-X", "theirs", "-m", merge_msg],
            cwd=main_workspace, capture_output=True, text=True,
        )
        if retry.returncode != 0:
            raise RuntimeError(
                f"git merge failed even with -X theirs: {retry.stderr}"
            )

    subprocess.run(
        ["git", "tag", f"iteration_{iteration}"],
        cwd=main_workspace, capture_output=True,
    )

    for i in range(n_variants):
        branch = f"iter{iteration}_v{i}"
        if i != winner_idx:
            subprocess.run(
                ["git", "tag", f"iteration_{iteration}_loser_v{i}", branch],
                cwd=main_workspace, capture_output=True,
            )
        subprocess.run(["git", "branch", "-D", branch],
                        cwd=main_workspace, capture_output=True)

    winner_manifest = (iter_dir / "evolve" / f"variant_{winner_idx}"
                       / "change_manifest.json")
    if winner_manifest.exists():
        shutil.copy2(winner_manifest, exp_dir / "change_manifest.json")

    print(f"[bon] Adopted variant_{winner_idx} via git merge, tagged losers")


def _extract_evolve_changes_summary(evolve_result: str) -> str:
    """Extract a short summary of what changes were made from evolve agent output."""
    if not evolve_result:
        return "(no changes described)"
    result = evolve_result[:2000]
    lines = []
    for line in result.split("\n"):
        stripped = line.strip()
        if stripped.startswith("chg-") or stripped.startswith("- Commit:") or \
           stripped.startswith("- Files:") or stripped.startswith("- File:"):
            lines.append(stripped)
        if "Changes made" in stripped or "changes made" in stripped:
            lines.append(stripped)
    if lines:
        return "; ".join(lines[:10])
    paragraphs = result.split("\n\n")
    for p in paragraphs:
        if "change" in p.lower() or "commit" in p.lower() or "chg" in p.lower():
            return p.strip()[:500]
    return result[:500]


def save_variant_results(iteration_dir: Path, exp_dir: Path, iteration: int,
                         variant_results: list[dict],
                         winner_idx: int) -> None:
    """Save variant evolve outputs to evolve/, and selection+stats to next iteration's input/."""
    evolve_dir = iteration_dir / "evolve"

    for vr in variant_results:
        vdir = evolve_dir / f"variant_{vr['idx']}"
        vdir.mkdir(parents=True, exist_ok=True)

        worktree = vr.get("worktree_path")
        workspace_dir = vdir / "workspace"
        if not workspace_dir.exists() and worktree and Path(worktree).exists():
            shutil.copytree(worktree, workspace_dir,
                            ignore=shutil.ignore_patterns(".git"))

        if vr.get("evolve_result"):
            (vdir / "evolve_summary.md").write_text(
                f"# Variant {vr['idx']} — Evolve Agent Output\n\n"
                + (vr["evolve_result"].strip() if vr["evolve_result"] else "(no output)")
                + "\n",
                encoding="utf-8",
            )

        if vr.get("hint"):
            (vdir / "strategy_hint.txt").write_text(vr["hint"], encoding="utf-8")

    # Write selection and per-variant stats to NEXT iteration's input
    next_input = exp_dir / "runs" / f"iteration_{iteration + 1:03d}" / "input"
    next_input.mkdir(parents=True, exist_ok=True)

    for vr in variant_results:
        if vr.get("stats"):
            bm_dir = next_input / "benchmark" / f"variant_{vr['idx']}"
            bm_dir.mkdir(parents=True, exist_ok=True)
            (bm_dir / "stats.json").write_text(
                json.dumps(vr["stats"], indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )

    selection = {
        "winner_idx": winner_idx,
        "variants": [],
    }
    for vr in variant_results:
        s = vr.get("stats", {})
        selection["variants"].append({
            "idx": vr["idx"],
            "hint": vr.get("hint", ""),
            "pass_rate": s.get("pass_rate", 0),
            "n_pass": s.get("n_pass", 0),
            "n_fail": s.get("n_fail", 0),
            "n_exception": s.get("n_exception", 0),
            "changes_summary": _extract_evolve_changes_summary(vr.get("evolve_result", "")),
            "is_winner": vr["idx"] == winner_idx,
        })
    (next_input / "variant_selection.json").write_text(
        json.dumps(selection, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[bon] Saved variant results and selection info")


def select_variant_winner(variant_results: list[dict],
                          base_stats: dict | None = None) -> int:
    """Select the best variant index based on pass_rate.

    On tie, prefer lower n_exception count, then lower index.
    """
    best_idx = 0
    best_rate = -1.0
    best_exc = float("inf")
    for vr in variant_results:
        s = vr.get("stats", {})
        rate = s.get("pass_rate", 0)
        exc = s.get("n_exception", 999)
        if rate > best_rate or (rate == best_rate and exc < best_exc):
            best_rate = rate
            best_exc = exc
            best_idx = vr["idx"]

    if base_stats:
        base_rate = base_stats.get("pass_rate", 0)
        if best_rate < base_rate:
            print(f"[bon] ⚠️ Warning: best variant ({best_rate:.1%}) is worse than baseline ({base_rate:.1%})")

    return best_idx


def run_evolve_agent_on_variant(config: dict, exp_dir: Path,
                                workspace_path: str, iteration: int,
                                query: str, job_dir: Path,
                                iteration_dir: Path, variant_idx: int) -> str:
    """Run the evolve agent for a variant.

    EVOLVE_WORK_DIR stays at exp_dir so the agent can read runs/ and
    evolution_history.md normally.  The workspace_path context variable
    tells the Jinja prompt (and the query) where the variant workspace is.
    """
    evolve_config_path = (exp_dir / "evolve_agent" / "evolve_agent.yaml").resolve()
    evolve_agent_dir = str((exp_dir / "evolve_agent").resolve())

    os.environ["EVOLVE_WORK_DIR"] = str(exp_dir)

    if evolve_agent_dir not in sys.path:
        sys.path.insert(0, evolve_agent_dir)

    evolve_llm = get_llm_config(config, role="evolve")
    variant_label = f"variant_{variant_idx}"
    print(
        f"[evolve] Starting evolve agent ({variant_label}, iteration {iteration}, "
        f"model={evolve_llm['model']}, workspace={workspace_path})...",
        flush=True,
    )
    set_llm_env(evolve_llm)

    adb_llm = config.get("agent_debugger", {}).get("llm", {})
    if adb_llm and _ensure_adb_installed():
        adb = _adb_path or "adb"
        adb_cfg_json = json.dumps({"llm": adb_llm})
        r = subprocess.run([adb, "config", adb_cfg_json], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"[adb] pre-configured LLM for {variant_label}", flush=True)

    from nexau import Agent

    agent = Agent.from_yaml(config_path=evolve_config_path)

    variant_evolve_dir = iteration_dir / "evolve" / variant_label
    variant_evolve_dir.mkdir(parents=True, exist_ok=True)

    tracer_key = f"variant_{variant_idx}"
    _register_tracer(tracer_key, agent, variant_evolve_dir)

    flush_stop = threading.Event()
    flush_thread = threading.Thread(
        target=_periodic_evolve_tracer_flush, args=(flush_stop, tracer_key), daemon=True,
    )
    flush_thread.start()

    try:
        raw_result = agent.run(
            message=query,
            context={
                "date": datetime.now().strftime("%Y-%m-%d"),
                "username": "agentic-harness-engineering",
                "working_directory": str(exp_dir),
                "workspace_path": workspace_path,
                "iteration": iteration,
                "adb_llm": adb_llm if adb_llm else None,
            },
        )
    finally:
        flush_stop.set()
        flush_thread.join(timeout=2)

    if isinstance(raw_result, tuple):
        result = raw_result[0]
    else:
        result = raw_result

    save_evolve_trace(agent, variant_evolve_dir, iteration, tracer_key=tracer_key)
    _unregister_tracer(tracer_key)

    print(f"[evolve] Evolve agent completed ({variant_label})", flush=True)
    return result or ""


def run_multi_variant_adb(config: dict, variant_results: list[dict],
                          iteration_dir: Path, exp_dir: Path, iteration: int,
                          k: int = 1, winner_idx: int = 0) -> str | None:
    """Run debugger analysis across all variants, grouping traces by variant.

    For each task, collects traces from all variants and labels them with
    variant ID and architecture change description.
    Returns the overview text for injection into the next evolution query.
    """
    adb_config = config.get("agent_debugger", {})
    if not adb_config.get("enabled") or not _ensure_adb_installed():
        return None

    all_tasks: set[str] = set()
    for vr in variant_results:
        s = vr.get("stats", {})
        for task_name in s.get("task_results", {}):
            all_tasks.add(task_name)

    if not all_tasks:
        return None

    max_tasks = adb_config.get("max_tasks", 90)
    max_workers = adb_config.get("max_concurrent", 10)

    jobs: list[TaskAnalysisJob] = []
    variant_context_map: dict[str, str] = {}

    for task_name in sorted(all_tasks):
        traces: list[Path] = []
        rewards: list[float] = []
        verifier_outputs: list[str] = []
        trace_variant_labels: list[str] = []
        n_pass = n_fail = n_timeout = 0
        is_timeout = False
        all_have_cleaned = True

        for vr in variant_results:
            vidx = vr["idx"]
            job_dir_v = vr.get("job_dir")
            if not job_dir_v:
                continue
            vs = vr.get("stats", {})
            vtr = vs.get("task_results", {})
            if task_name not in vtr:
                continue

            timeout_tasks_v = vs.get("timeout_tasks", set())
            is_timeout_v = task_name in timeout_tasks_v
            if is_timeout_v:
                is_timeout = True

            if vtr[task_name] == "exception" and not is_timeout_v:
                continue

            for d in sorted(job_dir_v.iterdir()):
                if not d.is_dir():
                    continue
                base = _TRIAL_SUFFIX_RE.sub("", d.name)
                if base != task_name:
                    continue

                cleaned = d / "agent" / "nexau_in_memory_tracer.cleaned.json"
                raw = d / "agent" / "nexau_in_memory_tracer.json"
                if cleaned.exists():
                    traces.append(cleaned)
                elif raw.exists():
                    traces.append(raw)
                    all_have_cleaned = False
                else:
                    continue

                reward_path = d / "verifier" / "reward.txt"
                if reward_path.exists():
                    try:
                        rv = float(reward_path.read_text().strip())
                    except ValueError:
                        rv = 0.0
                else:
                    rv = -1.0 if is_timeout_v else 0.0

                rewards.append(rv)
                if rv >= 1.0:
                    n_pass += 1
                elif rv < 0:
                    n_timeout += 1
                else:
                    n_fail += 1

                label = "PASS" if rv >= 1.0 else ("TIMEOUT" if rv < 0 else "FAIL")
                trace_variant_labels.append(f"variant_{vidx}:{label}")

                test_stdout = d / "verifier" / "test-stdout.txt"
                if test_stdout.exists():
                    try:
                        text = test_stdout.read_text(errors="replace").strip()
                        if len(text) > 4000:
                            text = "... (truncated) ...\n" + text[-4000:]
                        verifier_outputs.append(text)
                    except OSError:
                        verifier_outputs.append("")
                else:
                    verifier_outputs.append("")

        if not traces:
            continue

        variant_context_map[task_name] = "\n".join(
            f"  trace{i+1:02d} = {lbl}" for i, lbl in enumerate(trace_variant_labels)
        )

        trace_type = None if all_have_cleaned else "in_memory_tracer"

        jobs.append(TaskAnalysisJob(
            task_name=task_name,
            trace_paths=traces,
            trace_rewards=rewards,
            verifier_outputs=verifier_outputs,
            n_pass=n_pass,
            n_fail=n_fail,
            n_timeout=n_timeout,
            is_timeout=is_timeout,
            mode="debug" if (n_fail > 0 or n_timeout > 0) else "summary",
            trace_type=trace_type,
        ))

    jobs.sort(key=lambda j: (j.mode == "summary", -(j.n_fail + j.n_timeout)))
    jobs = jobs[:max_tasks]

    if not jobs:
        return None

    variant_header_parts = []
    for vr in variant_results:
        vidx = vr["idx"]
        hint = vr.get("hint", "no specific strategy")
        changes = _extract_evolve_changes_summary(vr.get("evolve_result", ""))
        vs = vr.get("stats", {})
        rate = vs.get("pass_rate", 0)
        variant_header_parts.append(
            f"Variant {vidx} (strategy: {hint[:80]}): pass_rate={rate:.1%}, changes: {changes[:200]}"
        )
    variant_header = (
        f"This task was evaluated under {len(variant_results)} different agent architecture variants.\n"
        + "\n".join(variant_header_parts)
    )

    print(f"[bon-adb] Analyzing {len(jobs)} tasks across {len(variant_results)} variants "
          f"with {max_workers} workers")

    def _run_one(job: TaskAnalysisJob) -> dict:
        ctx = variant_context_map.get(job.task_name, "")
        extra_prefix = (
            f"\n{variant_header}\n\n"
            f"Trace-to-variant mapping for {job.task_name}:\n{ctx}\n\n"
            f"Compare across variants: which variant's approach was more effective and why?\n"
        )
        original_job = TaskAnalysisJob(
            task_name=job.task_name,
            trace_paths=job.trace_paths,
            trace_rewards=job.trace_rewards,
            trial_dirs=job.trial_dirs,
            verifier_outputs=job.verifier_outputs,
            n_pass=job.n_pass,
            n_fail=job.n_fail,
            n_timeout=job.n_timeout,
            is_timeout=job.is_timeout,
            mode=job.mode,
            trace_type=job.trace_type,
        )
        result = _run_single_adb_ask(original_job, adb_config, k=k,
                                      extra_query_prefix=extra_prefix)
        result["variant_context"] = ctx
        return result

    first_result = _run_one(jobs[0])
    status = "ok" if not first_result["response"].startswith("[adb") else "err"
    print(f"  [{status}] {jobs[0].task_name} [warmup]")
    results: list[dict] = [first_result]

    if len(jobs) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {pool.submit(_run_one, job): job for job in jobs[1:]}
            for future in concurrent.futures.as_completed(future_map):
                job = future_map[future]
                try:
                    r = future.result()
                    results.append(r)
                    status = "ok" if not r["response"].startswith("[adb") else "err"
                    print(f"  [{status}] {job.task_name}")
                except Exception as e:
                    print(f"  [exc] {job.task_name}: {e}")
                    results.append({
                        "task_name": job.task_name,
                        "mode": job.mode,
                        "n_pass": job.n_pass,
                        "n_fail": job.n_fail,
                        "n_timeout": job.n_timeout,
                        "is_timeout": job.is_timeout,
                        "response": f"[adb exception] {e}",
                        "trace_paths": [str(p) for p in job.trace_paths],
                        "trace_rewards": list(job.trace_rewards),
                        "verifier_outputs": list(job.verifier_outputs),
                    })

    results.sort(key=lambda r: (r["mode"] == "summary", -r["n_fail"]))

    next_input = exp_dir / "runs" / f"iteration_{iteration + 1:03d}" / "input"
    analyse_dir = next_input / "analysis"
    detail_dir = analyse_dir / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    body_lines = [
        f"# Cross-Variant Debugger Analysis — Iteration {iteration}\n",
        f"Analyzed {len(results)} tasks across {len(variant_results)} architecture variants.\n",
    ]
    for vr in variant_results:
        vidx = vr["idx"]
        hint = vr.get("hint", "no specific strategy")
        vs = vr.get("stats", {})
        rate = vs.get("pass_rate", 0)
        marker = " **[WINNER]**" if vidx == winner_idx else ""
        body_lines.append(f"- **Variant {vidx}**{marker}: pass_rate={rate:.1%}, strategy: {hint[:100]}")
    body_lines.append("")

    debug_results = [r for r in results if r["mode"] == "debug" and not r.get("is_timeout")]
    timeout_results = [r for r in results if r.get("is_timeout")]
    summary_results = [r for r in results if r["mode"] == "summary"]

    if timeout_results:
        body_lines.append("### Timeout\n")
        for r in timeout_results:
            one_liner = _extract_one_liner(r["response"])
            body_lines.append(f"- **{r['task_name']}**: {one_liner}")
        body_lines.append("")

    if debug_results:
        body_lines.append("### Debug (has failures)\n")
        for r in debug_results:
            one_liner = _extract_one_liner(r["response"])
            body_lines.append(f"- **{r['task_name']}** ({r['n_pass']} pass, {r['n_fail']} fail): {one_liner}")
        body_lines.append("")

    if summary_results:
        names = ", ".join(r["task_name"] for r in summary_results)
        body_lines.append(f"### Summary (all pass): {len(summary_results)} tasks\n")
        body_lines.append(f"{names}\n")
        body_lines.append("(All rollouts passed — no per-task analysis included. Read `detail/{{task}}.md` if needed.)\n")

    overview_text = "\n".join(body_lines)
    (analyse_dir / "overview.md").write_text(overview_text, encoding="utf-8")

    for r in results:
        n_total = r["n_pass"] + r["n_fail"] + r.get("n_timeout", 0)
        ctx = r.get("variant_context", "")
        detail_content = (
            f"# {r['task_name']} ({r['n_pass']}/{n_total} pass)\n\n"
            f"## Variant-Trace Mapping\n\n{ctx}\n\n"
            f"## Analysis\n\n{r['response']}\n"
        )
        trace_section = "\n## Trace Paths\n\n"
        for idx, tp in enumerate(r.get("trace_paths", [])):
            rv = r["trace_rewards"][idx] if idx < len(r.get("trace_rewards", [])) else 0
            label = "PASS" if rv >= 1.0 else ("TIMEOUT" if rv < 0 else "FAIL")
            trace_section += f"- trace{idx+1:02d} ({label}): `{tp}`\n"
        detail_content += trace_section
        (detail_dir / f"{r['task_name']}.md").write_text(detail_content, encoding="utf-8")

    print(f"[bon-adb] Wrote cross-variant analysis to {analyse_dir.relative_to(iteration_dir.parent.parent)}/")
    return overview_text


def run_best_of_n_evolution(
    config: dict,
    exp_dir: Path,
    iteration: int,
    iteration_dir: Path,
    stats: dict,
    job_dir: Path,
    prev_stats: dict | None,
    diff: dict | None,
    stability: dict | None,
    best_ever: dict | None,
    scores_trend: list[dict] | None,
    change_eval: dict | None,
    adb_overview: str | None,
    prev_variant_comparison: dict | None,
    agent_config_filename: str,
) -> dict:
    """Orchestrate Best-of-N parallel exploration using git worktrees.

    1. Create N git worktrees (one per variant)
    2. Concurrently run N evolve agents (each writes to its own worktree)
    3. Parallel Harbor evaluation for all variants
    4. Select winner, merge into main, tag losers
    5. Cross-variant debugger analysis
    """
    bon_config = config.get("best_of_n", {})
    n = bon_config.get("n", 2)
    hints = bon_config.get("strategy_hints", [])
    k = int(config.get("harbor", {}).get("k", 1))
    job_timeout = int(config.get("harbor_job_timeout_minutes") or 0)
    _bon_phase_start = time.monotonic()

    print(f"\n[bon] === Best-of-{n} Evolution (iteration {iteration}) ===")

    main_workspace = exp_dir / "workspace"
    subprocess.run(["git", "add", "-A"], cwd=main_workspace, capture_output=True)
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=main_workspace, capture_output=True,
    )
    if diff_check.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"pre-bon snapshot iteration {iteration}"],
            cwd=main_workspace, capture_output=True,
        )

    # Phase 3a: Create worktrees (sequential), then run N evolve agents concurrently
    variant_prep: list[dict] = []
    for i in range(n):
        worktree_path = setup_variant_workspace(exp_dir, iteration, i)
        workspace_rel = str(worktree_path.relative_to(exp_dir))
        hint = hints[i] if i < len(hints) else None

        query = build_evolution_query(
            iteration=iteration,
            stats=stats,
            job_dir=job_dir,
            iteration_dir=iteration_dir,
            prev_stats=prev_stats,
            diff=diff,
            stability=stability,
            best_ever=best_ever,
            scores_trend=scores_trend,
            change_evaluation=change_eval,
            adb_overview=adb_overview,
            strategy_hint=hint,
            prev_variant_comparison=prev_variant_comparison,
            workspace_path=workspace_rel,
        )

        variant_prep.append({
            "idx": i,
            "worktree_path": worktree_path,
            "workspace_rel": workspace_rel,
            "hint": hint,
            "query": query,
        })

    # Pre-configure adb once before concurrent agents to avoid duplicate pip installs
    adb_llm = config.get("agent_debugger", {}).get("llm", {})
    if adb_llm:
        _ensure_adb_installed()

    n_concurrent = config["harbor"]["n_concurrent"]
    per_variant_concurrent = max(1, n_concurrent // n)

    _evolve_done = [0]
    _eval_launched = [0]
    _progress_lock = threading.Lock()
    _bon_start = time.monotonic()
    # Collect harbor launch info from all variant threads (thread-safe list)
    _harbor_launches: list[tuple[subprocess.Popen, str, Path, dict]] = []

    def _run_variant(vp: dict) -> dict:
        """Run evolve agent, then immediately launch harbor evaluation (pipeline)."""
        idx = vp["idx"]
        variant_label = f"variant_{idx}"
        t0 = time.monotonic()

        evolve_result = run_evolve_agent_on_variant(
            config, exp_dir, vp["workspace_rel"], iteration, vp["query"],
            job_dir, iteration_dir, variant_idx=idx,
        )

        evolve_elapsed = time.monotonic() - t0
        with _progress_lock:
            _evolve_done[0] += 1
            done = _evolve_done[0]
        print(
            f"[bon] {variant_label} evolution finished ({done}/{n} done, "
            f"elapsed {evolve_elapsed / 60:.1f} min)",
            flush=True,
        )

        # Copy change manifest to evolve dir
        evolve_vdir = iteration_dir / "evolve" / variant_label
        evolve_vdir.mkdir(parents=True, exist_ok=True)
        manifest_dst = evolve_vdir / "change_manifest.json"
        variant_manifest = vp["worktree_path"] / "change_manifest.json"
        shared_manifest = exp_dir / "change_manifest.json"
        if variant_manifest.exists():
            shutil.copy2(variant_manifest, manifest_dst)
        elif shared_manifest.exists():
            try:
                shutil.copy2(shared_manifest, manifest_dst)
            except FileNotFoundError:
                pass

        # Save evolved workspace snapshot
        snap = evolve_vdir / "workspace"
        if not snap.exists():
            variant_ws = vp["worktree_path"]
            shutil.copytree(variant_ws, snap, ignore=shutil.ignore_patterns(".git"))

        # Launch harbor evaluation — results go to NEXT iteration's input/benchmark/
        next_bm_dir = exp_dir / "runs" / f"iteration_{iteration + 1:03d}" / "input" / "benchmark" / variant_label
        next_bm_dir.mkdir(parents=True, exist_ok=True)

        variant_ws = vp["worktree_path"]
        proc, started_after = launch_harbor(
            config, variant_ws, agent_config_filename, next_bm_dir,
            label=variant_label,
            n_concurrent_override=per_variant_concurrent,
        )
        with _progress_lock:
            _eval_launched[0] += 1
            launched = _eval_launched[0]
        print(
            f"[bon] {variant_label} evaluation launched ({launched}/{n} launched)",
            flush=True,
        )

        result = {
            "idx": idx,
            "worktree_path": vp["worktree_path"],
            "workspace_rel": vp["workspace_rel"],
            "evolve_result": evolve_result,
            "hint": vp["hint"],
        }

        with _progress_lock:
            _harbor_launches.append((proc, started_after, next_bm_dir, result))

        return result

    print(f"[bon] Launching {n} evolve agents concurrently...", flush=True)
    variant_results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        future_map = {pool.submit(_run_variant, vp): vp["idx"] for vp in variant_prep}
        for future in concurrent.futures.as_completed(future_map):
            idx = future_map[future]
            try:
                variant_results.append(future.result())
            except Exception as e:
                print(f"[bon] ERROR: variant_{idx} evolve agent failed: {e}", flush=True)
                vp = next(v for v in variant_prep if v["idx"] == idx)
                variant_results.append({
                    "idx": idx,
                    "worktree_path": vp["worktree_path"],
                    "workspace_rel": vp["workspace_rel"],
                    "evolve_result": "",
                    "hint": vp["hint"],
                })
    variant_results.sort(key=lambda vr: vr["idx"])

    total_elapsed = time.monotonic() - _bon_start
    print(
        f"[bon] All {n} evolve agents completed in {total_elapsed / 60:.1f} min, "
        f"{len(_harbor_launches)}/{n} evaluations already running",
        flush=True,
    )

    # Phase 3b: Wait for all harbor evaluations (already launched in pipeline above)
    print(f"[bon] Waiting for {len(_harbor_launches)} parallel Harbor evaluations...", flush=True)
    eval_start = time.monotonic()

    for proc, started_after, bm_dir, vr in _harbor_launches:
        try:
            job_dir_v = wait_for_harbor(
                proc, bm_dir, started_after,
                timeout_minutes=job_timeout,
                label=f"variant_{vr['idx']}",
            )
            variant_stats = compute_stats(job_dir_v, k=k)
            vr["job_dir"] = job_dir_v
            vr["stats"] = variant_stats
            rate = variant_stats["pass_rate"]
            print(f"[bon] variant_{vr['idx']}: pass_rate={rate:.1%} "
                  f"({variant_stats['n_pass']}/{variant_stats['n_pass'] + variant_stats['n_fail'] + variant_stats['n_exception']})",
                  flush=True)
        except HarborJobTimeoutError as e:
            print(f"[bon] variant_{vr['idx']} evaluation failed: {e}", flush=True)
            vr["job_dir"] = None
            vr["stats"] = {"pass_rate": 0, "n_pass": 0, "n_fail": 0,
                           "n_exception": 0, "task_results": {}}

    eval_elapsed = time.monotonic() - eval_start
    print(
        f"[bon] All evaluations completed (wait phase: {eval_elapsed / 60:.1f} min, "
        f"total: {(time.monotonic() - _bon_start) / 60:.1f} min)",
        flush=True,
    )

    # Phase 3c: Select winner
    valid_variants = [vr for vr in variant_results if vr.get("job_dir") is not None]
    if not valid_variants:
        print("[bon] ERROR: All variants failed evaluation, keeping current workspace")
        save_variant_results(iteration_dir, exp_dir, iteration, variant_results, winner_idx=0)
        for i in range(n):
            wt = iteration_dir / f"workspace_variant_{i}"
            if wt.exists():
                subprocess.run(
                    ["git", "worktree", "remove", str(wt), "--force"],
                    cwd=main_workspace, capture_output=True,
                )
            subprocess.run(["git", "branch", "-D", f"iter{iteration}_v{i}"],
                            cwd=main_workspace, capture_output=True)
        subprocess.run(["git", "worktree", "prune"],
                        cwd=main_workspace, capture_output=True)
        return {"winner": variant_results[0], "all_variants": variant_results,
                "variant_adb": None, "winner_idx": 0,
                "timing_min": round((time.monotonic() - _bon_phase_start) / 60, 1)}

    winner_idx = select_variant_winner(valid_variants, base_stats=stats)
    winner = next(vr for vr in variant_results if vr["idx"] == winner_idx)
    winner_stats = winner.get("stats", {})
    print(f"\n[bon] Winner: variant_{winner_idx} (pass_rate={winner_stats.get('pass_rate', 0):.1%})")

    for vr in variant_results:
        if vr["idx"] != winner_idx:
            vs = vr.get("stats", {})
            print(f"[bon] Loser:  variant_{vr['idx']} (pass_rate={vs.get('pass_rate', 0):.1%})")

    # Phase 3d: Save all variant results (before removing worktrees)
    save_variant_results(iteration_dir, exp_dir, iteration, variant_results, winner_idx)

    # Phase 3e: Adopt winner — remove worktrees, merge winner, tag losers
    adopt_variant_winner(
        exp_dir, iteration, n, winner_idx,
        winner_evolve_result=winner.get("evolve_result", ""),
    )

    # Phase 3f: Cross-variant debugger analysis → next iteration's input/analysis/
    variant_adb = run_multi_variant_adb(
        config, variant_results, iteration_dir, exp_dir, iteration, k=k,
        winner_idx=winner_idx,
    )

    bon_total_min = (time.monotonic() - _bon_phase_start) / 60
    print(f"[bon] Best-of-{n} total: {bon_total_min:.1f} min", flush=True)

    return {
        "winner": winner,
        "winner_idx": winner_idx,
        "all_variants": variant_results,
        "variant_adb": variant_adb,
        "timing_min": round(bon_total_min, 1),
    }


def load_prev_variant_comparison(exp_dir: Path, current_iteration: int) -> dict | None:
    """Load variant selection results from the current iteration's input (produced by previous iteration)."""
    if current_iteration <= 1:
        return None
    cur_input = exp_dir / "runs" / f"iteration_{current_iteration:03d}" / "input"
    selection_path = cur_input / "variant_selection.json"
    if not selection_path.exists():
        prev_iter_dir = exp_dir / "runs" / f"iteration_{current_iteration - 1:03d}"
        selection_path = prev_iter_dir / "variant_selection.json"
    if not selection_path.exists():
        return None
    try:
        data = json.loads(selection_path.read_text(encoding="utf-8"))
        for candidate in [
            cur_input / "analysis" / "overview.md",
            exp_dir / "runs" / f"iteration_{current_iteration - 1:03d}" / "variant_debugger_analyse" / "overview.md",
        ]:
            if candidate.exists():
                data["variant_adb_overview"] = candidate.read_text(encoding="utf-8")
                break
        return data
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Post-Evolve: Validation Phase After Evolution
# ---------------------------------------------------------------------------

def run_post_evolve(config: dict, exp_dir: Path, workspace_dir: Path,
                    agent_config_filename: str) -> None:
    """After evolution, run harbor evaluation for each post_evolve dataset."""
    post_cfg = config.get("post_evolve", {})
    if not post_cfg.get("enabled"):
        return

    datasets = post_cfg.get("datasets", [])
    if not datasets:
        return

    post_dir = exp_dir / "post_evolve"
    post_dir.mkdir(exist_ok=True)

    for ds in datasets:
        ds_name = ds["name"] if isinstance(ds, dict) else ds
        n_concurrent = ds.get("n_concurrent", config["harbor"]["n_concurrent"]) if isinstance(ds, dict) else config["harbor"]["n_concurrent"]

        print(f"\n[post_evolve] Starting validation: {ds_name} (n_concurrent={n_concurrent})")

        ds_dir = post_dir / ds_name.replace("@", "_")
        ds_dir.mkdir(exist_ok=True)

        post_config = copy.deepcopy(config)
        post_config["dataset"] = ds_name
        post_config["harbor"]["n_concurrent"] = n_concurrent

        job_dir = run_harbor(post_config, workspace_dir, agent_config_filename, ds_dir)
        post_k = int(post_config.get("harbor", {}).get("k", 1))
        post_stats = compute_stats(job_dir, k=post_k)
        pass_rate = post_stats["pass_rate"]
        n_pass = post_stats["n_pass"]
        n_total = post_stats["n_total"]

        if post_k > 1:
            trial_stats = post_stats.get("trial_stats", {})
            pak_data = post_stats.get("pass_at_k", {})
            pak_rates = pak_data.get("pass_at", {}) if pak_data else {}
            pak_str = " | ".join(f"pass@{i}={pak_rates[i]:.1%}" for i in sorted(pak_rates)) if pak_rates else ""
            summary = (
                f"# {ds_name} Validation Results (k={post_k})\n\n"
                f"- {pak_str}\n"
                f"- Tasks: {n_pass} all-pass / {n_total} total\n"
                f"- Trials: {trial_stats.get('n_pass', '?')} pass / {trial_stats.get('n_total', '?')} total\n"
                f"- Job: {job_dir.name}\n"
            )
            print(f"[post_evolve] {ds_name}: {pak_str} | tasks: {n_pass} all-pass / {n_total}")
        else:
            summary = (
                f"# {ds_name} Validation Results\n\n"
                f"- Pass rate: {pass_rate:.1%} ({n_pass}/{n_total})\n"
                f"- Job: {job_dir.name}\n"
            )
            print(f"[post_evolve] {ds_name}: {pass_rate:.1%} ({n_pass}/{n_total})")
        (ds_dir / "summary.md").write_text(summary, encoding="utf-8")


# ---------------------------------------------------------------------------
# Explore-Agent Parallel Support
# ---------------------------------------------------------------------------

def _run_explore_agent_standalone(config: dict, exp_dir: Path) -> None:
    """Run explore-agent standalone in skip_eval mode (synchronous)."""
    from explore_agent.run import run_explore_agent, register_explore_agent_skills

    evolve_llm = get_llm_config(config, role="evolve")
    ml_model = config.get("explore_agent", {}).get("model") or evolve_llm["model"]
    os.environ["EXPLORE_AGENT_MODEL"] = ml_model
    os.environ["EXPLORE_AGENT_WORK_DIR"] = str(exp_dir)

    ml_agent_patch = build_explore_agent_patch(config)
    if ml_agent_patch:
        config = deep_merge(config, {"explore_agent_patch": ml_agent_patch})

    print(f"\n[explore-agent] Starting explore-agent (model={ml_model})...")
    ml_success = run_explore_agent(config, exp_dir)
    if ml_success:
        print("[explore-agent] Done, skills ready")
    else:
        print("[explore-agent] Warning: not all skills were fully produced")
    register_explore_agent_skills(exp_dir)


def _run_harbor_with_explore_agent(
    config: dict,
    exp_dir: Path,
    workspace_dir: Path,
    agent_config_filename: str,
    jobs_dir: Path,
) -> Path:
    """Run harbor eval + explore-agent in parallel, return job_dir.

    Both are fully independent: explore-agent doesn't need eval results, eval doesn't need explore-agent output.
    Evolve agent only starts after both complete (by then explore-agent skills are available).
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
    from explore_agent.run import run_explore_agent, register_explore_agent_skills

    ml_timeout = config.get("explore_agent", {}).get("timeout_minutes", 30)
    ml_start_time = time.monotonic()

    agent_llm = get_llm_config(config, role="agent")
    evolve_llm = get_llm_config(config, role="evolve")
    ml_model = config.get("explore_agent", {}).get("model") or evolve_llm["model"]

    set_llm_env(agent_llm)
    os.environ["EXPLORE_AGENT_MODEL"] = ml_model
    os.environ["EXPLORE_AGENT_WORK_DIR"] = str(exp_dir)

    ml_agent_patch = build_explore_agent_patch(config)
    if ml_agent_patch:
        config = deep_merge(config, {"explore_agent_patch": ml_agent_patch})

    print(f"\n[parallel] Starting harbor eval + explore-agent in parallel (ml_model={ml_model}, timeout={ml_timeout}min)")

    pool = ThreadPoolExecutor(max_workers=2)
    harbor_future = pool.submit(
        run_harbor, config, workspace_dir, agent_config_filename, jobs_dir,
    )
    ml_future = pool.submit(run_explore_agent, config, exp_dir)

    job_dir = harbor_future.result()

    ml_elapsed = time.monotonic() - ml_start_time
    ml_remaining = max(0, ml_timeout * 60 - ml_elapsed)
    try:
        ml_success = ml_future.result(timeout=ml_remaining)
        if ml_success:
            print("[explore-agent] Done, skills ready")
        else:
            print("[explore-agent] Warning: not all skills produced, continuing with existing skills")
    except FuturesTimeoutError:
        print(f"[explore-agent] Warning: timeout ({ml_timeout}min), continuing with existing skills")
    except Exception as e:
        print(f"[explore-agent] Error: {e}, continuing with existing skills")

    register_explore_agent_skills(exp_dir)
    pool.shutdown(wait=False, cancel_futures=True)

    return job_dir


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def run_single_experiment(config: dict, config_path: str, experiment_name: str | None = None,
                          start_iteration: int = 1, skip_eval: bool = False) -> None:
    """Run the full evolution loop for a single experiment."""
    source_dir = resolve_source_dir(config)
    agent_config_filename = config["agent_config_filename"]
    target_pass_rate = config["target_pass_rate"]
    max_iterations = config["max_iterations"]
    code_agent_patch = config.get("code_agent_patch", {})
    evolve_agent_patch = build_evolve_agent_patch(config.get("evolve_agent", {}))

    # Phase 0: Create/Resume experiment directory
    exp_dir = create_experiment_dir(config, config_path, experiment_name=experiment_name)
    workspace_dir = exp_dir / "workspace"

    is_new = init_workspace(source_dir, workspace_dir)

    # Phase 0.05: On resume, restore workspace and experiment metadata to the start iteration's state
    if not is_new and start_iteration > 1:
        snapshot_dir = exp_dir / "runs" / f"iteration_{start_iteration:03d}" / "input" / "workspace"
        if snapshot_dir.exists():
            print(f"[resume] Restoring workspace to iteration {start_iteration} snapshot ...")
            perform_auto_rollback(exp_dir, workspace_dir, start_iteration)
        else:
            print(f"[resume] No snapshot found for iteration {start_iteration}, using current workspace state")
        rollback_experiment_metadata(exp_dir, start_iteration)

    # Phase 0.1: Apply patches
    if is_new:
        # Fresh init: apply full patches (tool_call_mode, api_type, reasoning, etc.)
        apply_code_agent_patch(workspace_dir, agent_config_filename, code_agent_patch)
        apply_agent_yaml_patch(exp_dir / "evolve_agent" / "evolve_agent.yaml", evolve_agent_patch, label="evolve_agent_patch")
    else:
        # Resume: re-apply LLM connection config from the current CLI yaml
        # to keep api_key/base_url/model in sync when the user switches providers.
        # code_agent.yaml uses ${env.LLM_*} so it picks up changes via set_llm_env();
        # evolve_agent.yaml stores values directly, so we must patch it explicitly.
        _llm_keys = ("api_key", "base_url", "model")

        _evolve_llm = get_llm_config(config, role="evolve")
        _evolve_llm_patch = {k: v for k, v in _evolve_llm.items() if k in _llm_keys and v}
        if _evolve_llm_patch:
            apply_agent_yaml_patch(
                exp_dir / "evolve_agent" / "evolve_agent.yaml",
                {"llm_config": _evolve_llm_patch},
                label="resume_evolve_agent_llm",
            )

    experiment_timeout = int(config.get("experiment_timeout_minutes") or 0)
    experiment_start = time.monotonic()

    llm_cfg = get_llm_config(config, role="agent")
    evolve_llm_cfg = get_llm_config(config, role="evolve")
    meta_name = config.get("_meta", {}).get("_name", "")
    exp_k = int(config.get("harbor", {}).get("k", 1))
    print(f"\n{'='*60}")
    print(f"Agentic Harness Engineering Automated Evolution System")
    print(f"Experiment directory: {exp_dir.name}")
    if meta_name:
        print(f"Experiment name: {meta_name}")
    print(f"Target pass rate: {target_pass_rate:.0%}")
    print(f"Max iterations: {max_iterations}")
    if exp_k > 1:
        print(f"Rollout k: {exp_k}")
    print(f"Data source: {config.get('dataset') or config.get('path', 'unspecified')}")
    print(f"Agent model: {llm_cfg['model']}")
    print(f"Evolve model: {evolve_llm_cfg['model']}")
    bon_cfg = config.get("best_of_n", {})
    if bon_cfg.get("enabled"):
        print(f"Best-of-N: {bon_cfg.get('n', 2)} parallel variants per iteration")
    if experiment_timeout > 0:
        print(f"Experiment timeout: {experiment_timeout} min")
    print(f"Per-evaluation timeout: {config.get('harbor_job_timeout_minutes', 0) or 'unlimited'} min")
    print(f"{'='*60}\n")

    bon_info = f"\n**Best-of-N**: {bon_cfg.get('n', 2)} variants" if bon_cfg.get("enabled") else ""
    send_feishu_notification(config, "Experiment Started", (
        f"**Target pass rate**: {target_pass_rate:.0%}\n"
        f"**Max iterations**: {max_iterations}\n"
        f"**Rollout k**: {exp_k}\n" +
        f"**Data source**: {config.get('dataset') or config.get('path', 'unspecified')}\n"
        f"**Agent model**: {llm_cfg['model']}\n"
        f"**Evolve model**: {evolve_llm_cfg['model']}" + bon_info
    ))

    def check_experiment_timeout():
        if experiment_timeout > 0:
            elapsed = (time.monotonic() - experiment_start) / 60
            if elapsed > experiment_timeout:
                raise ExperimentTimeoutError(
                    f"Experiment timeout: ran {elapsed:.1f} min, exceeded limit of {experiment_timeout} min"
                )

    bon_enabled = config.get("best_of_n", {}).get("enabled", False)
    _bon_prev_winner: dict | None = None

    try:
        for iteration in range(start_iteration, max_iterations + 1):
            check_experiment_timeout()

            _iter_start = time.monotonic()
            _iter_timing: dict[str, float] = {}

            print(f"\n{'='*60}")
            print(f"  Iteration {iteration}/{max_iterations}")
            if bon_enabled:
                print(f"  Mode: Best-of-{config['best_of_n'].get('n', 2)}")
            if experiment_timeout > 0:
                elapsed = (time.monotonic() - experiment_start) / 60
                print(f"  Running for {elapsed:.1f}/{experiment_timeout} min")
            print(f"{'='*60}\n")

            subprocess.run(
                ["git", "tag", f"iteration_{iteration}_before"],
                cwd=workspace_dir, capture_output=True,
            )

            iteration_dir = exp_dir / "runs" / f"iteration_{iteration:03d}"
            iteration_dir.mkdir(parents=True, exist_ok=True)

            input_dir = iteration_dir / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            snapshot_dir = input_dir / "workspace"
            if not snapshot_dir.exists():
                shutil.copytree(workspace_dir, snapshot_dir, ignore=shutil.ignore_patterns(".git"))
                print(f"[snapshot] Backed up workspace to {snapshot_dir.relative_to(exp_dir)}")
            else:
                print(f"[snapshot] Workspace snapshot already exists, skipping")

            # Phase 1: Evaluation
            _phase1_start = time.monotonic()
            # When Best-of-N is enabled and we have a winner from the previous iteration,
            # skip the base eval and reuse the winner's stats/job_dir directly.
            reusing_bon_winner = False
            if bon_enabled and _bon_prev_winner is not None:
                print(f"[eval] Reusing previous iteration's Best-of-N winner results (skipping base eval)")
                job_dir = _bon_prev_winner["job_dir"]
                stats = _bon_prev_winner["stats"]
                reusing_bon_winner = True
            else:
                ml_enabled = config.get("explore_agent", {}).get("enabled", False)
                benchmark_dir = input_dir / "benchmark"
                benchmark_dir.mkdir(parents=True, exist_ok=True)
                try:
                    if skip_eval:
                        job_dir = find_latest_job_dir(benchmark_dir)
                        if job_dir is None:
                            job_dir = find_latest_job_dir(iteration_dir)
                        if job_dir is None:
                            job_dir = find_latest_job_dir(ROOT_DIR / "jobs")
                        if job_dir is None:
                            print("[error] --skip-eval but no job directory found")
                            sys.exit(1)
                        print(f"[eval] Skipping evaluation, using existing results: {job_dir.name}")
                        skip_eval = False

                        if iteration == 1 and ml_enabled:
                            from explore_agent.run import register_explore_agent_skills, ML_SKILL_NAMES
                            skills_dir = exp_dir / "evolve_agent" / "skills"
                            existing = [s for s in ML_SKILL_NAMES if (skills_dir / s / "SKILL.md").exists()]
                            if existing:
                                print(f"[explore-agent] Skills already exist ({len(existing)}/{len(ML_SKILL_NAMES)}), skipping re-run")
                                register_explore_agent_skills(exp_dir)
                            else:
                                _run_explore_agent_standalone(config, exp_dir)

                    elif iteration == 1 and ml_enabled:
                        job_dir = _run_harbor_with_explore_agent(
                            config, exp_dir, workspace_dir, agent_config_filename, benchmark_dir,
                        )
                    else:
                        job_dir = run_harbor(config, workspace_dir, agent_config_filename, benchmark_dir)
                except HarborJobTimeoutError as e:
                    print(f"\n[timeout] {e}")
                    print(f"[timeout] No available evaluation results, skipping this iteration")
                    continue

                # Phase 2: Statistics
                k = int(config.get("harbor", {}).get("k", 1))
                stats = compute_stats(job_dir, k=k)

            _iter_timing["eval_min"] = round((time.monotonic() - _phase1_start) / 60, 1)
            _phase2_start = time.monotonic()

            k = int(config.get("harbor", {}).get("k", 1))
            pass_rate = stats["pass_rate"]
            n_pass = stats["n_pass"]
            n_total = stats["n_total"]

            # Phase 2.4: Task stability tracking
            task_history = update_task_history(exp_dir, iteration, stats["task_results"],
                                              per_task_rollouts=stats.get("per_task_rollouts"))
            stability = compute_task_stability(task_history)

            # Load previous iteration results from task_history for diff, avoiding re-computing stats
            prev_stats = None
            prev_task_results = None
            prev_rollouts = None
            if iteration > 1:
                prev_task_results = {}
                prev_rollouts = {}
                for task_name, entries in task_history.items():
                    for entry in entries:
                        if entry[0] == iteration - 1:
                            prev_task_results[task_name] = entry[1]
                            if len(entry) >= 3:
                                prev_rollouts[task_name] = entry[2]
                if not prev_task_results:
                    prev_task_results = None
                    prev_rollouts = None
                else:
                    if not prev_rollouts:
                        prev_rollouts = None
                    scores_path = exp_dir / "iteration_scores.yaml"
                    if scores_path.exists():
                        with open(scores_path, encoding="utf-8") as f:
                            scores_data = yaml.safe_load(f) or {}
                        for s in scores_data.get("scores", []):
                            if s["iteration"] == iteration - 1:
                                prev_stats = {
                                    "pass_rate": s.get("pass_rate", 0),
                                }
                                break

            diff = compute_iteration_diff(stats["task_results"], prev_task_results,
                                          current_rollouts=stats.get("per_task_rollouts"),
                                          prev_rollouts=prev_rollouts)

            update_history_before(exp_dir, iteration, stats, job_dir, diff=diff)

            # Phase 2.7: Change attribution evaluation (report only, rollback decided by evolve agent)
            change_eval = None
            if iteration > 1 and diff:
                prev_manifest = load_change_manifest(exp_dir, iteration - 1)
                if prev_manifest:
                    print(f"\n[attribution] Found iteration {iteration-1} change_manifest, performing change attribution evaluation...")
                    change_eval = evaluate_changes(prev_manifest, diff, stats["task_results"])
                    save_change_evaluation(exp_dir, iteration, change_eval)

                    print(f"[attribution] Attribution summary: {change_eval['summary']}")
                    if change_eval.get("unattributed_regressions"):
                        print(f"[attribution] Warning: unattributed regressions: {change_eval['unattributed_regressions']}")
                else:
                    print(f"[attribution] No change_manifest.json found for iteration {iteration-1}, skipping change attribution")

            # Phase 2.8: Update best-ever
            best_ever = update_best_ever(exp_dir, iteration, stats)

            target_metric = stats["pass_rate"]
            if target_metric >= target_pass_rate:
                _iter_timing["total_min"] = round((time.monotonic() - _iter_start) / 60, 1)
                update_iteration_scores(exp_dir, config, iteration, pass_rate, n_pass, n_total, job_dir,
                                        n_exception=stats["n_exception"],
                                        stats=stats, timing=_iter_timing)
                print(f"\n{'='*60}")
                print(f"  Target achieved! Pass rate: {target_metric:.1%} >= {target_pass_rate:.0%}")
                print(f"{'='*60}\n")
                if k > 1:
                    _pak_data = stats.get("pass_at_k", {})
                    _pak_rates = _pak_data.get("pass_at", {}) if _pak_data else {}
                    _pak_str = " | ".join(f"pass@{i}={_pak_rates[i]:.1%}" for i in sorted(_pak_rates)) if _pak_rates else ""
                    target_body = (
                        f"**Iteration**: {iteration}/{max_iterations}\n"
                        f"**Pass@1**: {target_metric:.1%} >= target {target_pass_rate:.0%}\n"
                        f"**Pass@k**: {_pak_str}\n"
                        f"**Tasks**: {n_pass} all-pass / {n_total} total"
                    )
                else:
                    target_body = (
                        f"**Iteration**: {iteration}/{max_iterations}\n"
                        f"**Pass rate**: {target_metric:.1%} >= target {target_pass_rate:.0%}\n"
                        f"**Tasks**: {n_pass} pass / {n_total} total"
                    )
                send_feishu_notification(config, "Target Achieved!", target_body)
                break

            check_experiment_timeout()

            # Load historical trends
            scores_trend = None
            scores_path = exp_dir / "iteration_scores.yaml"
            if scores_path.exists():
                with open(scores_path, encoding="utf-8") as f:
                    scores_data = yaml.safe_load(f) or {}
                scores_trend = scores_data.get("scores", [])

            # Phase 2.5a: Agent Debugger QA analysis
            adb_overview = None
            adb_config = config.get("agent_debugger", {})
            if adb_config.get("enabled") and not reusing_bon_winner:
                existing_overview = iteration_dir / "input" / "analysis" / "overview.md"
                if existing_overview.exists():
                    overview_text = existing_overview.read_text(encoding="utf-8")
                    lines_raw = overview_text.split("\n", 1)
                    adb_overview = lines_raw[1].strip() if len(lines_raw) > 1 else overview_text.strip()
                    print(f"[adb] Reusing existing analysis from {existing_overview.relative_to(exp_dir)}")
                else:
                    print(
                        f"[adb] Starting agent debugger phase "
                        f"(iteration {iteration}, job_dir={job_dir.name})",
                        flush=True,
                    )
                    adb_overview = run_parallel_adb_ask(
                        config=adb_config,
                        job_dir=job_dir,
                        task_results=stats["task_results"],
                        iteration_dir=iteration_dir,
                        iteration=iteration,
                        timeout_tasks=stats.get("timeout_tasks"),
                        k=k,
                    )

            _iter_timing["analysis_min"] = round((time.monotonic() - _phase2_start) / 60, 1)
            _phase3_start = time.monotonic()

            # Phase 3: Evolution
            if bon_enabled:
                prev_variant_comparison = load_prev_variant_comparison(exp_dir, iteration)

                bon_result = run_best_of_n_evolution(
                    config=config,
                    exp_dir=exp_dir,
                    iteration=iteration,
                    iteration_dir=iteration_dir,
                    stats=stats,
                    job_dir=job_dir,
                    prev_stats=prev_stats,
                    diff=diff,
                    stability=stability,
                    best_ever=best_ever,
                    scores_trend=scores_trend,
                    change_eval=change_eval,
                    adb_overview=adb_overview,
                    prev_variant_comparison=prev_variant_comparison,
                    agent_config_filename=agent_config_filename,
                )

                evolve_result = bon_result["winner"].get("evolve_result", "")
                winner_stats = bon_result["winner"].get("stats", stats)

                save_evolve_summary(iteration_dir, iteration, evolve_result)
                update_history_after(exp_dir, iteration, evolve_result)
                archive_change_manifest(exp_dir, iteration)

                _bon_prev_winner = {
                    "job_dir": bon_result["winner"].get("job_dir", job_dir),
                    "stats": winner_stats,
                }

                pass_rate = winner_stats.get("pass_rate", pass_rate)
                n_pass = winner_stats.get("n_pass", n_pass)

            else:
                query = build_evolution_query(
                    iteration=iteration,
                    stats=stats,
                    job_dir=job_dir,
                    iteration_dir=iteration_dir,
                    prev_stats=prev_stats,
                    diff=diff,
                    stability=stability,
                    best_ever=best_ever,
                    scores_trend=scores_trend,
                    change_evaluation=change_eval,
                    adb_overview=adb_overview,
                )
                print(
                    f"[evolve] Phase 3: evolution (iteration {iteration}, job_dir={job_dir.name})",
                    flush=True,
                )
                evolve_result = run_evolve_agent(config, exp_dir, iteration, query, job_dir, iteration_dir)

                save_evolve_summary(iteration_dir, iteration, evolve_result)
                update_history_after(exp_dir, iteration, evolve_result)
                git_tag_and_commit(workspace_dir, iteration, evolve_result)
                archive_change_manifest(exp_dir, iteration)

                _bon_prev_winner = None

            _iter_timing["evolve_min"] = round((time.monotonic() - _phase3_start) / 60, 1)
            _iter_timing["total_min"] = round((time.monotonic() - _iter_start) / 60, 1)

            # Build variant info for scores
            _bon_variants_info = None
            if bon_enabled:
                try:
                    _bon_variants_info = []
                    for vr in bon_result.get("all_variants", []):
                        vs = vr.get("stats", {})
                        _bon_variants_info.append({
                            "idx": vr["idx"],
                            "pass_rate": round(vs.get("pass_rate", 0), 4),
                            "n_pass": vs.get("n_pass", 0),
                            "n_fail": vs.get("n_fail", 0),
                            "n_exception": vs.get("n_exception", 0),
                            "winner": vr["idx"] == bon_result.get("winner_idx", 0),
                        })
                except Exception:
                    _bon_variants_info = None

            # Update scores with timing and variant info
            update_iteration_scores(exp_dir, config, iteration, pass_rate, n_pass, n_total, job_dir,
                                    n_exception=stats["n_exception"],
                                    stats=stats,
                                    timing=_iter_timing,
                                    bon_variants=_bon_variants_info)

            _timing_parts = []
            for phase_name, phase_key in [("eval", "eval_min"), ("analysis", "analysis_min"), ("evolve", "evolve_min")]:
                if phase_key in _iter_timing:
                    _timing_parts.append(f"{phase_name}={_iter_timing[phase_key]:.0f}m")
            _timing_str = f" [{', '.join(_timing_parts)}, total={_iter_timing['total_min']:.0f}m]"

            if k > 1:
                pass_at_k_data = stats.get("pass_at_k", {})
                pass_at_rates = pass_at_k_data.get("pass_at", {}) if pass_at_k_data else {}
                pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates)) if pass_at_rates else ""
                print(f"\n[done] Iteration {iteration} complete, pass rate: {pass_rate:.1%} | {pak_str}{_timing_str}")
            else:
                print(f"\n[done] Iteration {iteration} complete, pass rate: {pass_rate:.1%}{_timing_str}")

            if k > 1:
                pass_at_k_data = stats.get("pass_at_k", {})
                pass_at_rates = pass_at_k_data.get("pass_at", {}) if pass_at_k_data else {}
                pak_str = " | ".join(f"pass@{i}={pass_at_rates[i]:.1%}" for i in sorted(pass_at_rates)) if pass_at_rates else ""
                feishu_body = (
                    f"**Iteration**: {iteration}/{max_iterations}\n"
                    f"**Pass@1**: {pass_rate:.1%} | Target: {target_pass_rate:.0%}\n"
                    f"**Pass@k**: {pak_str}\n"
                    f"**Tasks**: {n_pass} all-pass / {n_total} total (k={k})"
                )
            else:
                feishu_body = (
                    f"**Iteration**: {iteration}/{max_iterations}\n"
                    f"**Pass rate**: {pass_rate:.1%} ({n_pass}/{n_total})\n"
                    f"**Target**: {target_pass_rate:.0%}"
                )
            send_feishu_notification(config, f"Iteration {iteration} Complete", feishu_body)

    except ExperimentTimeoutError as e:
        elapsed = (time.monotonic() - experiment_start) / 60
        print(f"\n{'='*60}")
        print(f"  Experiment timeout! Ran for {elapsed:.1f} min")
        print(f"  {e}")
        print(f"{'='*60}\n")
        send_feishu_notification(config, "Experiment Timeout", (
            f"**Ran for**: {elapsed:.1f} min\n"
            f"**Limit**: {experiment_timeout} min\n"
            f"**Reason**: {e}"
        ))

    # Post-evolve validation
    run_post_evolve(config, exp_dir, workspace_dir, agent_config_filename)

    # Final summary
    scores_path = exp_dir / "iteration_scores.yaml"
    final_summary = ""
    if scores_path.exists():
        with open(scores_path, encoding="utf-8") as f:
            scores_data = yaml.safe_load(f) or {}
        scores = scores_data.get("scores", [])
        if scores:
            last = scores[-1]
            best = max(scores, key=lambda s: s.get("pass_rate", 0))
            last_pak = last.get("pass_at", {})
            best_pak = best.get("pass_at", {})
            if last_pak:
                last_pak_str = " | ".join(f"pass@{i}={last_pak[i]:.1%}" if isinstance(i, int) else f"pass@{i}={last_pak[i]:.1%}" for i in sorted(last_pak, key=lambda x: int(x)))
                best_pak_str = " | ".join(f"pass@{i}={best_pak[i]:.1%}" if isinstance(i, int) else f"pass@{i}={best_pak[i]:.1%}" for i in sorted(best_pak, key=lambda x: int(x))) if best_pak else ""
                final_summary = (
                    f"**Total iterations**: {len(scores)}\n"
                    f"**Final**: {last_pak_str}\n"
                    f"**Best**: {best_pak_str} (iteration {best.get('iteration', '?')})"
                )
            else:
                final_summary = (
                    f"**Total iterations**: {len(scores)}\n"
                    f"**Final pass rate**: {last.get('pass_rate', 0):.1%}\n"
                    f"**Best pass rate**: {best.get('pass_rate', 0):.1%} (iteration {best.get('iteration', '?')})"
                )
    send_feishu_notification(config, "Experiment Ended", final_summary or "Experiment completed")

    history_path = exp_dir / "evolution_history.md"
    if history_path.exists():
        print(f"\nEvolution history: {history_path}")

    git_log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=workspace_dir, capture_output=True, text=True,
    )
    if git_log.stdout:
        print(f"\nWorkspace git history:")
        print(git_log.stdout)


def run_batch(config_paths: list[str]) -> None:
    """Launch multiple experiments in batch, each in an independent tmux session."""
    for config_path in config_paths:
        config = load_config(config_path)
        meta_name = config.get("_meta", {}).get("_name", Path(config_path).stem)
        session_name = f"ahe-{meta_name}"

        cmd = f"cd {PROJECT_DIR} && python evolve.py --config {Path(config_path).resolve()}"
        tmux_cmd = ["tmux", "new-session", "-d", "-s", session_name, cmd]

        print(f"[batch] Launching experiment '{meta_name}' -> tmux session '{session_name}'")
        subprocess.run(tmux_cmd, check=True)

    print(f"\n[batch] Launched {len(config_paths)} experiments")
    print(f"[batch] View: tmux ls")
    print(f"[batch] Attach: tmux attach -t <session_name>")


def main():
    parser = argparse.ArgumentParser(description="Agentic Harness Engineering Automated Evolution Evaluation System")
    parser.add_argument(
        "--config", default=None,
        help="Config file path (base.yaml or overlay file with _base)",
    )
    parser.add_argument(
        "--batch", nargs="*", default=None,
        help="Batch mode: pass a directory or multiple overlay files, each experiment runs in an independent tmux session",
    )
    parser.add_argument(
        "--experiment", type=str, default=None,
        help="Resume existing experiment (pass directory name under experiments/)",
    )
    parser.add_argument(
        "--start-iteration", type=int, default=1,
        help="Start from which iteration (for resuming interrupted runs)",
    )
    parser.add_argument(
        "--skip-eval", action="store_true",
        help="Skip evaluation, use latest job results (for debugging)",
    )
    args = parser.parse_args()

    # --batch mode
    if args.batch is not None:
        paths = args.batch
        if not paths:
            paths = [str(PROJECT_DIR / "configs" / "experiments")]

        config_files = []
        for p in paths:
            p = Path(p)
            if p.is_dir():
                config_files.extend(sorted(p.glob("*.yaml")))
            else:
                config_files.append(p)

        if not config_files:
            print("[batch] No config files found")
            sys.exit(1)

        run_batch([str(f) for f in config_files])
        return

    # Single experiment mode
    if not args.config:
        parser.error("Single experiment mode requires --config argument")
    config = load_config(args.config)
    run_single_experiment(
        config=config,
        config_path=args.config,
        experiment_name=args.experiment,
        start_iteration=args.start_iteration,
        skip_eval=args.skip_eval,
    )


if __name__ == "__main__":
    main()
