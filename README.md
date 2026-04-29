# Agentic Harness Engineering: Self-Evolving Coding Agents

<div align="left">

<p align="left" style="display:flex; gap:18px;">
  <a href="LICENSE" target="_blank" style="margin-right:0;">
    <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg">
  </a>
  <a href="https://www.python.org/downloads/" target="_blank" style="margin-right:0;">
    <img alt="Python" src="https://img.shields.io/badge/python-%E2%89%A53.13-blue.svg">
  </a>
  <a href="https://docs.astral.sh/uv/" target="_blank" style="margin-right:0;">
    <img alt="Managed with uv" src="https://img.shields.io/badge/managed_with-uv-261230?logo=python&logoColor=white">
  </a>
</p>

</div>

---

## 📰 News

- **[2026-01-16]** 📄 Paper released on arXiv: [OctoBench: Benchmarking Scaffold-Aware Instruction Following in Repository-Grounded Agentic Coding](https://arxiv.org/abs/2604.25850)
- **[2026-01]** 🎉 Dataset & Framework released

---

## 🎯 Overview

**AHE (Agentic Harness Engineering)** is an open **observability system** for automatically evolving the harness around a coding agent. The base model is held fixed; what evolves are the harness components — system prompts, tool descriptions, tool implementations, middleware, skills, sub-agents, and long-term memory.

AHE rests on three observability layers:

- **Component observability** — [**NexAU**](https://github.com/nex-agi/NexAU.git) decomposes the harness into seven orthogonal, file-level components, each git-tracked so every edit is auditable and revertible.
- **Experience observability** — *Agent Debugger* distills ~10M-token raw traces into layered, sourced reports; the optimizer reads digests by default but can always drill back to any rollout's raw trace.
- **Decision observability** — *Evolve Agent* proposes evidence-backed edits, predicts their impact, and is automatically falsified by the next iteration's flipped tasks.

> **Note on Agent Debugger licensing.** The current release ships a *partially* open-sourced Agent Debugger; due to company strategy, it cannot be fully open-sourced at this time.

Each outer loop runs `evaluate → analyze → improve`: the current harness is benchmarked via `harbor`, the resulting traces are distilled, then *Evolve Agent* rewrites whichever components the evidence points at — until a target pass rate or iteration cap is reached.

---

## 🚀 Quick Start

### 0. Prerequisites

- Python ≥ 3.13
- [uv](https://docs.astral.sh/uv/)
- tmux

```bash
# macOS
brew install uv tmux

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt install -y tmux
```

### 1. Clone + install dependencies

```bash
git clone https://github.com/Curry09/agentic-harness-engineering.git
cd agentic-harness-engineering
uv sync
```

> `uv sync` installs every dependency declared in `pyproject.toml`, including the private repositories `NexAU` and `harbor-LJH`. Your `GITHUB_TOKEN` must have pull access to both.

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`. At minimum, set:

| Variable | Purpose |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` | Main LLM endpoint (`code_agent` and `evolve_agent` both consume it) |
| `E2B_API_KEY` | E2B sandbox — see the next subsection for SaaS vs. self-hosted |
| `GITHUB_TOKEN` | Required for private deps (`NexAU`, `harbor-LJH`) and internal harbor operations |
| `SERPER_API_KEY` | Web search used by `evolve_agent` |

`ADB_LLM_*` and `GPT54_LLM_*` are optional — leave them unset to fall back to `LLM_*`, or set them to point ADB / the gpt-5.4 experiment at a stronger model. `LANGFUSE_*`, `BP_HTML_PARSER_*`, and `FEISHU_WEBHOOK` are all optional observability / convenience hooks; see `.env.example` for the full list.

#### E2B sandbox: SaaS vs. self-hosted

AHE runs every rollout inside an E2B sandbox. Two deployment modes are supported:

- **SaaS E2B (default).** Set **only** `E2B_API_KEY` and leave `E2B_API_URL` / `E2B_DOMAIN` unset (or commented out). The SDK talks to `e2b.dev` automatically.

  > ⚠️ **Concurrency cap.** SaaS E2B enforces a per-account **concurrent sandbox limit** tied to your tier. If harbor tries to spawn more sandboxes than the cap allows, the extra sandboxes fail to start and the iteration stalls. Before raising parallelism in your harbor / experiment config, check your tier's quota and stay safely under it.

- **Self-hosted E2B cluster.** Set `E2B_API_KEY` **and** point the SDK at your cluster:

  ```dotenv
  E2B_API_KEY="your_e2b_key"
  E2B_API_URL="https://your-e2b-host.example.com"
  E2B_DOMAIN="your-e2b-host.example.com"
  ```

  No shared concurrency cap applies, but the cluster's hardware capacity still does.

### 3. Build E2B templates (one-time per dataset)

Every rollout runs inside an E2B sandbox spawned from a prebuilt template that already has `uv` and the NexAU/harbor venv at `/opt/nexau-venv`. Build those templates once before launching:

```bash
# Build every template declared by the dataset, 16 in parallel
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset -j 16

# Resume after a failure: only retry tasks whose latest E2B build status is ERROR
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset --retry-failed

# Build a specific subset of tasks
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset task_a task_b
```

The dataset directory must contain one subdir per task with a `task.toml` declaring `[environment].docker_image` (or an `environment/Dockerfile` fallback). Each task's template alias is `<task_name>` with `.` replaced by `-`.

The default packages baked into each template come from `scripts/build_templates.py:DEFAULT_NEXAU_PACKAGES` (a public NexAU + the in-sandbox `NexAU-harbor` variant, intentionally distinct from the host-side `harbor-LJH` in `pyproject.toml`). Override with one or more `--nexau-package <git-or-pip-spec>` flags if you need a different revision in the sandbox.

If your tasks pull from a private Docker registry, also export `DOCKER_REGISTRY_USERNAME` and `DOCKER_REGISTRY_PASSWORD` before invoking the script.

### 4. Launch

```bash
# Run a single experiment in the background via tmux
./scripts/evolve.sh configs/experiments/exp-003-simple-code-gpt54.yaml

# Launch and auto-attach to the log stream
./scripts/evolve.sh --attach configs/experiments/exp-003-simple-code-gpt54.yaml

# Batch: launch every experiment under configs/experiments/
./scripts/evolve.sh --batch
```

Common tmux operations after launch:

```bash
tmux ls                         # list sessions
tmux attach -t <session>        # attach to a session
# Ctrl-b d                      # detach (keeps running in background)
tmux kill-session -t <session>  # terminate
```

---

## 🔧 How It Works

The core is an **evaluate → analyze → improve** loop:

1. **Evaluate**: use `harbor` to run the current agent against the dataset.
2. **Analyze**: collect failing traces, summaries, and metrics.
3. **Improve**: `evolve_agent` uses that evidence to modify the `code_agent`'s prompts, tool descriptions, and workflow.
4. **Loop**: return to step 1 until `target_pass_rate` or `max_iterations` is reached.

### Main components

| Component | Role |
|---|---|
| `evolve.py` | Main-loop orchestrator |
| `agents/code_agent_simple/` | The coding agent that is being evaluated and evolved |
| `agents/evolve_agent/` | The meta-agent that performs the improvement step (built on the [NexAU](https://github.com/nex-agi/NexAU.git) framework) |
| `agents/explore_agent/` | Upstream dataset / source-code exploration agent |
| `configs/` | `base.yaml` (shared defaults) + `experiments/` (per-experiment overlays) |
| `scripts/` | tmux launcher wrappers (`evolve.sh`, `evolve-resume.sh`) |

### Directory layout

```
agentic-harness-engineering/
├── evolve.py                       # main loop
├── trace_converter.py              # rollout trace → debugger-friendly JSON
├── agents/
│   ├── code_agent_simple/          # the coding agent under evolution
│   ├── evolve_agent/               # the evolution meta-agent
│   │   ├── evolve_prompt.md
│   │   ├── middleware/             # context compaction / failover / ralph loop …
│   │   ├── skills/                 # agent-debugger-cli / nexau-evolution-guide
│   │   └── tools/                  # file / shell / web / session tools
│   └── explore_agent/              # exploration agent (sources + web)
├── configs/
│   ├── base.yaml                   # shared defaults
│   └── experiments/                # one overlay per experiment
├── scripts/
│   ├── evolve.sh                   # tmux launcher
│   └── evolve-resume.sh            # resume helper
└── .env.example
```

---

## Configuration (base + overlay)

`configs/base.yaml` holds the shared defaults. Each `configs/experiments/exp-*.yaml` inherits it via a leading `_base: ../base.yaml` line and overrides only the fields that differ. Any `${ENV_NAME}` reference inside a YAML file is substituted from `.env`.

**Key fields in `base.yaml`:**

| Field | Description |
|---|---|
| `path` | Dataset path |
| `target_pass_rate` | Stop once reached (default 0.95) |
| `max_iterations` | Maximum number of iterations (default 100) |
| `harbor_job_timeout_minutes` | Per-harbor-evaluation timeout (0 = unlimited) |
| `experiment_timeout_minutes` | Total wall-clock budget for the experiment (0 = unlimited) |
| `llm.api_key / base_url / model` | Main LLM config (usually left as `${LLM_*}`) |
| `agent_debugger.llm` | Dedicated LLM for ADB (can use a stronger model for debugging) |
| `notify.feishu_webhook` | Optional Feishu webhook for experiment milestones |

### Dataset configuration

An experiment's data source is specified via `path` **or** `dataset` — pick one:

| Form | Meaning | Example |
|---|---|---|
| `path: "./dataset/xxx"` | Local dataset directory (relative to the AHE root) | `./dataset/terminal-bench-2` |
| `path: "/abs/path/xxx"` | Local dataset directory (absolute path) | `/root/dataset/terminal-bench-2` |
| `dataset: "<name>@<ver>"` | Reference a harbor built-in dataset (no local files required) | `terminal-bench@2.0` |

The default `path` values in `base.yaml` and `configs/experiments/*.yaml` are **placeholders only** — adjust them for your environment, or comment out `path` and uncomment the `dataset` line to use a harbor built-in dataset instead.

---

## CLI reference

### `python evolve.py`

| Flag | Description |
|---|---|
| `--config <file>` | Config file (overlay takes precedence) |
| `--batch [dir\|files...]` | Batch mode; defaults to scanning `configs/experiments/` |
| `--experiment <name>` | Resume an existing experiment (pass the directory name under `experiments/`) |
| `--start-iteration N` | Start from iteration N (default 1) |
| `--skip-eval` | Skip evaluation and reuse existing rollouts (for debugging) |

### `./scripts/evolve.sh`

A thin wrapper around `uv run python evolve.py` + tmux.

| Flag | Description |
|---|---|
| `<config_file>` | Positional argument: path to the config file |
| `--experiment <name>` | Resume an existing experiment |
| `--start-iteration N` | Starting iteration |
| `--skip-eval` | Skip evaluation |
| `--session <name>` | Custom tmux session name |
| `--batch` | Launch every overlay in batch mode |
| `--attach` | Auto-attach after launch |

---

## Common scenarios

**Resume an interrupted experiment from iteration 16:**

```bash
./scripts/evolve.sh \
  --experiment 2026-04-10__23-20-14__gpt54 \
  --start-iteration 16 \
  configs/experiments/exp-003-simple-code-gpt54.yaml
```

**Run only evolve_agent without re-running evaluation:**

```bash
./scripts/evolve.sh \
  --experiment <existing-exp-dir> \
  --skip-eval \
  configs/experiments/exp-003-simple-code-gpt54.yaml
```

---

## License

MIT
