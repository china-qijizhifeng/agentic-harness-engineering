# 🦁 HARNESS.md + AHE.skill

> **此文件为原项目仓库的 README 备份（作为 skill 的参考材料保留）。**
> 文档中出现的相对路径（如 `AHE.skill/scripts/...`、`examples/...`）描述的是原 GitHub 仓库布局；
> 在本 skill 内的对应位置为：
> - `AHE.skill/scripts/` → `scripts/`
> - `AHE.skill/references/` → `references/`
> - `examples/`、`analysis/`、`docs/`、`HARNESS.md` → `references/examples/`、`references/analysis/`、`references/docs/`、`references/HARNESS.md`

> **Status: v0.1 experimental**
>
> 将复旦&北大的 Agentic Harness Engineering 论文的核心思想，蒸馏成一份跨 Agent 的 Harness 规范和一个初版审计工具。
>
> ⚠️ 当前版本是 AHE 思想的规范化摘录 + 静态审计原型，不是完整的 AHE 自动演化框架。详见下方「当前能力边界」。

---

## 这是什么

2026 年，行业共识已经形成：**决定 AI Agent 能不能稳定干活的，不是模型本身，而是模型外面的那套 Harness（外挂系统）。**

复旦 & 北大的 [AHE 论文](https://arxiv.org/abs/2604.25850) 证明：用三层可观测性驱动 Harness 自动演化，可以把 coding agent 的 Terminal-Bench 2 成绩从 69.7% 推到 77.0%，超越了人类手写的 Codex CLI。

但论文是论文，落地是另一回事。

这个仓库把 AHE 的核心洞察蒸馏成了两样东西：

### 📄 HARNESS.md — Agent Harness 规范 v1.0

一份**跨 Agent 的 Harness 结构规范**。任何 Agent（Hermes / OpenClaw / Claude Code / Codex / Cursor）都可以声明"遵循 HARNESS.md v1.0"，让 harness 组件有统一的语言、结构和可演化能力。

**核心内容：**
- 7 个正交组件的定义和职责
- 标准目录结构
- **Change Manifest 标准** — 每次修改带 falsifiable 预测，可验证、可归因、可回滚
- 演化循环标准流程（Evaluate → Analyze → Improve → Verify）
- 合规检查清单

### 🔧 AHE.skill — Hermes 上的 Harness 审计工具

把 HARNESS.md 规范在 Hermes Agent 上落地为可执行的 Skill。

**当前已实现：**
- ✅ **Harness Audit** — 检查 workspace 结构合规性（支持多 profile：hermes / openclaw / codex / generic）

**规划中（未实现）：**
- ⏳ Generate Manifest — 自动生成 Change Manifest（目前只有模板和 schema）
- ⏳ Verify Changes — 验证修改预测（目前只有流程文档）
- ⏳ Harness Init — 自动初始化 workspace（目前有 `init_harness.py` 脚本）

---

## 快速开始

### 1. 阅读规范

```bash
cat HARNESS.md
```

### 2. 审计你的 Agent workspace

```bash
# 通用审计
python AHE.skill/scripts/validate_harness.py /path/to/workspace

# 指定 Agent 框架（更精确的规则判断）
python AHE.skill/scripts/validate_harness.py /path/to/workspace --profile hermes
python AHE.skill/scripts/validate_harness.py /path/to/workspace --profile openclaw
python AHE.skill/scripts/validate_harness.py /path/to/workspace --profile codex

# JSON 输出（便于程序消费）
python AHE.skill/scripts/validate_harness.py /path/to/workspace --json

# 正向示例：审计一个最小合规 workspace
python AHE.skill/scripts/validate_harness.py examples/minimal-workspace
```

> ⚠️ **不要对本仓库自身运行 audit**。本仓库是规范 + 工具仓库，不是 Agent workspace。
> 应**对目标 Agent workspace 运行**。
> 正向示例请使用 `examples/minimal-workspace/`。

### 3. 从零初始化新 workspace

```bash
python AHE.skill/scripts/init_harness.py my-agent-workspace [--profile hermes]
```

### 4. 在你的 AGENTS.md 中声明遵循

```markdown
# AGENTS.md

> 🏷️ 遵循 HARNESS.md v1.0
```

---

## 当前能力边界

### 已实现
- ✅ **HARNESS.md 规范** — 组件拆分、目录标准、Change Manifest 格式、演化循环
- ✅ **validate_harness.py** — 静态目录结构审计（支持 4 种 profile）
- ✅ **Change Manifest JSON Schema** — 可校验 manifest 格式
- ✅ **directory-template.md** — 目录结构模板
- ✅ **examples/minimal-workspace/** — 正向示例
- ✅ **profiles/** — Hermes / OpenClaw / Codex / generic 适配

### 未实现（后续迭代）
- ⏳ **Experience Observability** — trace 采集格式、评估运行数据结构、失败分析报告生成器
- ⏳ **自动 Generate Manifest** — 根据 git diff / 用户输入自动生成
- ⏳ **自动 Verify Manifest** — 读取评估结果，验证预测，回写 verdict
- ⏳ **演化闭环** — Evaluate → Analyze → Improve → Verify 的可执行闭环
- ⏳ **更多 profile** — Claude Code、Cursor 等框架的适配

### 已知限制
- validate_harness.py 只能做静态文件/目录检查，不能检查组件内容质量
- 对 OpenClaw / Codex 等框架的 tool_descriptions 可能与其运行时注入机制不完全匹配
- 部分 Agent 框架的工具描述不必然以文件形式存在于 workspace 中

---

## 项目结构

```
├── HARNESS.md                              # 规范文档（跨 Agent）
├── README.md                               # 本文件
├── AHE.skill/
│   ├── SKILL.md                            # Hermes Skill 定义
│   ├── references/
│   │   ├── HARNESS.md                      # 规范副本
│   │   ├── change-manifest-schema.json     # Manifest JSON Schema
│   │   └── directory-template.md           # 目录模板
│   ├── scripts/
│   │   ├── validate_harness.py             # 合规检查脚本
│   │   ├── init_harness.py                 # 初始化脚本
│   │   ├── generate_manifest.py            # Manifest 生成工具（开发中）
│   │   └── verify_manifest.py              # Manifest 验证工具（开发中）
│   └── profiles/
│       ├── hermes.yaml                     # Hermes 框架适配
│       ├── openclaw.yaml                   # OpenClaw 框架适配
│       └── codex.yaml                      # Codex 框架适配
├── examples/
│   └── minimal-workspace/                  # 最小合规 workspace 示例
└── analysis/                               # 经验可观测性标准文档
    ├── README.md                           # 分析报告标准说明
    ├── overview-template.md                # 概览报告模板
    └── detail-template.md                  # 逐任务分析模板
```

---

## 背景

这份工作基于以下研究：

- **[Agentic Harness Engineering](https://arxiv.org/abs/2604.25850)** — Jiahang Lin, Shichun Liu, Chengjun Pan 等（复旦 & 北大, 2026）
  - 论文提出三层可观测性框架（组件 / 经验 / 决策），让 coding agent 的 Harness 可以自动演化
  - Terminal-Bench 2 #3（84.7% pass@1），超过手写 Codex CLI 和 ACE/TF-GRPO
  - 冻结的 Harness 跨 benchmark、跨模型迁移

- **[Agent Harness for LLM Agents: A Survey](https://huggingface.co/datasets/GloriaaaM/LLM-Agent-Harness-Survey)** — Harness 六组件形式化定义 H=(E,T,C,S,L,V)

---

## 许可

MIT
