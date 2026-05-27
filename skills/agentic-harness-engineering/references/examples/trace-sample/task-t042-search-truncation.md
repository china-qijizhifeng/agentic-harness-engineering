# Trace Sample — Task T-042: Search Results Exceed 50

> 真实失败的 trace 示例，用于演示经验可观测性分析流程。

## 任务描述

用户要求"搜索所有关于 Agent Harness 的中文文章，总结前 10 篇的核心观点"。

## 原始 Trace 摘录

```
[USER]: 搜索所有关于 Agent Harness 的中文文章，总结前 10 篇的核心观点

[THOUGHT]: 用户需要搜索中文文章，我使用 search 工具。
[TOOL: search(query="Agent Harness 中文 文章")]
[RESULT]: 找到 15 条结果（每页 15 条，共 1 页）

[THOUGHT]: 只有 15 条结果不够，用户要的是"所有"。尝试加更宽泛的关键词。
[TOOL: search(query="Agent Harness 中文 文章 site:zhihu.com")]
[RESULT]: 找到 8 条结果

[THOUGHT]: 还是不够。再试试不同的搜索词。
[TOOL: search(query="智能体 Harness 工程 中文")]
[RESULT]: 找到 12 条结果

...（多次尝试，每次 8-15 条结果，最终只搜到约 50 条）
```

## 失败分析

| 维度 | 内容 |
|------|------|
| **观察到的错误** | 总结果数约 50 条，无法覆盖"所有"中文文章 |
| **预期行为** | 搜索工具应支持分页，一次搜索应能返回前 50 条以上结果 |
| **实际行为** | 每次搜索只返回 15 条，无法翻页 |

## 根因

| 维度 | 内容 |
|------|------|
| **涉及组件** | tool_descriptions |
| **根因** | search 工具的 tool description 未声明 page_size 和 offset 参数。工具实际支持分页，但 LLM 不知道，每次都只用默认参数 |
| **证据** | trace 显示 agent 从未传入 page_size 或 offset 参数。工具返回值中也没有分页相关的 next_page / total_results 字段提示 |

## 修复建议

在 `tool_descriptions/search.tool.yaml` 的 `input_schema.properties` 中添加：

```yaml
page_size:
  type: integer
  description: 每页返回结果数（默认 15，最大 100）
  default: 15
offset:
  type: integer
  description: 结果偏移量，用于分页
  default: 0
```

同时更新 description 字段说明分页用法。
