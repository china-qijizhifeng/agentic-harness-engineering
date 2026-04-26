#!/usr/bin/env python3
"""NexAU Agent config validator for the evolution pipeline.

Validates agent YAML config and all referenced files:
- YAML syntax and schema
- System prompt file existence
- Tool YAML definitions (existence + content)
- Middleware import paths → actual Python files
- Python syntax of all .py files under tools/ and middleware/
- Skill directory structure

Usage:
    python validate_agent.py <agent.yaml> [--check-python] [--json]

Exit codes:
    0 = valid (no errors, warnings OK)
    1 = invalid (one or more errors)
"""

from __future__ import annotations

import argparse
import ast
import json as json_mod
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(2)


@dataclass
class Issue:
    severity: str  # "ERROR" | "WARNING"
    category: str
    message: str
    path: str | None = None


@dataclass
class Report:
    yaml_path: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not any(i.severity == "ERROR" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARNING")


_ENV_VAR_RE = re.compile(r"\$\{env\.")


def _resolve(yaml_dir: Path, rel: str) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else (yaml_dir / p).resolve()


def _has_env_var(value: str) -> bool:
    return bool(_ENV_VAR_RE.search(value))


# ── Phase 1: YAML syntax ──────────────────────────────────────────


def _load_raw(yaml_path: Path, issues: list[Issue]) -> dict[str, Any] | None:
    try:
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        issues.append(Issue("ERROR", "syntax", f"YAML parse error: {exc}"))
        return None
    if isinstance(data, dict):
        return cast(dict[str, Any], data)
    issues.append(Issue("ERROR", "syntax", f"YAML root is not a mapping (got {type(data).__name__})"))
    return None


# ── Phase 2: Schema validation (uses nexau if available) ──────────


def _validate_schema(yaml_path: str, issues: list[Issue]) -> None:
    try:
        from contextlib import redirect_stderr
        from io import StringIO
        from nexau.archs.main_sub.config.schema import AgentConfigSchema, ConfigError

        with redirect_stderr(StringIO()):
            AgentConfigSchema.from_yaml(yaml_path)
    except ImportError:
        issues.append(Issue("WARNING", "schema", "nexau not importable, skipping schema validation"))
    except Exception as exc:
        msg = str(exc)
        if "is not set" in msg and "Environment variable" in msg:
            issues.append(Issue("WARNING", "schema", msg))
        else:
            issues.append(Issue("ERROR", "schema", msg))


# ── Phase 3: File reference checks ───────────────────────────────


def _check_system_prompt(config: dict[str, Any], yaml_dir: Path, issues: list[Issue]) -> None:
    sp_type = str(config.get("system_prompt_type", "string"))
    sp_value = config.get("system_prompt")

    if sp_type in ("file", "jinja"):
        if not sp_value or not isinstance(sp_value, str):
            issues.append(Issue("ERROR", "system_prompt",
                                f"system_prompt is required when type is '{sp_type}'"))
            return
        if _has_env_var(sp_value):
            issues.append(Issue("WARNING", "system_prompt",
                                f"contains env var, cannot verify: {sp_value}"))
            return
        if not _resolve(yaml_dir, sp_value).is_file():
            issues.append(Issue("ERROR", "system_prompt", f"file not found: {sp_value}"))


def _check_tool_yaml_content(tool_path: Path, idx: int, issues: list[Issue]) -> str | None:
    try:
        with open(tool_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        issues.append(Issue("ERROR", "tool", f"tools[{idx}] YAML parse error: {exc}"))
        return None

    if not isinstance(raw, dict):
        issues.append(Issue("ERROR", "tool", f"tools[{idx}] YAML is not a mapping"))
        return None

    raw_dict = cast(dict[str, Any], raw)

    # Try pydantic validation if nexau available
    try:
        from pydantic import ValidationError
        from nexau.archs.tool.tool import ToolYamlSchema
        ToolYamlSchema.model_validate(raw_dict)
    except ImportError:
        pass
    except Exception as exc:
        issues.append(Issue("ERROR", "tool", f"tools[{idx}] schema validation: {exc}"))

    return raw_dict.get("binding")


def _check_tools(config: dict[str, Any], yaml_dir: Path, issues: list[Issue]) -> None:
    tools_raw = config.get("tools", [])
    if not isinstance(tools_raw, list):
        return

    for idx, entry in enumerate(cast(list[Any], tools_raw)):
        if not isinstance(entry, dict):
            issues.append(Issue("ERROR", "tool", f"tools[{idx}] is not a mapping"))
            continue

        entry_dict = cast(dict[str, Any], entry)
        yaml_path_val = entry_dict.get("yaml_path")

        if not yaml_path_val or not isinstance(yaml_path_val, str):
            issues.append(Issue("ERROR", "tool", f"tools[{idx}] missing 'yaml_path'"))
            continue
        if _has_env_var(yaml_path_val):
            issues.append(Issue("WARNING", "tool",
                                f"tools[{idx}] yaml_path has env var: {yaml_path_val}"))
            continue

        resolved = _resolve(yaml_dir, yaml_path_val)
        if not resolved.is_file():
            issues.append(Issue("ERROR", "tool",
                                f"tools[{idx}] yaml_path not found: {yaml_path_val}"))
            continue

        _check_tool_yaml_content(resolved, idx, issues)


# ── Phase 4: Middleware import paths ──────────────────────────────


def _resolve_middleware_path(import_str: str, yaml_dir: Path) -> Path | None:
    """Resolve 'module.path:ClassName' to a Python file relative to yaml_dir.

    For imports like 'middleware.long_tool_output:LongToolOutputMiddleware',
    the module path 'middleware.long_tool_output' maps to
    'middleware/long_tool_output.py' or 'middleware/long_tool_output/__init__.py'.

    For imports starting with 'nexau.', they are framework imports and NOT local.
    """
    if ":" not in import_str:
        return None

    module_part = import_str.split(":")[0]

    if module_part.startswith("nexau."):
        return None

    rel_path = module_part.replace(".", "/")

    # Try as .py file
    py_file = yaml_dir / f"{rel_path}.py"
    if py_file.is_file():
        return py_file

    # Try as package (__init__.py)
    init_file = yaml_dir / rel_path / "__init__.py"
    if init_file.is_file():
        return init_file

    return None


def _check_middlewares(config: dict[str, Any], yaml_dir: Path, issues: list[Issue]) -> None:
    middlewares = config.get("middlewares", [])
    if not isinstance(middlewares, list):
        return

    for idx, entry in enumerate(cast(list[Any], middlewares)):
        if not isinstance(entry, dict):
            continue

        entry_dict = cast(dict[str, Any], entry)
        import_str = entry_dict.get("import")

        if not import_str or not isinstance(import_str, str):
            issues.append(Issue("WARNING", "middleware",
                                f"middlewares[{idx}] missing 'import' key"))
            continue

        if ":" not in import_str:
            issues.append(Issue("ERROR", "middleware",
                                f"middlewares[{idx}] import missing ':' separator: {import_str}"))
            continue

        module_part, class_name = import_str.split(":", 1)

        # Framework imports (nexau.*) — just check format
        if module_part.startswith("nexau."):
            continue

        # Local import — resolve to file
        resolved = _resolve_middleware_path(import_str, yaml_dir)
        if resolved is None:
            issues.append(Issue("ERROR", "middleware",
                                f"middlewares[{idx}] local module not found: {module_part} "
                                f"(expected {module_part.replace('.', '/')}.py or "
                                f"{module_part.replace('.', '/')}/__init__.py)"))
            continue

        # Check class is defined or re-exported in the file
        try:
            source = resolved.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(resolved))

            # Collect directly defined classes
            exported_names: set[str] = {
                node.name for node in ast.walk(tree)
                if isinstance(node, ast.ClassDef)
            }

            # Collect re-exported names (from X import Y, from X import Y as Z)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        exported_names.add(alias.asname or alias.name)

            # Check __all__ if present
            for node in ast.walk(tree):
                if (isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "__all__"
                                for t in node.targets)):
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                exported_names.add(elt.value)

            if class_name not in exported_names:
                issues.append(Issue("ERROR", "middleware",
                                    f"middlewares[{idx}] class '{class_name}' not found in "
                                    f"{resolved.relative_to(yaml_dir)}"))
        except SyntaxError as exc:
            issues.append(Issue("ERROR", "middleware",
                                f"middlewares[{idx}] Python syntax error in "
                                f"{resolved.relative_to(yaml_dir)}: {exc}"))


# ── Phase 5: Hooks (legacy format) ───────────────────────────────


def _check_hooks(config: dict[str, Any], issues: list[Issue]) -> None:
    for field_name in ("after_model_hooks", "after_tool_hooks",
                       "before_model_hooks", "before_tool_hooks", "tracers"):
        entries = config.get(field_name)
        if not isinstance(entries, list):
            continue
        for idx, entry in enumerate(cast(list[Any], entries)):
            if isinstance(entry, dict):
                imp = cast(dict[str, Any], entry).get("import")
                if isinstance(imp, str) and ":" not in imp:
                    issues.append(Issue("WARNING", "hook",
                                        f"{field_name}[{idx}] import missing ':': {imp}"))


# ── Phase 6: Skills ──────────────────────────────────────────────


def _check_skills(config: dict[str, Any], yaml_dir: Path, issues: list[Issue]) -> None:
    skills = config.get("skills", [])
    if not isinstance(skills, list):
        return

    for idx, entry in enumerate(cast(list[Any], skills)):
        if not isinstance(entry, str):
            issues.append(Issue("ERROR", "skill", f"skills[{idx}] is not a string path"))
            continue
        if _has_env_var(entry):
            continue

        skill_dir = _resolve(yaml_dir, entry)
        if not skill_dir.is_dir():
            issues.append(Issue("ERROR", "skill",
                                f"skills[{idx}] directory not found: {entry}"))
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            issues.append(Issue("ERROR", "skill",
                                f"skills[{idx}] SKILL.md not found in {entry}"))
            continue

        content = skill_md.read_text(encoding="utf-8")
        if not content.startswith("---"):
            issues.append(Issue("ERROR", "skill",
                                f"skills[{idx}] SKILL.md has no YAML frontmatter"))


# ── Phase 7: Python syntax check ─────────────────────────────────


def _check_python_syntax(yaml_dir: Path, issues: list[Issue]) -> None:
    """Check Python syntax of all .py files under tools/ and middleware/."""
    dirs_to_check = ["tools", "middleware"]

    for dir_name in dirs_to_check:
        target = yaml_dir / dir_name
        if not target.is_dir():
            continue

        for py_file in target.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8")
                ast.parse(source, filename=str(py_file))
            except SyntaxError as exc:
                rel = py_file.relative_to(yaml_dir)
                issues.append(Issue("ERROR", "python_syntax",
                                    f"{rel}: line {exc.lineno}: {exc.msg}"))


# ── Main validator ────────────────────────────────────────────────


def validate_agent_yaml(
    yaml_path: str,
    *,
    check_python: bool = True,
) -> Report:
    report = Report(yaml_path=yaml_path)
    path = Path(yaml_path).resolve()

    if not path.is_file():
        report.issues.append(Issue("ERROR", "file", f"file not found: {yaml_path}"))
        return report

    # 1. Schema validation via nexau (if available)
    _validate_schema(str(path), report.issues)

    # 2. Load raw YAML
    config = _load_raw(path, report.issues)
    if config is None:
        return report

    yaml_dir = path.parent

    # 3. File reference checks
    _check_system_prompt(config, yaml_dir, report.issues)
    _check_tools(config, yaml_dir, report.issues)
    _check_middlewares(config, yaml_dir, report.issues)
    _check_hooks(config, report.issues)
    _check_skills(config, yaml_dir, report.issues)

    # 4. Python syntax check
    if check_python:
        _check_python_syntax(yaml_dir, report.issues)

    return report


# ── Output ────────────────────────────────────────────────────────


def _print_report(report: Report) -> None:
    print(f"Validating: {report.yaml_path}")
    print("=" * 60)

    if report.issues:
        print()
        for issue in report.issues:
            sev = issue.severity.ljust(7)
            cat = issue.category.ljust(14)
            print(f"[{sev}] {cat} | {issue.message}")
        print()
    else:
        print("\n  No issues found.\n")

    print("=" * 60)
    status = "VALID" if report.is_valid else "INVALID"
    print(f"Result: {status} ({report.error_count} errors, {report.warning_count} warnings)")


def _print_json(report: Report) -> None:
    data = {
        "yaml_path": report.yaml_path,
        "valid": report.is_valid,
        "error_count": report.error_count,
        "warning_count": report.warning_count,
        "issues": [asdict(i) for i in report.issues],
    }
    print(json_mod.dumps(data, indent=2, ensure_ascii=False))


# ── CLI ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a NexAU agent YAML config and all its references.",
    )
    parser.add_argument("agent_yaml", help="Path to the agent YAML file")
    parser.add_argument("--no-python-check", action="store_true",
                        help="Skip Python syntax checking")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON")
    args = parser.parse_args()

    report = validate_agent_yaml(
        args.agent_yaml,
        check_python=not args.no_python_check,
    )

    if args.json_output:
        _print_json(report)
    else:
        _print_report(report)

    sys.exit(0 if report.is_valid else 1)


if __name__ == "__main__":
    main()
