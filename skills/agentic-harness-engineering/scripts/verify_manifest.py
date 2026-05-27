#!/usr/bin/env python3
"""
Verify Change Manifest — 读取 manifests/ 下的 pending manifest，
对比评估结果，更新 verification 字段并给出 verdict。

用法:
  python verify_manifest.py --workspace /path/to/workspace
                           [--manifest manifests/change_xxx.json]
                           [--results results.json]
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))


def load_pending_manifests(root: Path) -> list:
    """读取所有 pending 状态的 manifest"""
    manifests_dir = root / "manifests"
    if not manifests_dir.is_dir():
        return []

    pending = []
    for f in sorted(manifests_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            status = data.get("verification", {}).get("status")
            if status == "pending" or status == "draft":
                pending.append((f, data))
        except (json.JSONDecodeError, KeyError):
            continue
    return pending


def evaluate_predictions(changes: list, results: dict) -> dict:
    """对比 changes 中的预测与 results 中的评估结果"""
    expected_fixes_verified = []
    unexpected_fixes = []
    regressions_observed = []
    false_predictions = []

    # 获取本轮结果
    all_passed = set(results.get("passed", []))
    all_failed = set(results.get("failed", []))

    for change in changes:
        predicted = change.get("predicted_impact", {})
        expected = set(predicted.get("expected_fixes", []))
        at_risk = set(predicted.get("at_risk_regressions", []))

        # 检查预期修复是否真的通过了
        for task_id in expected:
            if task_id in all_passed:
                expected_fixes_verified.append(task_id)

        # 检查是否有预期之外的通过
        for task_id in all_passed:
            if task_id not in expected and task_id != "":
                unexpected_fixes.append(task_id)

        # 检查是否有回归
        for task_id in at_risk:
            if task_id in all_failed:
                regressions_observed.append(task_id)

        # 检查预测错误
        for task_id in expected:
            if task_id in all_failed:
                false_predictions.append(f"{task_id}: expected pass but failed")

        for task_id in at_risk:
            if task_id in all_passed:
                false_predictions.append(f"{task_id}: expected regression but passed")

    # 计算 verdict
    if regressions_observed:
        verdict = "revert"
    elif false_predictions and not expected_fixes_verified:
        verdict = "revert"
    elif expected_fixes_verified and not regressions_observed:
        verdict = "keep"
    else:
        verdict = "partial"

    return {
        "expected_fixes_verified": expected_fixes_verified,
        "unexpected_fixes": unexpected_fixes,
        "regressions_observed": regressions_observed,
        "false_predictions": false_predictions,
        "verdict": verdict
    }


def verify_manifest(root: Path, manifest_path: str = None, results_path: str = None):
    root = Path(root).resolve()

    if manifest_path:
        manifest_files = [(Path(manifest_path), json.loads(Path(manifest_path).read_text(encoding="utf-8")))]
    else:
        manifest_files = load_pending_manifests(root)

    if not manifest_files:
        print("[WARN] No pending manifests found")
        return

    # 加载评估结果
    results = {"passed": [], "failed": []}
    if results_path:
        try:
            results = json.loads(Path(results_path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError) as e:
            print(f"[WARN] Could not load results: {e}")

    for fpath, data in manifest_files:
        print(f"\n[Verifying] {fpath.relative_to(root)}")
        print(f"  Iteration: {data.get('iteration', '?')}")
        print(f"  Changes: {len(data.get('changes', []))}")

        if not data.get("changes"):
            print(f"  [SKIP] No changes to verify")
            continue

        result = evaluate_predictions(data.get("changes", []), results)
        now = datetime.now(TZ).isoformat()

        # 更新 manifest
        status_map = {"keep": "verified", "revert": "reverted"}
        data["verification"]["status"] = status_map.get(result["verdict"], "partial")
        data["verification"]["completed_at"] = now
        data["verification"]["result"] = {
            "expected_fixes_verified": result["expected_fixes_verified"],
            "unexpected_fixes": result["unexpected_fixes"],
            "regressions_observed": result["regressions_observed"],
            "false_predictions": result["false_predictions"]
        }
        data["verification"]["verdict"] = result["verdict"]

        # 写回文件
        fpath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

        # 输出摘要
        print(f"  Verdict: {result['verdict'].upper()}")
        if result["expected_fixes_verified"]:
            print(f"  Fixed: {', '.join(result['expected_fixes_verified'])}")
        if result["regressions_observed"]:
            print(f"  Regressed: {', '.join(result['regressions_observed'])}")
        if result["false_predictions"]:
            print(f"  Mispredictions: {', '.join(result['false_predictions'])}")

        print(f"  [OK] Updated: {fpath.name}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Verify HARNESS.md Change Manifest")
    parser.add_argument("--workspace", "-w", default=".", help="Workspace root path")
    parser.add_argument("--manifest", "-m", help="Specific manifest file to verify")
    parser.add_argument("--results", "-r", help="Evaluation results JSON file")
    args = parser.parse_args()

    verify_manifest(args.workspace, args.manifest, args.results)
