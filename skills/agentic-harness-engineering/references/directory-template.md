# HARNESS.md Directory Template

以下是一个符合 HARNESS.md v1.0 标准的起始目录结构。用于初始化新 Agent workspace。

> ⚠️ 注意：模板创建的目录结构可与 `validate_harness.py` 配合使用。
> 运行 `python init_harness.py my-agent` 可自动生成完整可用的 workspace。

```
<project-root>/
│
├── AGENTS.md                   # [System Rules] 核心行为规则（必填）
├── SOUL.md                     # [System Rules] 身份宣言（推荐）
│
├── MEMORY.md                   # [Long-Term Memory] 长期稳定事实（推荐）
│
├── tool_descriptions/          # [Tool Descriptions]（必填，至少一个工具）
│   └── example.tool.yaml       # 示例工具描述
│
├── tools/                      # [Tool Implementations]（必填，至少一个工具）
│   └── example_tool.py         # 示例工具实现
│
├── middleware/                 # [Middleware]（可选）
│
├── skills/                     # [Skills]（可选）
│
├── sub_agents/                 # [Sub-Agents]（可选）
│
├── manifests/                  # Change Manifest 存储目录（推荐）
│
├── agent.yaml                  # [Registry] 组件注册中心（视框架而定）
│
└── .gitignore
```

### 最小可运行文件示例

#### AGENTS.md
```markdown
# AGENTS.md

> 遵循 HARNESS.md v1.0

## 组件概述
- System Rules → AGENTS.md + SOUL.md
- Tool Descriptions → tool_descriptions/
- Tool Implementations → tools/
- Long-Term Memory → MEMORY.md
```

#### tool_descriptions/example.tool.yaml
```yaml
name: example_tool
description: 一个示例工具
input_schema:
  type: object
  properties:
    message:
      type: string
      description: 输入消息
  required: [message]
```

#### tools/example_tool.py
```python
def example_tool(message: str) -> str:
    return f"Echo: {message}"
```

#### .gitignore
```
# HARNESS.md
manifests/*.json
__pycache__/
```
