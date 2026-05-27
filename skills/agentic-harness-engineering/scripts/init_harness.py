#!/usr/bin/env python3
"""
Harness Init — 从零创建符合 HARNESS.md v1.0 标准的 Agent workspace。

用法:
  python init_harness.py /path/to/new-workspace [--profile hermes|openclaw|codex|generic]
"""
import os
import sys
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))

INITIAL_FILES = {
    "AGENTS.md": """# AGENTS.md

> 遵循 HARNESS.md v1.0

## 组件概述
- System Rules → AGENTS.md
- Tool Descriptions → tool_descriptions/
- Tool Implementations → tools/

## 核心原则
1. 所有修改必须附带 Change Manifest
2. 每次修改都是可被证伪的假设
3. 从最小化状态开始演化
""",

    "SOUL.md": """# SOUL.md — 身份宣言

本 Agent 遵循 HARNESS.md v1.0 规范。
""",

    "MEMORY.md": """# MEMORY.md

## 初始状态
- 由 HARNESS.md 初始化工具创建
- 遵循 HARNESS.md v1.0
""",
}

INITIAL_DIRS = [
    "tool_descriptions",
    "tools",
    "middleware",
    "skills",
    "sub_agents",
    "manifests",
]

# Profile-specific extras
PROFILE_EXTRAS = {
    "openclaw": {
        "extra_dirs": [],
        "extra_files": {},
    },
    "codex": {
        "extra_dirs": [],
        "extra_files": {},
    },
    "hermes": {
        "extra_dirs": [],
        "extra_files": {},
    },
    "generic": {
        "extra_dirs": [],
        "extra_files": {},
    },
}

INITIAL_EXAMPLES = {
    "tool_descriptions/example.tool.yaml": """name: example_tool
description: 一个示例工具，展示 HARNESS.md 工具描述格式
input_schema:
  type: object
  properties:
    message:
      type: string
      description: 输入消息
  required: [message]
""",
    "tools/example_tool.py": """def example_tool(message: str) -> str:
    return f"Echo: {message}"
""",
    "manifests/init_manifest.json": json.dumps({
        "manifest_version": "1.0",
        "harness_spec_version": "1.0",
        "iteration": 0,
        "timestamp": datetime.now(TZ).isoformat(),
        "author": "harness-init",
        "changes": [{
            "change_id": "ch_init",
            "component": "system_rules",
            "subtype": "create",
            "file_path": "AGENTS.md",
            "summary": "Initialize harness workspace",
            "failure_evidence": "N/A",
            "root_cause": "N/A",
            "targeted_fix": "Create initial harness workspace structure",
            "predicted_impact": {
                "expected_fixes": [],
                "at_risk_regressions": [],
                "rationale": "Initial setup, no predictions"
            }
        }],
        "verification": {"status": "pending"}
    }, indent=2),
}


def init_workspace(path: str, profile: str = "generic"):
    root = Path(path)
    if root.exists():
        print(f"[ERROR] Path already exists: {path}")
        sys.exit(1)

    # Create directories
    for d in INITIAL_DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    # Create files
    for name, content in INITIAL_FILES.items():
        if name == "AGENTS.md":
            content = f"""# AGENTS.md

> 遵循 HARNESS.md v1.0
> Profile: {profile}

## 组件概述
- System Rules → AGENTS.md
- Tool Descriptions → tool_descriptions/
- Tool Implementations → tools/

## 核心原则
1. 所有修改必须附带 Change Manifest
2. 每次修改都是可被证伪的假设
3. 从最小化状态开始演化
"""
        (root / name).write_text(content, encoding="utf-8")

    for rel_path, content in INITIAL_EXAMPLES.items():
        full_path = root / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")

    # .gitignore
    (root / ".gitignore").write_text(
        "# HARNESS.md\nmanifests/*.json\n__pycache__/\n", encoding="utf-8")

    print(f"[OK] Harness workspace initialized at: {root.resolve()}")
    print(f"     Profile: {profile}")
    print(f"     Files created: {sum(1 for _ in root.rglob('*') if _.is_file())}")
    print(f"")
    print(f"     Next steps:")
    print(f"       1. Edit AGENTS.md with your Agent's rules")
    print(f"       2. Add your tools to tool_descriptions/ and tools/")
    print(f"       3. Run validate: python validate_harness.py {root.resolve()} --profile {profile}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python init_harness.py /path/to/new-workspace [--profile name]")
        sys.exit(1)

    target = sys.argv[1]
    profile = "generic"
    if "--profile" in sys.argv:
        idx = sys.argv.index("--profile")
        if idx + 1 < len(sys.argv):
            profile = sys.argv[idx + 1]

    init_workspace(target, profile)
