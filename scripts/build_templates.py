#!/usr/bin/env python3
"""Build one E2B sandbox template per task in a dataset.

Each template starts from the task's docker image (or Dockerfile) and gets
the AHE runtime baked in: ``uv`` + a Python venv at ``/opt/nexau-venv`` with
NexAU and harbor pre-installed. AHE rollouts then attach to a sandbox
launched from this prebuilt template, skipping per-rollout install time.

Layout assumed for ``--dataset-dir``:

    <dataset-dir>/<task_name>/task.toml
    <dataset-dir>/<task_name>/environment/Dockerfile  # optional fallback

``task.toml`` must declare an ``[environment]`` table with at least
``docker_image`` (or omit it and provide ``environment/Dockerfile``).
``cpus`` and ``memory`` (e.g. ``"2G"``) are read from the same table.

Usage:

    # Build everything in the dataset, 16 in parallel.
    python3 scripts/build_templates.py --dataset-dir /path/to/dataset -j 16

    # Build only specific tasks by name.
    python3 scripts/build_templates.py --dataset-dir /path/to/dataset task_a task_b

    # Skip tasks already present on E2B.
    python3 scripts/build_templates.py --dataset-dir /path/to/dataset --missing-only

    # Re-run only tasks whose latest build failed.
    python3 scripts/build_templates.py --dataset-dir /path/to/dataset --retry-failed

Required environment:
    E2B_API_KEY                       (and E2B_API_URL/E2B_DOMAIN if self-hosted)
Optional environment:
    DOCKER_REGISTRY_USERNAME / DOCKER_REGISTRY_PASSWORD  (private base images)
"""

import argparse
import asyncio
import functools
import os
import sys
import tomllib
from pathlib import Path
from typing import Optional

# Flush every print so live progress shows up under tee/tmux/log piping.
print = functools.partial(print, flush=True)

from e2b import AsyncTemplate, Template
from e2b.api.client.api.templates import (
    get_templates_aliases_alias,
    get_templates_template_id,
)
from e2b.api.client.models.error import Error
from e2b.api.client.models.template_build_status import TemplateBuildStatus
from e2b.api.client_async import get_api_client
from e2b.connection_config import ConnectionConfig
from e2b.template_async.build_api import check_alias_exists


# --- Tunables baked into every template ----------------------------------
# Empty UV_VERSION installs the latest uv; pin to e.g. "0.7.13" if you need
# reproducible template builds.
UV_VERSION = ""
NEXAU_PYTHON = "3.13"
NEXAU_VENV = "/opt/nexau-venv"

# Default packages installed into the template's nexau venv. Keep these in
# sync with the runtime deps in pyproject.toml so a sandbox spawned from a
# template runs the same NexAU/harbor as the host driving evolve.py.
DEFAULT_NEXAU_PACKAGES = [
    "git+https://github.com/nex-agi/NexAU.git@v0.3.9",
    "git+https://github.com/Curry09/harbor-LJH.git",
]


def parse_mem_mb(s: str) -> int:
    """Convert ``"2G"`` / ``"512M"`` / ``"1024"`` from task.toml to MiB."""
    s = s.strip().upper()
    if s.endswith("G"):
        return int(float(s[:-1]) * 1024)
    if s.endswith("M"):
        return int(float(s[:-1]))
    return int(s)


def _nexau_steps(tpl: Template, nexau_packages: list[str]) -> Template:
    """Append the standard install chain (apt + uv + venv + packages) to ``tpl``.

    The resulting image has ``uv`` available, a fresh Python venv at
    ``NEXAU_VENV`` containing ``nexau_packages``, and a bashrc shim that
    pre-pends ``~/.local/bin`` and exports ``NEXAU_VENV`` for interactive use.
    """
    uv_url = (
        f"https://astral.sh/uv/{UV_VERSION}/install.sh"
        if UV_VERSION
        else "https://astral.sh/uv/install.sh"
    )
    install_uv = f"(curl -LsSf {uv_url} | sh) || pip install uv"
    setup_venv = (
        f'export PATH="$HOME/.local/bin:$PATH"'
        f' && (test -f "$HOME/.local/bin/env" && . "$HOME/.local/bin/env" || true)'
        f" && uv python install {NEXAU_PYTHON}"
        f" && uv venv {NEXAU_VENV} --python {NEXAU_PYTHON} --clear"
    )
    install_pkgs = (
        f'export PATH="$HOME/.local/bin:$PATH"'
        f' && (test -f "$HOME/.local/bin/env" && . "$HOME/.local/bin/env" || true)'
        f" && . {NEXAU_VENV}/bin/activate"
        + "".join(f" && uv pip install {p}" for p in nexau_packages)
    )
    bashrc_path = (
        f'grep -q "NEXAU_VENV" "$HOME/.bashrc" ||'
        f' echo \'export PATH="$HOME/.local/bin:$PATH"'
        f"\nexport NEXAU_VENV={NEXAU_VENV}' >> \"$HOME/.bashrc\""
    )
    return (
        tpl
        .apt_install(["curl", "build-essential", "git"])
        .run_cmd(install_uv)
        .run_cmd(setup_venv)
        .run_cmd(install_pkgs)
        .run_cmd(bashrc_path)
    )


def make_template_from_image(docker_image: str, nexau_packages: list[str]) -> Template:
    """Seed a template from a prebuilt Docker image (private registry creds optional)."""
    tpl = (
        Template()
        .from_image(
            image=docker_image,
            username=os.environ.get("DOCKER_REGISTRY_USERNAME"),
            password=os.environ.get("DOCKER_REGISTRY_PASSWORD"),
        )
        .set_user("root")
    )
    return _nexau_steps(tpl, nexau_packages)


def make_template_from_dockerfile(dockerfile_path: str, nexau_packages: list[str]) -> Template:
    """Seed a template from a Dockerfile sitting in the task's ``environment/`` dir."""
    tpl = (
        Template()
        .from_dockerfile(dockerfile_content_or_path=dockerfile_path)
        .set_user("root")
    )
    return _nexau_steps(tpl, nexau_packages)


def task_alias(task_dir: Path) -> str:
    """E2B aliases reject ``.``; replace with ``-`` to derive a stable alias."""
    return task_dir.name.replace(".", "-")


async def filter_missing_templates(dirs: list[Path], jobs: int) -> list[Path]:
    """Keep only tasks with no E2B alias yet (never built / build failed before alias write)."""
    config = ConnectionConfig()
    api_client = get_api_client(
        config, require_api_key=True, require_access_token=False
    )
    sem = asyncio.Semaphore(jobs)

    async def check(d: Path) -> Optional[Path]:
        alias = task_alias(d)
        async with sem:
            exists = await check_alias_exists(api_client, alias)
        return None if exists else d

    results = await asyncio.gather(*(check(d) for d in dirs))
    missing = sorted((r for r in results if r is not None), key=lambda p: p.name)
    print(f"Missing templates (no alias on E2B): {len(missing)} / {len(dirs)}")
    return missing


async def filter_retry_failed_templates(dirs: list[Path], jobs: int) -> list[Path]:
    """Keep tasks with no alias OR whose newest build status is ERROR (per E2B API).

    Stricter than ``filter_missing_templates`` because it inspects each
    template's build history instead of just alias existence.
    """
    config = ConnectionConfig()
    api_client = get_api_client(
        config, require_api_key=True, require_access_token=False
    )
    sem = asyncio.Semaphore(jobs)

    async def needs_retry(d: Path) -> Optional[Path]:
        alias = task_alias(d)
        async with sem:
            ar = await get_templates_aliases_alias.asyncio_detailed(
                alias=alias, client=api_client
            )
            if ar.status_code == 404:
                return d
            if ar.status_code >= 300:
                return d
            parsed = ar.parsed
            if isinstance(parsed, Error) or parsed is None:
                return d
            tid = parsed.template_id
            tr = await get_templates_template_id.asyncio_detailed(
                template_id=tid, client=api_client
            )
            if tr.status_code == 404 or tr.status_code >= 300:
                return d
            tw = tr.parsed
            if isinstance(tw, Error) or tw is None:
                return d
            if not tw.builds:
                return d
            latest = max(tw.builds, key=lambda b: b.updated_at)
            if latest.status == TemplateBuildStatus.ERROR:
                return d
        return None

    results = await asyncio.gather(*(needs_retry(d) for d in dirs))
    retry = sorted((r for r in results if r is not None), key=lambda p: p.name)
    print(f"Retry queue (no alias or latest build=error): {len(retry)} / {len(dirs)}")
    return retry


async def build_one(task_dir: Path, nexau_packages: list[str]) -> str:
    """Build one E2B template from a single task dir. Returns 'ok' or 'skip'."""
    name = task_dir.name
    alias = task_alias(task_dir)
    with open(task_dir / "task.toml", "rb") as f:
        cfg = tomllib.load(f)

    env = cfg.get("environment", {})
    image = env.get("docker_image")
    cpus = env.get("cpus", 1)
    mem = parse_mem_mb(env.get("memory", "2G"))

    if image:
        # Prefer the prebuilt image when task.toml declares one.
        print(f"BUILD {name}  alias={alias}  image={image}  cpu={cpus}  mem={mem}MB")
        tpl = make_template_from_image(image, nexau_packages)
    else:
        # Fall back to a Dockerfile shipped alongside the task.
        dockerfile = task_dir / "environment" / "Dockerfile"
        if not dockerfile.exists():
            print(f"SKIP  {name}: no docker_image and no Dockerfile")
            return "skip"
        print(f"BUILD {name}  alias={alias}  dockerfile={dockerfile}  cpu={cpus}  mem={mem}MB")
        tpl = make_template_from_dockerfile(str(dockerfile), nexau_packages)

    info = await AsyncTemplate.build(
        template=tpl, alias=alias, cpu_count=cpus, memory_mb=mem,
    )
    print(f"DONE  {name}  id={info.template_id}")
    return "ok"


async def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        required=True,
        help="root dir holding one subdir per task (each with task.toml)",
    )
    parser.add_argument("names", nargs="*", help="task names to build (default: all in dataset)")
    parser.add_argument("-j", "--jobs", type=int, default=16, help="concurrency (default: 16)")
    parser.add_argument(
        "--nexau-package",
        action="append",
        default=None,
        help=(
            "git/pip spec to install into the template venv. Repeat for multiple "
            "packages. If omitted, a public NexAU + harbor pair matching pyproject is used."
        ),
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="only build tasks with no existing E2B alias",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="only build tasks with no alias or whose latest build status is ERROR",
    )
    args = parser.parse_args()

    if args.missing_only and args.retry_failed:
        print("Use only one of --missing-only or --retry-failed", file=sys.stderr)
        sys.exit(2)

    dataset = args.dataset_dir
    if not dataset.is_dir():
        print(f"--dataset-dir does not exist or is not a directory: {dataset}", file=sys.stderr)
        sys.exit(2)

    nexau_packages = args.nexau_package or DEFAULT_NEXAU_PACKAGES

    dirs = sorted(
        p for p in dataset.iterdir()
        if p.is_dir() and (p / "task.toml").exists()
    )
    if args.names:
        names = set(args.names)
        dirs = [d for d in dirs if d.name in names]

    if args.retry_failed:
        dirs = await filter_retry_failed_templates(dirs, min(args.jobs, 32))
    elif args.missing_only:
        dirs = await filter_missing_templates(dirs, args.jobs)

    total = len(dirs)
    if total == 0:
        print("Nothing to build.")
        return
    print(f"Found {total} tasks, concurrency={args.jobs}")

    sem = asyncio.Semaphore(args.jobs)
    done = {"ok": 0, "fail": 0, "skip": 0}

    async def worker(d: Path):
        async with sem:
            try:
                result = await build_one(d, nexau_packages)
                done[result] += 1
            except Exception as e:
                done["fail"] += 1
                print(f"FAIL  {d.name}: {e}")
            finished = done["ok"] + done["fail"] + done["skip"]
            print(f"[{finished}/{total}] ok={done['ok']} fail={done['fail']} skip={done['skip']}")

    await asyncio.gather(*(worker(d) for d in dirs))
    print(f"\nAll done. ok={done['ok']} fail={done['fail']} skip={done['skip']}")


if __name__ == "__main__":
    asyncio.run(main())
