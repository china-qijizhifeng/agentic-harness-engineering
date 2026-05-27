# results.json — 评估结果标准格式

> HARNESS.md v1.0 中用于 Verify Changes 的评估结果数据格式。

## 格式定义

```json
{
  "version": "1.0",
  "iteration": 3,
  "timestamp": "2026-05-21T10:30:00+08:00",
  "total": 12,
  "passed": ["T-001", "T-002", "T-003", "T-005", "T-008", "T-009", "T-011", "T-012"],
  "failed": ["T-004", "T-006", "T-007", "T-010"],
  "traces": {
    "T-001": "traces/task-001.json",
    "T-004": "traces/task-004.json"
  },
  "summary": {
    "pass_rate": 66.7,
    "fail_rate": 33.3,
    "new_failures": ["T-010"],
    "fixed_issues": ["T-003"]
  }
}
```

## 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `version` | string | ✅ | 格式版本号 |
| `iteration` | integer | ✅ | 当前迭代编号（与 Change Manifest 的 iteration 字段对应） |
| `timestamp` | string (ISO 8601) | ✅ | 评估完成时间 |
| `total` | integer | ✅ | 本轮总任务数 |
| `passed` | string[] | ✅ | 通过的任务 ID 列表 |
| `failed` | string[] | ✅ | 失败的任务 ID 列表 |
| `traces` | object | 推荐 | 任务 ID → trace 文件路径的映射 |
| `summary.pass_rate` | number | 推荐 | 通过率百分比 |
| `summary.fail_rate` | number | 推荐 | 失败率百分比 |
| `summary.new_failures` | string[] | 推荐 | 本轮新增的失败（上一轮 pass 本轮 fail） |
| `summary.fixed_issues` | string[] | 推荐 | 本轮修复的失败（上一轮 fail 本轮 pass） |

## 使用方式

### 与 verify_manifest.py 配合

```bash
# 评估完成后生成 results.json
python3 evaluate.py > results.json

# 传入 verify 脚本验证 Change Manifest 预测
python3 AHE.skill/scripts/verify_manifest.py \
  --workspace . \
  --results results.json
```

### 与 generate_manifest.py 配合

generate_manifest.py 生成的 Change Manifest 中的 `expected_fixes` 和 `at_risk_regressions` 字段应与 results.json 的 `passed` / `failed` 列表对比验证。

## 示例

```json
{
  "version": "1.0",
  "iteration": 1,
  "timestamp": "2026-05-21T00:00:00+08:00",
  "total": 5,
  "passed": ["T-001", "T-003", "T-005"],
  "failed": ["T-002", "T-004"],
  "traces": {
    "T-002": "traces/task-002-tool-error.json",
    "T-004": "traces/task-004-memory-oom.json"
  },
  "summary": {
    "pass_rate": 60.0,
    "fail_rate": 40.0,
    "new_failures": ["T-002", "T-004"],
    "fixed_issues": []
  }
}
```
