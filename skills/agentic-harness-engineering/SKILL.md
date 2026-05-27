---
name: agentic-harness-engineering
description: Agent Harness Evolution — 基于 HARNESS.md v1.0 规范的评估、审计与改进工作流。加载后自动检查当前 workspace 结构合规性，引导 evidence-driven 的 Harness 改进。当用户要求做 Harness 审计、初始化 Agent workspace、生成或验证 Change Manifest、对 harness 组件做 evidence-driven 改进，或反复出现同一类失败模式时触发。
---

# AHE Skill — Agent Harness Evolution

> 将 [AHE（Agentic Harness Engineering）](https://arxiv.org/abs/2604.25850) 论文的核心模式实现为可执行的 Skill。
> 遵循 [HARNESS.md v1.0](references/HARNESS.md) 规范。

---

## 什么时候加载这个 Skill

- **做 Harness Audit** — 想知道当前 workspace 的结构是否符合规范 → 加载 skill，运行 audit 流程
- **改了一个核心组件** — 修改了 system prompt、工具、middleware 等 → 加载 skill，生成 Change Manifest
- **想验证上次修改** — 之前生成了 Change Manifest 但还没验证 → 加载 skill，运行 verify 流程
- **Harness 反复出同一个问题** — 同一个失败模式出现 2+ 次 → 加载 skill，做 evidence-driven 改进
- **搭建新 Agent** — 初始化一个新的 Agent workspace → 加载 skill，用 HARNESS.md 模板初始化

---

## 工作流 1：Harness Audit

评估当前 workspace 是否符合 HARNESS.md v1.0 规范。

### 步骤

1. **扫描当前 workspace 结构**
   - 检查是否存在 HARNESS.md 的 7 个组件目录
   - 统计每个组件下的文件数量和类型

2. **检查组件正交性**
   - System Rules 中是否有工具实现逻辑？→ 建议迁移到 tools/
   - Tool Descriptions 和 Tool Implementations 是否一一对应？→ 检查遗漏
   - MEMORY.md 中是否有会话级临时信息？→ 建议迁移到 workbench/

3. **检查 Change Manifest 存在性**
   - 根目录下是否存在 HARNESS_MANIFEST.json 或 manifests/ 目录？
   - 最近的修改是否有对应的 manifest？

4. **输出审计报告**
   - 合规度评分（满分 10）
   - 每个组件的状态（✅ 合规 / ⚠️ 需注意 / ❌ 不合规）
   - 改进建议列表

### 输出示例

```markdown
## Harness Audit Report — 2026-05-21

**合规度评分: 7/10**

| 组件 | 状态 | 说明 |
|------|------|------|
| System Rules | ✅ | AGENTS.md + SOUL.md 分离清晰 |
| Tool Descriptions | ✅ | 每个工具都有独立 YAML |
| Tool Implementations | ⚠️ | 部分工具的 description 和实现不同步 |
| Middleware | ❌ | 未定义 middleware 目录 |
| Skills | ✅ | skills/ 目录组织良好 |
| Sub-Agents | ⚠️ | sub_agents/ 目录存在但未注册 |
| Long-Term Memory | ✅ | MEMORY.md + experiences.md 分离 |

**改进建议：**
1. 排查 tools/vs tool_descriptions/ 的同步性
2. 考虑添加 middleware/ 目录
3. 在 code_agent.yaml 中注册 sub_agents
```

---

## 工作流 2：Generate Change Manifest

在对 Harness 组件做出修改时，生成标准化的 Change Manifest。

### 使用时机

你已经发现了问题并准备修改。在修改前或修改后，运行这个流程来记录变更。

### 步骤

1. **确认修改的组件类型**（必须从 7 个中选择一个）
2. **收集失败证据** — 描述什么失败了，附上 trace 或日志摘录
3. **分析根因** — 为什么失败？（不是"工具报错"而是"工具描述未声明参数导致 LLM 不知道可以分页"）
4. **描述修改内容** — 具体改了哪里
5. **预测影响** — 哪些任务应该修复，哪些可能回归
6. **写入 manifest 文件**

### Change Manifest 模板

将以下内容写入 `manifests/change_{timestamp}.json`：

```json
{
  "manifest_version": "1.0",
  "harness_spec_version": "1.0",
  "iteration": <当前迭代编号>,
  "timestamp": "<当前时间 ISO 格式>",
  "author": "<agent-name>",
  "changes": [
    {
      "change_id": "ch_<编号>",
      "component": "<system_rules|tool_descriptions|tool_implementations|middleware|skills|sub_agents|long_term_memory>",
      "subtype": "<update|create|delete|register>",
      "file_path": "<修改的文件路径>",
      "summary": "<一句话描述修改>",
      "failure_evidence": "<什么失败了 + 证据>",
      "root_cause": "<根因分析>",
      "targeted_fix": "<具体修改内容>",
      "predicted_impact": {
        "expected_fixes": ["<预期修复的任务>"],
        "at_risk_regressions": ["<可能回归的任务>"],
        "rationale": "<为什么这么预测>"
      }
    }
  ],
  "verification": {
    "status": "pending",
    "scheduled_at": "<下次验证时间>"
  }
}
```

---

## 工作流 3：Verify Changes

验证之前生成的 Change Manifest 的预测是否准确。

### 使用时机

- 生成了 pending 状态的 manifest，且到了 scheduled_at 时间
- 刚刚跑完一轮评估，想看之前的修改效果

### 步骤

1. **读取 manifests/** 下所有 pending 状态的 manifest
2. **收集评估结果** — 本轮 pass/fail 数据
3. **逐条验证预测**
   - `expected_fixes` 中的任务是否确实通过了？
   - `at_risk_regressions` 中的任务是否确实没有回归？
4. **更新 manifest 中的 verification 字段**
   - 设置 verdict: keep / revert / partial
5. **如果是 revert** — 执行回滚（git revert 或文件恢复）

### Verification 更新模板

```json
{
  "verification": {
    "status": "<verified|reverted|partial>",
    "scheduled_at": "<上次设置的验证时间>",
    "completed_at": "<当前时间>",
    "result": {
      "expected_fixes_verified": ["<实际修复的任务>"],
      "unexpected_fixes": ["<意外修复的任务>"],
      "regressions_observed": ["<观察到的回归>"],
      "false_predictions": ["<预测错误的任务>"]
    },
    "verdict": "<keep|revert|partial>"
  }
}
```

---

## 工作流 4：Harness 初始化

从零开始创建一个符合 HARNESS.md 规范的 Agent workspace。

### 步骤

1. **确认 Agent 类型** — Hermes / OpenClaw / Claude Code / Codex / 其他
2. **创建目录结构** — 按 HARNESS.md 标准模板创建
3. **初始化最小化 Harness**
   - 创建 System Rules 文件（AGENTS.md + SOUL.md）
   - 注册 1-2 个基本工具
4. **标记 HARNESS.md 版本** — 在 AGENTS.md 中加入 `遵循 HARNESS.md v1.0`
5. **创建 manifests/** 目录 — 用于存放 Change Manifest

---

## 原则

### Evidence-Driven

> 不要基于直觉、猜测或"最佳实践"做修改。每一次修改必须能追溯到具体的失败证据。

### Falsifiable

> 每次修改都是一个可被证伪的假设。如果下一轮评估证明预测错误，修改应被回滚。

### 最小化起始

> 从最少组件开始演化。不需要一次性配齐 7 个组件——只需要 System Rules + 1-2 个工具。

### 组件外推

> 如果同一个失败模式在 2+ 次迭代中持续出现，且在当前组件层面修复无效 → 回滚，换一个组件层面重新解决。

---

## 参考

- [HARNESS.md v1.0 — 完整规范文档](references/HARNESS.md)
- [Change Manifest JSON Schema](references/change-manifest-schema.json)
- [AHE 论文](https://arxiv.org/abs/2604.25850)
- [AHE 代码仓库](https://github.com/china-qijizhifeng/agentic-harness-engineering)
