#!/usr/bin/env python3
"""
Generate Change Manifest — 根据 git diff 或用户输入生成 HARNESS.md Change Manifest。

用法:
  python generate_manifest.py --workspace /path/to/workspace
                             [--diff --staged | --interactive]
                             [--author agent-name]

输出:
  将 manifest 写入 manifests/change_<timestamp>.json
"""
import os
import sys
import json
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))

COMPONENTS = ["system_rules", "tool_descriptions", "tool_implementations",
              "middleware", "skills", "sub_agents", "long_term_memory"]

SUBTYPES = ["update", "create", "delete", "register"]

MANIFEST_TEMPLATE = {
    "manifest_version": "1.0",
    "harness_spec_version": "1.0",
    "iteration": None,
    "timestamp": None,
    "author": None,
    "changes": [],
    "verification": {
        "status": "pending",
        "scheduled_at": None
    }
}


def get_next_iteration(root: Path) -> int:
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        return 0
    manifests = list(manifests_dir.glob("change_*.json"))
    return len(manifests)


def parse_git_diff(root: Path, staged: bool = False) -> list:
    """从 git diff 提取变更信息"""
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=str(root), timeout=30)
        if result.returncode != 0:
            print(f"[WARN] git diff failed: {result.stderr.strip()}")
            return []
        return parse_diff_output(result.stdout, root)
    except FileNotFoundError:
        print(f"[WARN] git not available, falling back to interactive mode")
        return []
    except subprocess.TimeoutExpired:
        print(f"[WARN] git diff timed out")
        return []


def parse_diff_output(diff_text: str, root: Path) -> list:
    """解析 git diff 输出，映射到组件"""
    changes = []
    current_file = None
    for line in diff_text.splitlines():
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            current_file = line[6:].strip()
        elif line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                current_file = parts[3][2:]  # b/path

        if current_file and current_file != "/dev/null":
            rel_path = Path(current_file)
            component = _detect_component(rel_path)
            if component and not any(c["file_path"] == current_file for c in changes):
                changes.append({
                    "change_id": f"ch_{len(changes) + 1:03d}",
                    "component": component,
                    "subtype": "update",
                    "file_path": current_file,
                    "summary": f"Modify {current_file}",
                    "failure_evidence": "",
                    "root_cause": "",
                    "targeted_fix": "",
                    "predicted_impact": {
                        "expected_fixes": [],
                        "at_risk_regressions": [],
                        "rationale": ""
                    }
                })
    return changes


def _detect_component(path: Path) -> str:
    """根据文件路径推断所属组件"""
    spath = str(path.as_posix())
    if spath in ("AGENTS.md", "SOUL.md", "systemprompt.md", "CLAUDE.md", ".cursorrules"):
        return "system_rules"
    if spath.startswith("tool_descriptions/"):
        return "tool_descriptions"
    if spath.startswith("tools/"):
        return "tool_implementations"
    if spath.startswith("middleware/"):
        return "middleware"
    if spath.startswith("skills/"):
        return "skills"
    if spath.startswith("sub_agents/"):
        return "sub_agents"
    if spath.startswith("MEMORY.md") or spath.startswith("experiences.md"):
        return "long_term_memory"
    return None


def interactive_mode(root: Path) -> list:
    """交互式输入变更信息"""
    print(f"\n[Harness Change Manifest Generator]")
    print(f"  Workspace: {root.resolve()}")
    print(f"  Iteration: {get_next_iteration(root)}")

    changes = []
    while True:
        print(f"\n--- Change #{len(changes) + 1} ---")
        print(f"Components: {', '.join(COMPONENTS)}")
        comp = input("  Component: ").strip()
        if comp not in COMPONENTS:
            print(f"  [SKIP] Invalid component, skipping")
            break

        print(f"Subtypes: {', '.join(SUBTYPES)}")
        subtype = input("  Subtype (update): ").strip() or "update"

        file_path = input("  File path: ").strip()
        summary = input("  Summary: ").strip()
        evidence = input("  Failure evidence: ").strip()
        root_cause = input("  Root cause: ").strip()
        targeted_fix = input("  Targeted fix: ").strip()

        changes.append({
            "change_id": f"ch_{len(changes) + 1:03d}",
            "component": comp,
            "subtype": subtype,
            "file_path": file_path,
            "summary": summary,
            "failure_evidence": evidence,
            "root_cause": root_cause,
            "targeted_fix": targeted_fix,
            "predicted_impact": {
                "expected_fixes": [],
                "at_risk_regressions": [],
                "rationale": ""
            }
        })

        more = input("\n  Add another change? (y/N): ").strip().lower()
        if more != "y":
            break

    return changes


def write_manifest(root: Path, changes: list, author: str):
    """将 manifest 写入文件"""
    next_iter = get_next_iteration(root)
    now = datetime.now(TZ)
    scheduled = (now + timedelta(hours=24)).isoformat()

    manifest = dict(MANIFEST_TEMPLATE)
    manifest["iteration"] = next_iter
    manifest["timestamp"] = now.isoformat()
    manifest["author"] = author or "agent"
    manifest["changes"] = changes
    manifest["verification"]["scheduled_at"] = scheduled

    # Check for empty evidence fields → mark as draft
    has_empty_evidence = any(
        not c.get("failure_evidence", "").strip()
        or not c.get("root_cause", "").strip()
        or not c.get("targeted_fix", "").strip()
        for c in changes
    )
    if has_empty_evidence:
        manifest["verification"]["status"] = "draft"
        print("[WARN] Some changes have empty evidence/root_cause/targeted_fix.")
        print("       Manifest marked as 'draft'. Complete these fields before verification.")

    filename = f"change_{now.strftime('%Y%m%d_%H%M%S')}.json"
    out_path = root / "manifests" / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n[OK] Manifest written to: {out_path}")
    return out_path


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate HARNESS.md Change Manifest")
    parser.add_argument("--workspace", "-w", default=".", help="Workspace root path")
    parser.add_argument("--diff", action="store_true", help="Parse from git diff")
    parser.add_argument("--staged", action="store_true", help="Use staged diff")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    parser.add_argument("--author", default="agent", help="Author name")
    args = parser.parse_args()

    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"[ERROR] Workspace not found: {root}")
        sys.exit(1)

    changes = []

    if args.diff:
        changes = parse_git_diff(root, staged=args.staged)

    if args.interactive or not changes:
        ic = interactive_mode(root)
        if ic:
            changes = ic
        elif not args.diff:
            # Try diff as fallback
            changes = parse_git_diff(root)

    if not changes:
        print("[WARN] No changes detected, not generating manifest")
        sys.exit(0)

    write_manifest(root, changes, author=args.author)


if __name__ == "__main__":
    main()
