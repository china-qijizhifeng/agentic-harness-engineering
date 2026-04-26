#!/usr/bin/env bash
set -euo pipefail
: "${LLM_API_KEY:?set LLM_API_KEY}"
: "${LLM_BASE_URL:?set LLM_BASE_URL}"
: "${LLM_MODEL:?set LLM_MODEL}"
: "${GOLDEN_TRACE:?set GOLDEN_TRACE to a cleaned.json path}"
: "${TRACE_TYPE:=openai_messages}"

cd "$HOME/agentic-harness-engineering"

TMP=$(mktemp -d)
OLD_PREFIX="$TMP/old"
NEW_PREFIX="$TMP/new"
export OLD_PREFIX NEW_PREFIX

for kind in ask check; do
  echo "[golden-diff] running OLD $kind ..."
  if [ "$kind" = "ask" ]; then
    /tmp/adb_old_venv/bin/adb "$kind" -t "$GOLDEN_TRACE" --trace-type "$TRACE_TYPE" -q "Summarize what the agent did in this trace." --format json > "${OLD_PREFIX}.$kind.json"
  else
    /tmp/adb_old_venv/bin/adb "$kind" -t "$GOLDEN_TRACE" --trace-type "$TRACE_TYPE" --format json > "${OLD_PREFIX}.$kind.json"
  fi
  echo "[golden-diff] running NEW $kind ..."
  if [ "$kind" = "ask" ]; then
    /tmp/adb_new_venv/bin/adb "$kind" -t "$GOLDEN_TRACE" --trace-type "$TRACE_TYPE" -q "Summarize what the agent did in this trace." --format json > "${NEW_PREFIX}.$kind.json"
  else
    /tmp/adb_new_venv/bin/adb "$kind" -t "$GOLDEN_TRACE" --trace-type "$TRACE_TYPE" --format json > "${NEW_PREFIX}.$kind.json"
  fi
done

python3 - <<'PY'
import json, os, sys
OLD = os.environ["OLD_PREFIX"]; NEW = os.environ["NEW_PREFIX"]
STABLE = ["status", "command", "trace_path", "trace_id", "request_id"]
def check(kind):
    old = json.load(open(f"{OLD}.{kind}.json"))
    new = json.load(open(f"{NEW}.{kind}.json"))
    for k in STABLE:
        assert old.get(k) == new.get(k), f"{kind}: {k} mismatch old={old.get(k)!r} new={new.get(k)!r}"
    if kind == "check":
        assert isinstance(new.get("issues_count"), int), "check.issues_count must be int"
        for issue in new.get("issues") or []:
            for k in ("issue_type", "summary", "evidence", "message_index"):
                assert k in issue, f"issue missing {k}"
            assert issue["issue_type"] in ("工具错误", "幻觉", "循环", "不合规", "截断"), f"bad issue_type {issue['issue_type']}"
    print(f"{kind}: OK")
check("ask"); check("check")

# Also echo TMP dir so caller can copy
print(f"TMP_DIR={os.path.dirname(OLD)}")
PY

echo "[golden-diff] artifacts at: $TMP"
# Copy artifacts to a stable location for reviewer inspection
mkdir -p /tmp/golden_diff_artifacts
cp "${OLD_PREFIX}.ask.json" /tmp/golden_diff_artifacts/old.ask.json
cp "${OLD_PREFIX}.check.json" /tmp/golden_diff_artifacts/old.check.json
cp "${NEW_PREFIX}.ask.json" /tmp/golden_diff_artifacts/new.ask.json
cp "${NEW_PREFIX}.check.json" /tmp/golden_diff_artifacts/new.check.json
echo "[golden-diff] copied artifacts to /tmp/golden_diff_artifacts/"
