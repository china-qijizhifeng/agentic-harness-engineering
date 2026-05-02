<p align="right">
  <a href="README.md">English</a> | 简体中文
</p>

# Agentic Harness Engineering：以可观测性驱动的编码 Agent Harness 自动演化

<div align="left">

<p align="left">
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-yellow.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-%E2%89%A53.13-blue.svg">
  <img alt="Managed with uv" src="https://img.shields.io/badge/managed_with-uv-261230?logo=python&logoColor=white">
</p>

</div>

<p align="center">
  <img src="assets/figures/case_study.png" alt="Case Study" width="44%">
  <img src="assets/figures/training_curve.png" alt="Training Curve" width="54%">
</p>

> 本文档为英文 [README.md](README.md) 的中文翻译，可能略有滞后；如有冲突以英文版为准。

---

## 📰 动态

- **[2026-04-30]** ✍️ Dawning Road 上的博客（英文 & 中文）—— 关于 AHE 探索过程的更详细记述：[Agentic Harness Engineering](https://dawning-road.github.io/blog/agentic-harness-engineering)
- **[2026-04-28]** 📄 论文已在 arXiv 发布：[Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses](https://arxiv.org/abs/2604.25850)
- **[2026-04]** 🎉 框架开源

---

## 🎯 概览

**AHE (Agentic Harness Engineering)** 是一个开放的**可观测性系统**，用于自动演化围绕在编码 agent 周围的 harness。基础模型保持不变，演化的是 harness 的各个组件——系统提示词、工具描述、工具实现、中间件、skill、子 agent、以及长期记忆。

AHE 建立在三层可观测性之上：

- **组件可观测性 (Component observability)** —— [**NexAU**](https://github.com/nex-agi/NexAU.git) 把 harness 拆分为七个正交的文件级组件，每一项都纳入 git 追踪，因此每次修改都可审计、可回退。
- **经验可观测性 (Experience observability)** —— *Agent Debugger* 把 ~10M-token 的原始 trace 蒸馏成分层、可溯源的报告；优化器默认读 digest，但任何论断都可以下钻回某次 rollout 的原始 trace。
- **决策可观测性 (Decision observability)** —— *Evolve Agent* 提出有证据支撑的修改、预测其影响，并由下一轮迭代中翻转的任务自动证伪。

经过十轮 `评估 → 分析 → 改进` 迭代，**AHE** 把 Terminal-Bench 2 的 pass@1 从 **69.7% 提升到 77.0%**，超过手写的 Codex (71.9%) 以及自演化的 ACE 与 TF-GRPO 基线；同时产出了一个无需重新演化即可迁移到 SWE-bench-verified 以及四个其他基础模型上的"冻结 harness"，表明被演化出的组件编码的是通用工程经验，而非针对单一 benchmark 的调优。

<p align="center">
  <img src="assets/figures/transfer_model.png" alt="Cross-Model Transfer" width="60%">
</p>

---

## 🚀 快速开始

### 0. 前置依赖

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

### 1. 克隆 + 安装依赖

```bash
git clone https://github.com/Curry09/agentic-harness-engineering.git
cd agentic-harness-engineering
uv sync
```

> `uv sync` 会安装 `pyproject.toml` 中声明的所有依赖。

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，至少需要设置：

| 变量 | 用途 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` | 主 LLM 端点（`code_agent` 与 `evolve_agent` 都消费它） |
| `E2B_API_KEY` | [E2B](https://e2b.dev/) 沙箱——SaaS 与自部署的差异详见下一小节 |
| `SERPER_API_KEY` | `evolve_agent` 使用的 web 搜索 |

`ADB_LLM_*` 与 `GPT54_LLM_*` 是可选项——不设置时回退到 `LLM_*`，或可用它们让 ADB / gpt-5.4 实验指向更强的模型。`LANGFUSE_*`、`BP_HTML_PARSER_*` 与 `FEISHU_WEBHOOK` 全部是可选的可观测性 / 便利性钩子；完整列表见 `.env.example`。

#### E2B 沙箱：SaaS 与自部署

AHE 每一次 rollout 都跑在 E2B 沙箱里，支持两种部署模式：

- **SaaS E2B（默认）。** **只**设置 `E2B_API_KEY`，不要设置 `E2B_API_URL` / `E2B_DOMAIN`（注释掉即可）。SDK 会自动连到 `e2b.dev`。

  > ⚠️ **并发上限。** SaaS E2B 对每个账号有按 tier 划分的**并发沙箱上限**。如果 harbor 试图启动超出该上限的沙箱，多余的会启动失败，整轮迭代会卡住。在调高 harbor / 实验配置中的并行度之前，请先确认你的 tier 配额并保持安全余量。

- **自部署 E2B 集群。** 设置 `E2B_API_KEY` **并**让 SDK 指向你的集群：

  ```dotenv
  E2B_API_KEY="your_e2b_key"
  E2B_API_URL="https://your-e2b-host.example.com"
  E2B_DOMAIN="your-e2b-host.example.com"
  ```

  没有共享并发上限，但仍受集群硬件容量约束。

### 3. 构建 E2B 模板（每个数据集一次性）

这里使用的数据集来自 [`laude-institute/harbor-datasets`](https://github.com/laude-institute/harbor-datasets) 的子集——克隆你需要的部分，然后让 `--dataset-dir` 指向它的目录。

每次 rollout 都从一个预构建的 E2B 模板里启动沙箱，模板里已经装好了 `uv` 以及位于 `/opt/nexau-venv` 的 NexAU/harbor venv。在启动前一次性构建好这些模板：

```bash
# 构建数据集声明的所有模板，并发 16
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset -j 16

# 失败后续跑：仅重试当前 E2B 构建状态为 ERROR 的任务
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset --retry-failed

# 只构建某些任务
uv run python scripts/build_templates.py --dataset-dir /path/to/dataset task_a task_b
```

数据集目录下每个任务必须是一个子目录，里面有声明 `[environment].docker_image` 的 `task.toml`（或 `environment/Dockerfile` 作为 fallback）。每个任务的模板别名是 `<task_name>`，把 `.` 替换为 `-`。

每个模板默认烤进去的包列表来自 `scripts/build_templates.py:DEFAULT_NEXAU_PACKAGES`（一个公开的 NexAU + 沙箱内部使用的 `NexAU-harbor` 变体，故意区别于 `pyproject.toml` 中宿主侧的 `harbor-LJH`）。如果需要在沙箱里换一个版本，可用一个或多个 `--nexau-package <git-or-pip-spec>` 参数覆盖。

如果任务镜像来自私有 Docker registry，调用前还需要 export `DOCKER_REGISTRY_USERNAME` 和 `DOCKER_REGISTRY_PASSWORD`。

### 4. 启动

```bash
# 通过 tmux 在后台运行单个实验
./scripts/evolve.sh configs/experiments/exp-003-simple-code-gpt54.yaml

# 启动并自动 attach 到日志流
./scripts/evolve.sh --attach configs/experiments/exp-003-simple-code-gpt54.yaml

# 批量：启动 configs/experiments/ 下的所有实验
./scripts/evolve.sh --batch
```

启动后常用的 tmux 操作：

```bash
tmux ls                         # 列出会话
tmux attach -t <session>        # 接入某个会话
# Ctrl-b d                      # 分离（会话仍在后台运行）
tmux kill-session -t <session>  # 终止
```

---

## 🔧 工作原理

基础模型保持不变，演化的是**它周围的 harness**。每一轮外层循环都是 `评估 → 分析 → 改进`，构建在概览中介绍的三层可观测性之上。

### 1. 评估 (Evaluate) —— 输出的不只是分数，更是 trace

`harbor` 在隔离的 E2B 沙箱里把当前的 `code_agent` 跑一遍数据集。每个任务会写出：

- `agent/nexau_in_memory_tracer.cleaned.json` —— 完整的 step 级 trace（消息、工具调用、中间件事件）
- `agent/nexau.txt` —— 运行时日志（中间件错误、崩溃、警告）
- `verifier/reward.txt` —— pass / fail 结果

之后所有步骤操作的单位是 **trace，而不是 pass rate**。

### 2. 分析 (Analyze) —— 把 ~10M-token 的 trace 蒸馏成可溯源证据

*Agent Debugger* 把每一轮迭代的原始 trace（动辄 10M+ token）压缩成分层报告：

- `analysis/overview.md` —— 跨任务的根因汇总
- `analysis/detail/{task}.md` —— 每个任务的深度分析

优化器默认读 digest，但每一条结论都能反向链接到原始 trace，因此在落地修改前可以一路下钻。

> **关于 Agent Debugger 的开源说明。** 当前版本中 Agent Debugger 是**部分开源**的；出于公司战略原因，目前无法完全开源。

### 3. 改进 (Improve) —— 有证据支撑、可证伪的修改

*Evolve Agent* 只能写 `workspace/` 内部，那里暴露了 NexAU 的七个组件：`systemprompt.md`、`code_agent.yaml`、`tool_descriptions/`、`tools/`、`middleware/`、`skills/`、`sub_agents/`（外加 `LongTermMEMORY.md`）。每一次修改必须提交四个字段：

1. **失败证据 (Failure evidence)** —— 触发本次修改的失败任务和 trace 片段
2. **根因 (Root cause)** —— *为什么*失败，而不仅仅是*失败了什么*
3. **针对性修复 (Targeted fix)** —— 直接对准上述根因的改动
4. **预测影响 (Predicted impact)** —— 哪些任务应当翻转为 pass，哪些可能受到风险

### 4. 循环 (Loop) —— 错峰世代让证伪成为可能

每个 `runs/iteration_NNN/` 同时混着两代：`input/` 是上一轮 `NNN-1` 产出的 workspace（刚被评估完），`evolve/` 是本轮 `NNN` 写出的内容（在下一轮被评估）。下一次评估中出现的翻转（pass↔fail）会在 `change_evaluation.json` 中归因回本轮的修改——预测不成立的会被回滚或修订。循环在达到 `target_pass_rate` 或 `max_iterations` 时终止。

### 主要组件

| 组件 | 作用 |
|---|---|
| `evolve.py` | 主循环编排器 |
| `agents/code_agent_simple/` | 被评估和被演化的编码 agent |
| `agents/evolve_agent/` | 执行改进步骤的元 agent（基于 [NexAU](https://github.com/nex-agi/NexAU.git) 框架构建） |
| `agents/explore_agent/` | 上游数据集 / 源码探索 agent |
| `configs/` | `base.yaml`（共享默认值）+ `experiments/`（按实验覆盖） |
| `scripts/` | tmux 启动脚本（`evolve.sh`、`evolve-resume.sh`） |

### 目录结构

```
agentic-harness-engineering/
├── evolve.py                       # 主循环
├── trace_converter.py              # rollout trace → debugger 友好的 JSON
├── agents/
│   ├── code_agent_simple/          # 被演化的编码 agent
│   ├── evolve_agent/               # 演化元 agent
│   │   ├── evolve_prompt.md
│   │   ├── middleware/             # 上下文压缩 / failover / ralph 循环 …
│   │   ├── skills/                 # agent-debugger-cli / nexau-evolution-guide
│   │   └── tools/                  # 文件 / shell / web / 会话工具
│   └── explore_agent/              # 探索 agent（源码 + web）
├── configs/
│   ├── base.yaml                   # 共享默认值
│   └── experiments/                # 每个实验一个 overlay
├── scripts/
│   ├── evolve.sh                   # tmux 启动器
│   └── evolve-resume.sh            # 续跑辅助
└── .env.example
```

---

## 配置（base + overlay）

`configs/base.yaml` 保存共享默认值。每个 `configs/experiments/exp-*.yaml` 通过开头的 `_base: ../base.yaml` 继承它，并只覆盖需要修改的字段。YAML 中的任何 `${ENV_NAME}` 引用会从 `.env` 中替换。

**`base.yaml` 关键字段：**

| 字段 | 描述 |
|---|---|
| `path` | 数据集路径 |
| `target_pass_rate` | 达到后即停（默认 0.95） |
| `max_iterations` | 最大迭代轮数（默认 100） |
| `harbor_job_timeout_minutes` | 单次 harbor 评测的超时（0 = 不限） |
| `experiment_timeout_minutes` | 整个实验的总 wall-clock 预算（0 = 不限） |
| `llm.api_key / base_url / model` | 主 LLM 配置（通常保留 `${LLM_*}`） |
| `agent_debugger.llm` | 给 ADB 专用的 LLM（debug 时可换更强的模型） |
| `notify.feishu_webhook` | 可选：在实验里程碑时推飞书 webhook |

### 数据集配置

实验的数据来源通过 `path` **或** `dataset` 指定——二选一：

| 形式 | 含义 | 示例 |
|---|---|---|
| `path: "./dataset/xxx"` | 本地数据集目录（相对于 AHE 根目录） | `./dataset/terminal-bench-2` |
| `path: "/abs/path/xxx"` | 本地数据集目录（绝对路径） | `/root/dataset/terminal-bench-2` |
| `dataset: "<name>@<ver>"` | 引用 harbor 内置数据集（不需要本地文件） | `terminal-bench@2.0` |

公开的数据集包（按照 AHE 期望的目录结构）发布在 [`laude-institute/harbor-datasets`](https://github.com/laude-institute/harbor-datasets)——克隆或下载你需要的子集，然后让 `path` 指向它。

`base.yaml` 与 `configs/experiments/*.yaml` 中默认的 `path` 值都只是**占位符**——请按你的环境调整，或注释掉 `path`、解开 `dataset` 行以使用 harbor 的内置数据集。

---

## CLI 参考

### `python evolve.py`

| Flag | 描述 |
|---|---|
| `--config <file>` | 配置文件（overlay 优先） |
| `--batch [dir\|files...]` | 批量模式；默认扫描 `configs/experiments/` |
| `--experiment <name>` | 续跑某个已有实验（传 `experiments/` 下的目录名） |
| `--start-iteration N` | 从第 N 轮开始（默认 1） |
| `--skip-eval` | 跳过评估、复用已有 rollout（调试用） |

### `./scripts/evolve.sh`

`uv run python evolve.py` + tmux 的轻封装。

| Flag | 描述 |
|---|---|
| `<config_file>` | 位置参数：配置文件路径 |
| `--experiment <name>` | 续跑某个已有实验 |
| `--start-iteration N` | 起始迭代 |
| `--skip-eval` | 跳过评估 |
| `--session <name>` | 自定义 tmux 会话名 |
| `--batch` | 在批量模式下启动每个 overlay |
| `--attach` | 启动后自动 attach |

---

## 常用场景

**从第 16 轮恢复一次中断的实验：**

```bash
./scripts/evolve.sh \
  --experiment 2026-04-10__23-20-14__gpt54 \
  --start-iteration 16 \
  configs/experiments/exp-003-simple-code-gpt54.yaml
```

**只跑 evolve_agent，不重新评估：**

```bash
./scripts/evolve.sh \
  --experiment <existing-exp-dir> \
  --skip-eval \
  configs/experiments/exp-003-simple-code-gpt54.yaml
```

---

## 许可证

MIT
