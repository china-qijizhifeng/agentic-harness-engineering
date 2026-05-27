#!/usr/bin/env python3
"""
Harness Compliance Checker — 验证 Agent workspace 是否符合 HARNESS.md v1.0 规范。

用法:
  python validate_harness.py /path/to/workspace [--json] [--profile hermes|openclaw|codex|generic]

输出:
  合规审计报告 (JSON 格式 + 可读摘要)
"""
import os
import sys
import json
from pathlib import Path

# Try to load profiles from YAML files
PROFILES_DIR = Path(__file__).resolve().parent.parent / "profiles"
PROFILES = {}  # Will be populated from YAML files, with hardcoded fallback

def _load_profiles():
    profiles = {}
    if PROFILES_DIR.is_dir():
        for yaml_file in sorted(PROFILES_DIR.glob("*.yaml")):
            try:
                name = yaml_file.stem
                lines = yaml_file.read_text(encoding="utf-8").splitlines()
                checks = {}
                current_check = None
                for line in lines:
                    line = line.strip()
                    if line.startswith("check:"):
                        continue
                    if line.startswith("  ") and not line.startswith("    "):
                        # Top-level check name
                        current_check = line.strip().rstrip(":")
                    elif current_check and ":" in line:
                        key, val = line.split(":", 1)
                        key = key.strip()
                        val = val.strip()
                        if current_check not in checks:
                            checks[current_check] = {}
                        if key in ("required",):
                            checks[current_check][key] = val.lower() == "true"
                        elif key in ("weight",):
                            checks[current_check][key] = int(val)
                        elif key in ("files", "extensions"):
                            checks[current_check][key] = [v.strip().strip('"').strip("'") for v in val.strip("[]").split(",") if v.strip()]
                        elif key == "dir":
                            checks[current_check][key] = val.strip('"').strip("'")
                        elif key == "indicator":
                            checks[current_check][key] = val.strip('"').strip("'")
                if checks:
                    profiles[name] = checks
            except Exception:
                continue
    return profiles

# Load YAML profiles, fall back to hardcoded defaults
_yaml_profiles = _load_profiles()
HARDCODED_PROFILES = {
    "hermes": {
        "system_rules": {"files": ["AGENTS.md", "SOUL.md", "systemprompt.md"], "required": True, "weight": 2},
        "tool_descriptions": {"dir": "tool_descriptions", "extensions": [".yaml", ".yml", ".json"], "required": True, "weight": 2},
        "tool_implementations": {"dir": "tools", "extensions": [".py", ".js", ".ts", ".sh"], "required": True, "weight": 2},
        "middleware": {"dir": "middleware", "extensions": [".py", ".js", ".ts"], "required": False, "weight": 1},
        "skills": {"dir": "skills", "indicator": "SKILL.md", "required": False, "weight": 1},
        "sub_agents": {"dir": "sub_agents", "required": False, "weight": 1},
        "long_term_memory": {"files": ["MEMORY.md", "experiences.md", "LongTermMEMORY.md"], "required": False, "weight": 1},
    },
    "openclaw": {
        "system_rules": {"files": ["AGENTS.md", "SOUL.md", "systemprompt.md", "CLAUDE.md"], "required": True, "weight": 2},
        "tool_descriptions": {"dir": "tool_descriptions", "extensions": [".yaml", ".yml", ".json", ".md"], "required": False, "weight": 1},
        "tool_implementations": {"dir": "tools", "extensions": [".py", ".js", ".ts", ".sh"], "required": True, "weight": 2},
        "middleware": {"dir": "middleware", "extensions": [".py", ".js", ".ts"], "required": False, "weight": 1},
        "skills": {"dir": "skills", "indicator": "SKILL.md", "required": True, "weight": 2},
        "sub_agents": {"dir": "sub_agents", "required": False, "weight": 1},
        "long_term_memory": {"files": ["MEMORY.md", "experiences.md", "LongTermMEMORY.md"], "required": True, "weight": 1},
    },
    "codex": {
        "system_rules": {"files": ["AGENTS.md", "SOUL.md", "systemprompt.md"], "required": True, "weight": 2},
        "tool_descriptions": {"dir": "tool_descriptions", "extensions": [".yaml", ".yml", ".json"], "required": False, "weight": 1},
        "tool_implementations": {"dir": "tools", "extensions": [".py", ".js", ".ts", ".sh"], "required": False, "weight": 1},
        "middleware": {"dir": "middleware", "extensions": [".py", ".js", ".ts"], "required": False, "weight": 1},
        "skills": {"dir": "skills", "indicator": "SKILL.md", "required": False, "weight": 1},
        "sub_agents": {"dir": "sub_agents", "required": False, "weight": 1},
        "long_term_memory": {"files": ["MEMORY.md", "experiences.md"], "required": False, "weight": 1},
    },
    "generic": {
        "system_rules": {"files": ["AGENTS.md", "systemprompt.md", "CLAUDE.md", ".cursorrules"], "required": True, "weight": 2},
        "tool_descriptions": {"dir": "tool_descriptions", "extensions": [".yaml", ".yml", ".json"], "required": False, "weight": 1},
        "tool_implementations": {"dir": "tools", "extensions": [".py", ".js", ".ts", ".sh"], "required": False, "weight": 1},
        "middleware": {"dir": "middleware", "extensions": [".py", ".js", ".ts"], "required": False, "weight": 1},
        "skills": {"dir": "skills", "indicator": "SKILL.md", "required": False, "weight": 1},
        "sub_agents": {"dir": "sub_agents", "required": False, "weight": 1},
        "long_term_memory": {"files": ["MEMORY.md", "experiences.md"], "required": False, "weight": 1},
    },
}

# Merge: YAML profiles take priority, hardcoded fill gaps
if _yaml_profiles:
    PROFILES = dict(HARDCODED_PROFILES)
    PROFILES.update(_yaml_profiles)
else:
    PROFILES = dict(HARDCODED_PROFILES)


def audit_workspace(workspace_root: str, profile: str = "generic") -> dict:
    root = Path(workspace_root)
    if not root.exists():
        return {"error": f"Path does not exist: {workspace_root}"}

    if profile not in PROFILES:
        return {"error": f"Unknown profile: {profile}. Available: {', '.join(PROFILES.keys())}"}

    checks = PROFILES[profile]

    report = {
        "workspace": str(root.resolve()),
        "profile": profile,
        "compliance_score": 0,
        "max_score": 0,
        "compliance_pct": 0.0,
        "components": {},
        "issues": [],
        "warnings": [],
    }

    score = 0
    max_score = sum(c["weight"] for c in checks.values())

    # Manifest 检查
    has_manifest_dir = (root / "manifests").is_dir()
    has_manifest_paths = [p for p in root.glob("*manifest*") if p.is_file()] + [p for p in root.glob("manifests/*.json") if p.is_file()]
    has_manifest = bool(has_manifest_paths)

    for name, check in checks.items():
        component_report = {"status": "missing", "detail": "", "files_found": []}

        # Check by directory
        if "dir" in check:
            dir_path = root / check["dir"]
            if dir_path.is_dir():
                files = []
                if "extensions" in check:
                    files = [f for f in dir_path.iterdir() if f.suffix in check["extensions"]]
                elif "indicator" in check:
                    files = list(dir_path.rglob(check["indicator"]))

                if files:
                    component_report["status"] = "ok"
                    component_report["detail"] = f"Found {len(files)} file(s) in {check['dir']}/"
                    component_report["files_found"] = [str(f.relative_to(root)) for f in files[:10]]
                else:
                    component_report["status"] = "empty"
                    component_report["detail"] = f"Directory {check['dir']}/ exists but no matching files"
                    if not check["required"]:
                        report["warnings"].append(f"{name}: {check['dir']}/ exists but is empty")
            else:
                if check["required"]:
                    component_report["detail"] = f"Missing required directory: {check['dir']}/"
                    report["issues"].append(f"MISSING: {name} \u2014 required directory {check['dir']}/ not found")
                else:
                    component_report["status"] = "optional"
                    component_report["detail"] = f"Optional directory {check['dir']}/ not found"

        # Check by file list
        if "files" in check:
            found = [f for f in check["files"] if (root / f).is_file()]
            if found:
                component_report["status"] = "ok"
                component_report["detail"] = f"Found files: {', '.join(found)}"
                component_report["files_found"] = found
            else:
                if check["required"]:
                    component_report["detail"] = f"Missing required file(s): {', '.join(check['files'])}"
                    report["issues"].append(f"MISSING: {name} \u2014 required file not found")
                    component_report["status"] = "missing"
                else:
                    component_report["status"] = "optional"
                    component_report["detail"] = "No files found (optional)"

        if component_report["status"] == "ok":
            score += check["weight"]

        report["components"][name] = component_report

    # Manifest 检查
    manifest_files_str = []
    if has_manifest_paths:
        manifest_files_str = [str(p.relative_to(root)) for p in has_manifest_paths]
    report["manifest_check"] = {
        "has_manifest_dir": has_manifest_dir,
        "manifest_files": manifest_files_str,
        "count": len(manifest_files_str),
    }
    if has_manifest:
        score += 1  # Bonus point for having manifests

    report["compliance_score"] = score
    report["max_score"] = max_score + 1  # +1 for manifest bonus
    report["compliance_pct"] = round(score / (max_score + 1) * 100, 1)

    return report


def print_report(report: dict):
    if "error" in report:
        print(f"[ERROR] {report['error']}")
        return

    pct = report["compliance_pct"]

    print(f"\n{'='*60}")
    print(f"  HARNESS.md Compliance Audit")
    print(f"  Workspace: {report['workspace']}")
    print(f"  Profile: {report['profile']}")
    print(f"  Score: {report['compliance_score']}/{report['max_score']} ({pct}%)")
    print(f"{'='*60}")

    print(f"\n[Components]:")
    for name, comp in report["components"].items():
        icon = {"ok": "[OK]", "missing": "[MISSING]", "empty": "[WARN]", "optional": "[--]"}.get(comp["status"], "[?]")
        print(f"  {icon} {name}: {comp['detail']}")

    print(f"\n[Manifests]:")
    m = report["manifest_check"]
    if m["manifest_files"]:
        print(f"  [OK] {m['count']} manifest file(s) found")
    else:
        print(f"  [--] No change manifests found (optional but recommended)")

    if report["issues"]:
        print(f"\n[Issues] ({len(report['issues'])}):")
        for i in report["issues"]:
            print(f"  [MISSING] {i}")

    if report["warnings"]:
        print(f"\n[Warnings] ({len(report['warnings'])}):")
        for w in report["warnings"]:
            print(f"  [WARN] {w}")

    # 分级判定
    num_issues = len(report["issues"])
    if pct >= 90 and num_issues == 0:
        print(f"\n[PERFECT] Perfect compliance with HARNESS.md v1.0!")
    elif pct >= 60 and num_issues == 0:
        print(f"\n[PASS] Minimum viable compliance. Score {pct}%, no hard issues. Review warnings.")
    elif pct >= 60:
        print(f"\n[PARTIAL] Partial compliance ({pct}%). Fix {num_issues} issue(s) to reach minimum viable.")
    else:
        print(f"\n[FAIL] Needs improvement. Score {pct}%, {num_issues} issue(s) to resolve.")


if __name__ == "__main__":
    target = "."
    profile = "generic"

    args = sys.argv[1:]
    # Parse --profile
    if "--profile" in args:
        idx = args.index("--profile")
        if idx + 1 < len(args):
            profile = args[idx + 1]
            args = args[:idx] + args[idx + 2:]

    if args:
        target = args[0]

    report = audit_workspace(target, profile=profile)

    if "--json" in args:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
