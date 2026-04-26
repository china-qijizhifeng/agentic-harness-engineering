"""
Explore-Agent Runner

并行运行两个 sub-agent（源码探索 + Web 调研），
将蒸馏后的知识写入 skill 文件，供 evolve agent 使用。

主要入口：run_explore_agent(config, exp_dir)
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

AUTO_DIR = Path(__file__).resolve().parent.parent.parent

ML_SKILL_NAMES = [
    "nexau-framework-internals",
    "coding-agent-sota-research",
]

SOURCE_AGENT_SKILLS = [
    "nexau-framework-internals",
]

WEB_AGENT_SKILLS = [
    "coding-agent-sota-research",
]

CODE_SOURCES_DIR = AUTO_DIR / ".code_sources"

_HISTORY_FLUSH_INTERVAL_SEC = 30


def _dump_agent_to_disk(agent: "Agent | None", output_dir: Path) -> None:  # noqa: F821
    """将 agent 的 history 和 tracer 保存到磁盘。"""
    if agent is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        history_snapshot = list(agent.history)
        history_messages = [msg.model_dump(mode="json") for msg in history_snapshot]
        history_path = output_dir / "explore_agent_history.json"
        tmp_path = str(history_path) + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(history_messages, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(history_path))
    except Exception as e:
        sys.stderr.write(f"[explore-agent] flush history 失败: {e}\n")

    try:
        from nexau.archs.tracer.adapters.in_memory import InMemoryTracer
        tracers_snapshot = list(agent.config.tracers)
        for tracer in tracers_snapshot:
            if isinstance(tracer, InMemoryTracer):
                tracer_path = output_dir / "explore_agent_tracer.json"
                tmp_path = str(tracer_path) + ".tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(tracer.dump_traces(), f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, str(tracer_path))
                break
    except Exception as e:
        sys.stderr.write(f"[explore-agent] flush tracer 失败: {e}\n")


def _periodic_flush(agent_ref: list, output_dir: Path, stop_event: threading.Event) -> None:
    """后台线程：定期将 history 和 tracer flush 到磁盘。"""
    while not stop_event.wait(_HISTORY_FLUSH_INTERVAL_SEC):
        agent = agent_ref[0] if agent_ref else None
        if agent is not None:
            _dump_agent_to_disk(agent, output_dir)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """深度合并两个 dict，overlay 覆盖 base。"""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def prepare_code_sources(config: dict) -> dict[str, Path]:
    """准备源码目录，返回 {name: local_path} 映射。

    获取优先级：
    1. local_fallback（本地已有仓库，如 ../nexau）
    2. .code_sources/ 缓存（之前已 clone）
    3. git clone 到 .code_sources/（首次使用）
    """
    sources = config["explore_agent"]["code_sources"]
    paths = {}

    for name, src_cfg in sources.items():
        if src_cfg["type"] == "local":
            path = Path(src_cfg["path"])
            if not path.is_absolute():
                path = (AUTO_DIR / path).resolve()
            if path.exists():
                paths[name] = path
                print(f"[explore-agent] 使用本地路径 {name} → {path}")
            else:
                print(f"[explore-agent] ⚠️ 本地路径不存在: {path}")

        elif src_cfg["type"] == "git":
            local_fallback = src_cfg.get("local_fallback")
            if local_fallback:
                fallback_path = (AUTO_DIR / local_fallback).resolve()
                if fallback_path.exists():
                    paths[name] = fallback_path
                    print(f"[explore-agent] 使用本地 fallback {name} → {fallback_path}")
                    continue

            ref = src_cfg.get("ref", "main")
            clone_dir = CODE_SOURCES_DIR / f"{name}@{ref}"
            if not clone_dir.exists():
                CODE_SOURCES_DIR.mkdir(parents=True, exist_ok=True)
                print(f"[explore-agent] 正在 clone {name}@{ref}...")
                subprocess.run(
                    [
                        "git", "clone", "--depth", "1",
                        "--branch", ref,
                        src_cfg["url"], str(clone_dir),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                print(f"[explore-agent] 已 clone {name}@{ref} → {clone_dir}")
            else:
                print(f"[explore-agent] 使用缓存 {name}@{ref} → {clone_dir}")
            paths[name] = clone_dir

    return paths


def _build_source_agent_query(
    nexau_path: Path,
    output_skill_dir: Path,
) -> str:
    """构建源码探索 agent 的初始 query。"""
    return "\n".join([
        "开始执行 NexAU 框架源码探索任务。",
        "",
        "## 源码位置（只读）",
        f"- NexAU 框架: `{nexau_path}`",
        "",
        "## 输出位置",
        f"- Skill 输出目录: `{output_skill_dir}/`",
        "",
        "## 你的交付物",
        "",
        "1. `nexau-framework-internals/SKILL.md`",
        "",
        "## 执行指令",
        "按照系统提示词的指引，探索 NexAU 框架源码并写入 skill 文件。",
        "用 read_file（带 offset/limit）而不是 run_shell_command 读文件。",
        "完成后调用 complete_task。",
    ])


def _build_web_agent_query(
    output_skill_dir: Path,
    web_sources: list[dict],
) -> str:
    """构建 Web 调研 agent 的初始 query。"""
    lines = [
        "开始执行 SOTA 调研任务。",
        "",
        "## 输出位置",
        f"- Skill 输出目录: `{output_skill_dir}/`",
        "",
        "## 你的交付物",
        "",
        "1. `coding-agent-sota-research/SKILL.md` — 架构、benchmark、技术、ablation",
        "",
    ]

    if web_sources:
        lines.append("## 必须阅读的 Web 页面")
        for ws in web_sources:
            lines.append(f"- **{ws['url']}** — {ws.get('focus', '')}")
        lines.append("")

    lines.extend([
        "## 执行指令",
        "1. 先用 WebFetch 读取上面列出的所有 URL",
        "2. 读完后立即 write_file 写出 skill 文件初版",
        "3. 然后用 WebSearch 进行自主深度调研（15-20 次搜索）",
        "4. 用新发现更新 skill 文件",
        "5. 调用 complete_task 完成",
        "",
        "⚠️ 关键规则：",
        "- 先写初版再扩展，不要等到调研完才写",
        "- 记录精确数据和引用 URL",
        "- coding-agent-sota-research 目标 400-800 行",
        "- 架构/middleware/工具/ablation 等内容均写入 coding-agent-sota-research",
    ])

    return "\n".join(lines)


def _prepare_agent_config(
    base_config_path: Path,
    agent_patch: dict,
) -> Path:
    """读取 base agent yaml，应用 patch，写入同目录临时文件并返回。

    使用 tempfile 生成唯一文件名，避免并发实验写同一文件的竞争。
    临时文件与 base 同目录，确保 yaml 内的相对路径仍能正确解析。
    如果没有 patch，直接返回 base_config_path（调用方通过比较判断是否需清理）。
    """
    if not agent_patch:
        return base_config_path

    with open(base_config_path, encoding="utf-8") as f:
        agent_yaml = yaml.safe_load(f)
    patched = _deep_merge(agent_yaml, agent_patch)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(base_config_path.parent),
        suffix=".yaml",
        prefix="_patched_",
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        yaml.dump(patched, f, default_flow_style=False, allow_unicode=True)
    return Path(tmp_path)


def _run_single_agent(
    agent_name: str,
    config_path: Path,
    query: str,
    context: dict,
    output_dir: Path,
) -> bool:
    """运行单个 explore-agent sub-agent。在独立线程中调用，不修改 sys.stdout。

    返回 True 表示 agent 正常完成（不一定产出所有 skill）。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = output_dir / "explore_agent.log"

    agent_ref: list = [None]
    stop_flush = threading.Event()
    flush_thread: threading.Thread | None = None
    log_fh = None

    try:
        from nexau import Agent

        log_fh = open(log_file_path, "w", encoding="utf-8")

        def _log(msg: str) -> None:
            line = f"[{agent_name}] {msg}\n"
            sys.stderr.write(line)
            try:
                log_fh.write(line)
                log_fh.flush()
            except (ValueError, OSError):
                pass

        _log(f"启动 agent (config={config_path})")

        agent = Agent.from_yaml(config_path=config_path)
        agent_ref[0] = agent

        flush_thread = threading.Thread(
            target=_periodic_flush,
            args=(agent_ref, output_dir, stop_flush),
            daemon=True,
        )
        flush_thread.start()

        agent.run(message=query, context=context)
        _log("agent 运行完成")
        return True

    except Exception as e:
        sys.stderr.write(f"[{agent_name}] ❌ Agent 运行异常: {e}\n")
        return False

    finally:
        stop_flush.set()
        if flush_thread and flush_thread.is_alive():
            flush_thread.join(timeout=5)
        _dump_agent_to_disk(agent_ref[0], output_dir)
        if log_fh:
            log_fh.close()


def run_explore_agent(config: dict, exp_dir: Path) -> bool:
    """并行运行两个 explore-agent sub-agent，将产出的 skill 写入 exp_dir/skills/。

    - agent_source: 扫描 NexAU 框架源码 → nexau-framework-internals
    - agent_web:    Web 调研 SOTA coding agent 架构  → coding-agent-sota-research

    返回 True 表示成功产出所有 skill，False 表示失败或部分产出。
    """
    from concurrent.futures import ThreadPoolExecutor

    ml_config = config.get("explore_agent", {})
    if not ml_config.get("enabled", False):
        return False

    skills_dir = exp_dir / "evolve_agent" / "skills"
    start_time = time.monotonic()

    for skill_name in ML_SKILL_NAMES:
        (skills_dir / skill_name).mkdir(parents=True, exist_ok=True)

    try:
        code_paths = prepare_code_sources(config)
    except subprocess.CalledProcessError as e:
        print(f"[explore-agent] ❌ 源码准备失败: {e}")
        return False

    if "nexau" not in code_paths:
        print("[explore-agent] ⚠️ 缺少 NexAU 源码，跳过 explore-agent")
        return False

    ml_agent_patch = config.get("explore_agent_patch", {})
    ml_dir = AUTO_DIR / "agents" / "explore_agent"

    source_base = (ml_dir / "source_agent" / "agent.yaml").resolve()
    web_base = (ml_dir / "web_agent" / "agent.yaml").resolve()

    source_config_path = _prepare_agent_config(source_base, ml_agent_patch)
    web_config_path = _prepare_agent_config(web_base, ml_agent_patch)

    temp_configs = [p for p in (source_config_path, web_config_path)
                    if p not in (source_base, web_base)]

    source_query = _build_source_agent_query(
        nexau_path=code_paths["nexau"],
        output_skill_dir=skills_dir,
    )
    web_query = _build_web_agent_query(
        output_skill_dir=skills_dir,
        web_sources=ml_config.get("web_sources", []),
    )

    source_context = {
        "nexau_path": str(code_paths["nexau"]),
        "output_skill_dir": str(skills_dir),
    }
    today_str = datetime.now().strftime("%Y-%m-%d")
    web_context = {
        "output_skill_dir": str(skills_dir),
        "web_sources": ml_config.get("web_sources", []),
        "date": today_str,
    }

    source_output_dir = exp_dir / "explore_agent_output" / "source"
    web_output_dir = exp_dir / "explore_agent_output" / "web"

    print(f"[explore-agent] 🚀 并行启动 2 个 sub-agent:")
    print(f"  agent_source → Skills: {SOURCE_AGENT_SKILLS}")
    print(f"  agent_web    → Skills: {WEB_AGENT_SKILLS}")
    print(f"  source workspace: {source_output_dir}")
    print(f"  web workspace:    {web_output_dir}")

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ml") as pool:
        source_future = pool.submit(
            _run_single_agent,
            "agent_source", source_config_path, source_query,
            source_context, source_output_dir,
        )
        web_future = pool.submit(
            _run_single_agent,
            "agent_web", web_config_path, web_query,
            web_context, web_output_dir,
        )

        source_ok = source_future.result()
        web_ok = web_future.result()

    for tmp in temp_configs:
        tmp.unlink(missing_ok=True)

    elapsed = time.monotonic() - start_time
    produced = [s for s in ML_SKILL_NAMES if (skills_dir / s / "SKILL.md").exists()]
    success = len(produced) == len(ML_SKILL_NAMES)

    print(f"\n[explore-agent] === 并行执行完成 ({elapsed:.0f}s) ===")
    print(f"  agent_source: {'✅' if source_ok else '❌'}")
    print(f"  agent_web:    {'✅' if web_ok else '❌'}")

    if success:
        print(f"[explore-agent] ✅ 全部 {len(ML_SKILL_NAMES)} 个 skill 已生成")
    else:
        missing_skills = [s for s in ML_SKILL_NAMES if s not in produced]
        print(f"[explore-agent] ⚠️ 已生成 {len(produced)}/{len(ML_SKILL_NAMES)} 个 skill")
        print(f"[explore-agent] 缺失: {missing_skills}")

    _save_explore_agent_log(exp_dir, code_paths, produced, elapsed, success)

    return success


def _save_explore_agent_log(
    exp_dir: Path,
    code_paths: dict[str, Path],
    produced: list[str],
    elapsed: float,
    success: bool,
) -> None:
    """保存 explore-agent 执行日志。"""
    log = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_seconds": round(elapsed, 1),
        "success": success,
        "code_sources": {k: str(v) for k, v in code_paths.items()},
        "produced_skills": produced,
        "missing_skills": [s for s in ML_SKILL_NAMES if s not in produced],
    }
    log_path = exp_dir / "explore_agent_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def register_explore_agent_skills(exp_dir: Path) -> None:
    """将 explore-agent 产出的 skill 注册到实验副本的 evolve_agent.yaml，
    并在实验副本的 evolve_prompt.md 追加 skill 引导。"""
    evolve_yaml_path = exp_dir / "evolve_agent" / "evolve_agent.yaml"
    if not evolve_yaml_path.exists():
        print("[explore-agent] ⚠️ evolve_agent.yaml 不存在，跳过注册")
        return

    with open(evolve_yaml_path, encoding="utf-8") as f:
        evolve_config = yaml.safe_load(f)

    existing_skills = evolve_config.get("skills", [])
    registered = []

    for skill_name in ML_SKILL_NAMES:
        skill_path = f"./skills/{skill_name}"
        skill_md = exp_dir / "evolve_agent" / "skills" / skill_name / "SKILL.md"
        if skill_md.exists() and skill_path not in existing_skills:
            existing_skills.append(skill_path)
            registered.append(skill_name)

    if not registered:
        print("[explore-agent] 没有新 skill 需要注册")
        return

    evolve_config["skills"] = existing_skills

    with open(evolve_yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(evolve_config, f, default_flow_style=False, allow_unicode=True)

    print(f"[explore-agent] 已注册 {len(registered)} 个 skill 到 evolve_agent.yaml: {registered}")

    prompt_path = exp_dir / "evolve_agent" / "evolve_prompt.md"
    _ML_PROMPT_MARKER = "### Explore-Agent Skills（自动生成）"
    if prompt_path.exists():
        existing_content = prompt_path.read_text(encoding="utf-8")
        if _ML_PROMPT_MARKER in existing_content:
            print("[explore-agent] evolve_prompt.md 已包含 skill 引导，跳过追加")
            return

        ml_skill_guide = f"""

{_ML_PROMPT_MARKER}

以下 skill 由 explore-agent 自动生成，是你的 **核心知识来源**。可通过 LoadSkill 加载：

| Skill | 覆盖范围 | 何时使用 |
|-------|---------|---------|
| `nexau-framework-internals` | NexAU 框架全面参考：config schema、组件创建模式（middleware/tool/skill/sub-agent）、executor 循环精确序列、hook 边界行为、token 计算方式、sub-agent 生命周期、未文档化的 gotchas | 创建/修改任何框架组件时，以及遇到非预期运行时行为时 |
| `coding-agent-sota-research` | 顶尖 coding agent 架构模式、设计原则、精确 benchmark 分数、实际 prompt 文本、middleware 代码、ablation 结果、负面实验结果、gap analysis 框架 | 决策时需要具体数值/代码参考、或需要架构设计指导时 |
"""
        with open(prompt_path, "a", encoding="utf-8") as f:
            f.write(ml_skill_guide)
        print("[explore-agent] 已追加 skill 引导到 evolve_prompt.md 副本")
