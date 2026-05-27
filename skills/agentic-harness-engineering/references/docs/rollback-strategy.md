# 回滚策略 — Harness Change Rollback

> 对应 AHE 演化循环中的 Verify 步骤：当修改的预测被证伪时，如何安全回滚。

---

## 什么时候需要回滚

根据 Change Manifest 中 `verification.verdict` 的取值：

| Verdict | 含义 | 动作 |
|---------|------|------|
| **keep** | 所有预测正确，修改有效 | 保留修改，无需回滚 |
| **revert** | 修改无效或导致回归 | **立即回滚** |
| **partial** | 部分预测正确，部分错误 | 回滚出问题的部分，保留有效的部分 |

### 触发 revert 的具体条件

满足以下任意一条时，verdict 应为 revert：

1. **预期修复的任务未修复** — `expected_fixes` 中的所有任务本轮仍然 fail
2. **观察到回归** — `at_risk_regressions` 中的任务本轮 fail（原本是 pass 的）
3. **新引入的失败** — 不在预期范围内但新出现的 fail 任务达到 2+ 个
4. **严重度 P0 的回归** — 即使只有 1 个 P0 任务回归，也应 revert

---

## 回滚方式

### 方式 1：git revert（推荐）

如果 Harness 文件在版本控制中：

```bash
# 查看修改涉及的文件
cat manifests/change_20260521_103000.json | python3 -c "
import sys, json
m = json.load(sys.stdin)
for c in m['changes']:
    print(c['file_path'])
"

# 对每个修改的文件执行 git revert
git revert <commit-hash> --no-edit

# 或针对特定文件
git checkout HEAD~1 -- tool_descriptions/search.tool.yaml
```

### 方式 2：文件恢复

如果没有 git，直接从备份恢复：

```bash
# 如果有 pre-update backup
cp .hermes/backups/pre-update/tool_descriptions/search.tool.yaml \
   tool_descriptions/search.tool.yaml
```

### 方式 3：按 partial 回滚

如果 verdict 为 partial，只回滚失败的部分：

| 情况 | 操作 |
|------|------|
| 部分修改有效、部分无效 | 只 revert 无效的 change entry |
| 同文件多修改混在一起 | 手动编辑还原有问题的代码段 |
| 依赖链断裂 | 如果 B 依赖 A 的修改，但 A 被 revert，B 也需要连带 revert |

---

## 回滚后流程

### 1. 记录回滚 Change Manifest

回滚本身也是一次 Harness 修改，应同样生成 Change Manifest：

```json
{
  "manifest_version": "1.0",
  "iteration": 4,
  "changes": [{
    "change_id": "ch_rollback_003",
    "component": "tool_descriptions",
    "subtype": "update",
    "file_path": "tool_descriptions/search.tool.yaml",
    "summary": "Revert change ch_001: search pagination caused regression on T-013",
    "failure_evidence": "T-013 regressed from pass to fail after adding pagination parameters",
    "root_cause": "Pagination parameters changed default behavior from 'top results' to 'page 1 results'",
    "targeted_fix": "Revert search.tool.yaml to iteration 2 state",
    "predicted_impact": {
      "expected_fixes": ["T-013"],
      "at_risk_regressions": ["T-042", "T-057"],
      "rationale": "Reverting to known-good state; search truncation may reappear"
    }
  }],
  "verification": {
    "status": "pending",
    "scheduled_at": "2026-05-22T10:30:00+08:00"
  }
}
```

### 2. 更新 Harness 状态

- 如果使用版本控制：`git push` 推送 revert commit
- 如果使用 change manifest 跟踪：将原始 manifest 的 verification.verdict 更新为 "reverted"
- 记录事故日志

### 3. 重新分析

回滚后不应立即做下一次修改。应先回答：

- **为什么预测错了？** — failure_evidence 是否不够准确？root_cause 是否没找对？
- **是否需要换一个组件层面解决问题？** — 如果 tool_descriptions 修不好，是否应该在 system_rules 中加约束？或者在 middleware 中做自动补全？

---

## 防止无效回滚

| 不要做什么 | 为什么 |
|-----------|--------|
| 没有分析根因就直接 revert | 同样的失败会在下一轮重现 |
| 因为 1 个 P2 回归就 revert 全部修改 | 可能把有效的修复也丢了；优先 partial |
| 回滚后不更新 manifest | 丢失演化历史，无法归因 |
| 回滚后不更新 `at_risk_regressions` 预测 | 下次同样的风险还会再犯 |

---

## 最佳实践

1. **每次修改只改一个组件** — partial revert 更容易
2. **保持 git 提交粒度细** — 每个 change 对应一个 commit，revert 可以精确到单文件
3. **预填 at_risk_regressions** — 哪怕"不确定会不会回归"也要写，空列表意味着"我确定没有风险"——这几乎从不是真的
4. **回滚后不要立即重试** — 先问"为什么预测错了"，再考虑新的修改策略
