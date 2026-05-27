# HARNESS.md — Agent Harness Specification v1.0

> **一份跨 Agent 的 Harness 结构规范。**
> 让任何 Agent（Hermes / OpenClaw / Claude Code / Codex / Cursor）的"外挂系统"有统一的语言、结构和可演化能力。
>
> 灵感来源：[Agentic Harness Engineering: Observability-Driven Automatic Evolution of Coding-Agent Harnesses](https://arxiv.org/abs/2604.25850)（复旦 & 北大, 2026）

---

## 为什么需要这个规范

2026 年的行业共识已经形成：**决定 AI Agent 能不能稳定干活的，不是模型本身，而是模型外面那套 Harness（外挂系统）。**

但现状是：
- 每个 Agent 框架的 harness 结构都不一样，组件命名、目录规范、注册方式各自为政
- 没有统一语言来描述"我改了工具描述"、"我加了中间件"——不同 Agent 说的不是同一种话
- Harness 改了好不好，无法验证——改了就是改了，没人检查改对了还是改坏了

**HARNESS.md 要解决的就是这三个问题：统一语言、标准结构、可演化性。**

---

## 核心架构：7 个正交组件

一个完整的 Agent Harness 由 7 个正交组件构成。**正交**意味着：改其中一个不影响其他。

| # | 组件 | 职责 | 示例文件 | 类比 |
|---|------|------|---------|------|
| 1 | **System Rules** | Agent 的行为规则、工作流指引、边界定义 | `AGENTS.md`, `SOUL.md`, `systemprompt.md`, `.cursorrules` | 宪法 |
| 2 | **Tool Descriptions** | 每个工具的 Schema、用途说明、使用陷阱 | `tool_descriptions/*.yaml` | 产品说明书 |
| 3 | **Tool Implementations** | 工具执行的代码逻辑 | `tools/*.py`, `tools/*.js` | 机器人工厂 |
| 4 | **Middleware** | 执行管道的钩子——拦截、转换、增强 | `middleware/*.py` | 安检通道 |
| 5 | **Skills** | 可复用的工作流模式 | `skills/*/SKILL.md` | SOP 手册 |
| 6 | **Sub-Agents** | 可委托执行的子代理单元 | `sub_agents/*/` | 外包团队 |
| 7 | **Long-Term Memory** | 跨会话的持久知识存储 | `MEMORY.md`, `experiences.md` | 个人笔记 |

### 组件交互原则

```
用户输入
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  System Rules ───── 定义 Agent 怎么思考、怎么决策               │
├──────────────────────────────────────────────────────────────┤
│  Middleware ──────── 拦截/转换输入输出                          │
├──────────────────────────────────────────────────────────────┤
│  Skills ─────────── 匹配复用工作流                             │
├──────────────────────────────────────────────────────────────┤
│  Tool Descriptions ─ 告诉 LLM 有什么工具、怎么用                │
│  Tool Implementations ─ 执行工具逻辑                           │
├──────────────────────────────────────────────────────────────┤
│  Sub-Agents ─────── 委托专注子任务                             │
├──────────────────────────────────────────────────────────────┤
│  Long-Term Memory ─ 读取/写入跨会话知识                        │
└──────────────────────────────────────────────────────────────┘
    │
    ▼
  输出
```

### 反模式

- ❌ 把工具实现逻辑写进 System Rules（规则和实现混杂）
- ❌ 把长期记忆塞进 System Rules（每次加载都膨胀上下文）
- ❌ 多个组件做同一件事（比如 prompt 和 middleware 都在做输出格式化）
- ❌ 改了工具不更新描述（工具实现和描述不同步）

---

## 目录结构标准

一个符合 HARNESS.md 规范的 Agent workspace 应遵循以下结构：

```
<workspace>/
├── AGENTS.md                   # System Rules（核心规则文件）
├── SOUL.md                     # System Rules（身份宣言）
├── MEMORY.md                   # Long-Term Memory
├── experiences.md              # Long-Term Memory（经验教训）
│
├── tool_descriptions/          # Tool Descriptions
│   ├── search.tool.yaml
│   ├── read_file.tool.yaml
│   └── run_command.tool.yaml
│
├── tools/                      # Tool Implementations
│   ├── search_tools.py
│   ├── file_tools.py
│   └── shell_tools.py
│
├── middleware/                 # Middleware
│   ├── context_compaction.py
│   ├── output_formatter.py
│   └── tool_output_truncator.py
│
├── skills/                     # Skills
│   ├── code-review/
│   │   └── SKILL.md
│   ├── data-analysis/
│   │   └── SKILL.md
│   └── ...
│
├── sub_agents/                 # Sub-Agents
│   ├── researcher/
│   │   └── config.yaml
│   └── debugger/
│       └── config.yaml
│
└── agent.yaml                  # 组件注册中心（可选，视框架而定）
```

### 最小化起始状态

一个可工作的 harness 最小只需要 2 个组件：
1. **System Rules** — 至少一个规则文件
2. **Tool Descriptions + Implementations** — 至少一个工具

其他组件（Middleware、Skills、Sub-Agents、Long-Term Memory）应**在需要时才添加**。AHE 论文证明：从最小化状态开始演化的效果最好——所有改进都是"挣来的"，不是"预设的"。

---

## 组件注册标准

当 Agent 框架支持注册中心（如 Hermes 的 `config.yaml`、AHE 的 `code_agent.yaml`）时，注册格式应遵循：

### Tools 注册

```yaml
tools:
  - name: read_file
    description_path: ./tool_descriptions/read_file.tool.yaml
    binding: tools.file_tools:read_file
```

每个工具需要三个东西：
1. **描述 YAML** — LLM 看到的内容（用途、参数 Schema、使用陷阱）
2. **实现绑定** — 实际执行的代码路径
3. **注册条目** — 在配置文件中声明

### Middleware 注册

```yaml
middlewares:
  - import: middleware.long_tool_output:LongToolOutputMiddleware
    params:
      max_output_chars: 10000
```

### Skills 注册

```yaml
skills:
  - ./skills/code-review
```

### Sub-Agents 注册

```yaml
sub_agents:
  - name: researcher
    config: ./sub_agents/researcher/config.yaml
```

---

## Change Manifest 标准格式

**这是整个规范的核心。** 每一次对 Harness 组件的修改，都应该附带一份标准的 Change Manifest，使得改动能被验证、被归因、被回滚。

### Manifest Schema

```json
{
  "manifest_version": "1.0",
  "harness_spec_version": "1.0",
  "iteration": 3,
  "timestamp": "2026-05-21T10:30:00+08:00",
  "author": "agent-name-or-human",
  "changes": [
    {
      "change_id": "ch_001",
      "component": "tool_descriptions",
      "subtype": "update",
      "file_path": "tool_descriptions/search.tool.yaml",
      "summary": "为 search 工具添加分页参数说明",
      "failure_evidence": "Task T-042 在搜索超过50条结果时失败，trace 显示 agent 尝试传入 page=2 参数但工具描述未声明该参数",
      "root_cause": "search 工具实际支持分页，但 tool description 未声明 page_size 和 offset 参数，导致 LLM 不知道可以分页",
      "targeted_fix": "在 search.tool.yaml 的 input_schema.properties 中添加 page_size (integer) 和 offset (integer) 参数，更新 description 说明分页用法",
      "predicted_impact": {
        "expected_fixes": ["T-042", "T-057"],
        "at_risk_regressions": ["T-013"],
        "rationale": "T-042 和 T-057 都因搜索结果截断失败；T-013 对 search 有依赖但调用模式不同"
      }
    }
  ],
  "verification": {
    "status": "pending",
    "scheduled_at": "2026-05-22T10:30:00+08:00"
  }
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `manifest_version` | ✅ | Manifest 格式版本 |
| `harness_spec_version` | ✅ | 遵循的 HARNESS.md 版本 |
| `iteration` | ✅ | 迭代编号 |
| `timestamp` | ✅ | 修改时间 |
| `author` | ✅ | 修改者（Agent 名称或人） |
| `changes[].component` | ✅ | 修改的组件类型（必须是 7 个之一） |
| `changes[].failure_evidence` | ✅ | **什么失败了** + 可引用的 trace 证据 |
| `changes[].root_cause` | ✅ | **为什么失败**（根因分析） |
| `changes[].targeted_fix` | ✅ | **改了哪里**（具体的修改描述） |
| `changes[].predicted_impact` | ✅ | **预测什么会变好/变坏** |
| `verification.status` | ✅ | `pending` / `verified` / `reverted` |
| `verification.scheduled_at` | 推荐 | 下次验证的时间 |

### 核心原则：Falsifiable

> **每次修改都是一个可被证伪的假设。**

下一轮评估后的验证结果应回写到 manifest 中：

```json
{
  "verification": {
    "status": "verified",
    "scheduled_at": "2026-05-22T10:30:00+08:00",
    "completed_at": "2026-05-22T10:45:00+08:00",
    "result": {
      "expected_fixes_verified": ["T-042", "T-057"],
      "unexpected_fixes": [],
      "regressions_observed": [],
      "false_predictions": []
    },
    "verdict": "keep"
  }
}
```

三种 verdict：
- `keep` — 修改有效，保留
- `revert` — 修改无效或导致回归，回滚
- `partial` — 部分有效，需调整

---

## 演化循环标准流程

HARNESS.md 定义的标准演化循环：

```
┌──────────────────────────────────────────────────┐
│                    第 N 轮                         │
│                                                    │
│  ① Evaluate ── 用当前 Harness 跑测试任务            │
│      │           产出：轨迹 + 分数                     │
│      ▼                                              │
│  ② Analyze ─── 分析轨迹，提取模式化的失败证据         │
│      │           产出：根因报告                         │
│      ▼                                              │
│  ③ Improve ─── 基于证据修改 Harness 组件             │
│      │           产出：Change Manifest                 │
│      ▼                                              │
│  ④ Verify ──── 下一轮评估，验证修改预测               │
│                产出：Verdict（keep / revert）          │
└──────────────────────────────────────────────────┘
```

### 每步产出

| 步骤 | 输入 | 产出 |
|------|------|------|
| **Evaluate** | 当前 Harness (Hₙ) + 测试任务集 | 轨迹文件 + pass/fail 结果 |
| **Analyze** | 轨迹文件 | 根因分析报告（overview + per-task） |
| **Improve** | 根因报告 + 当前 Harness | 修改后的 Harness (Hₙ₊₁) + Change Manifest |
| **Verify** | Change Manifest + 下一轮评估结果 | Verdict（keep / revert / partial） |

### 可观测性的三层

1. **组件可观测性** — 每个组件对应唯一文件，修改可见、可回滚
2. **经验可观测性** — 轨迹被蒸馏为可消费的根因报告，而不是原始日志
3. **决策可观测性** — 每次修改带 falsifiable 预测，可验证、可归因

---

## 合规检查清单

要声明"遵循 HARNESS.md v1.0"，Agent 必须满足以下条件：

### 必须（Hard Requirements）

- [ ] Harness 的 7 个组件被清晰地分离到不同的文件/目录
- [ ] 每个工具都有独立的 description 文件（LLM 看到的 Schema）和实现文件
- [ ] 对 Harness 的每次修改都附带 Change Manifest
- [ ] Change Manifest 包含 failure_evidence + root_cause + targeted_fix + predicted_impact
- [ ] 每次修改后都经过验证（verified / reverted / partial）

### 推荐（Soft Requirements）

- [ ] 从最小化 Harness 开始演化（只有 System Rules + 1-2 个工具）
- [ ] Workspace 目录结构符合标准模板
- [ ] 有自动化的 Evaluate 流程（cronjob / CI）
- [ ] 有自动化的 Analyze 流程（轨迹蒸馏）
- [ ] Change Manifest 存储在版本控制中（git-tracked）

### 禁止（Anti-Patterns）

- [ ] ❌ 把工具实现逻辑写进 System Rules
- [ ] ❌ 不改描述只改实现（工具不同步）
- [ ] ❌ 不经验证直接修改核心组件

---

## 采纳指南

### 对现有 Agent 框架

如果你的 Agent 已经有自己的结构（如 Hermes 的 H3M、Claude Code 的 CLAUDE.md），不需要推倒重来。只需：

1. **做一次 Harness Audit** — 检查当前组件是否正交、边界是否清晰
2. **补充 Change Manifest** — 对最近的修改补齐 manifest
3. **声明引用** — 在 AGENTS.md 中加入一行 `遵循 HARNESS.md v1.0`

### 对新 Agent 项目

直接按上述目录结构初始化 workspace，从最小化状态开始。

### 实现参考

- [agentic-harness-engineering skill](../SKILL.md) — 实现本规范的 Skill（含 audit、generate-manifest、verify 功能）
- [AHE 论文](https://arxiv.org/abs/2604.25850) — 本规范的学术基础（复旦 & 北大，2026）

---

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-05-21 | 初始版本 |

---

## 许可

MIT — 自由使用、修改、分发。

---

*"Harness 是数据集。你的竞争优势是你 Harness 捕获的轨迹。"*
*— Philipp Schmid, 2026*
