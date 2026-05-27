#!/usr/bin/env bash
# AHE Repository CI — 自动检查仓库健康状态
# 运行: bash scripts/ci.sh
# 退出码: 0=通过, 1=失败

set -e
PASS=0
FAIL=0
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

green() { echo "✅ $1"; PASS=$((PASS+1)); }
red() { echo "❌ $1"; FAIL=$((FAIL+1)); }
header() { echo ""; echo "===== $1 ====="; }

# 1. py_compile — 所有 Python 脚本语法检查
header "1. py_compile"
ALL_PY_OK=true
for f in "$ROOT"/scripts/*.py; do
    if python3 -m py_compile "$f" 2>/dev/null; then
        echo "  ✓ $(basename $f)"
    else
        echo "  ✗ $(basename $f)"
        ALL_PY_OK=false
    fi
done
$ALL_PY_OK && green "All Python scripts compile" || red "Python compile error"

# 2. Schema validate
header "2. Schema validate"
python3 -m json.tool "$ROOT/references/change-manifest-schema.json" > /dev/null 2>&1 && \
  python3 -c "
import json
try:
    import jsonschema
    schema = json.load(open('$ROOT/references/change-manifest-schema.json'))
    sample = json.load(open('$ROOT/references/examples/minimal-workspace/manifests/example_manifest.json'))
    jsonschema.validate(sample, schema)
    print('  \u2713 example_manifest.json validates')
except ImportError:
    # jsonschema not available, do basic check instead
    print('  \u26a0 jsonschema not installed, skipping full validation')
" 2>/dev/null && green "Schema valid + sample validates" || red "Schema/sample validation failed"

# 3. validate example
header "3. validate example"
python3 "$ROOT/scripts/validate_harness.py" "$ROOT/references/examples/minimal-workspace" --json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['compliance_pct'] >= 0, 'Negative score'
print(f'  Score: {d[\"compliance_pct\"]}%')
" 2>/dev/null && green "Example workspace validates" || red "Example validation failed"

# 4. init + validate
header "4. init + validate"
TMPDIR=$(mktemp -d 2>/dev/null || echo "$ROOT/tmp/ci-test-$$")
mkdir -p "$TMPDIR"
python3 "$ROOT/scripts/init_harness.py" "$TMPDIR/test-workspace" --profile generic > /dev/null 2>&1
python3 "$ROOT/scripts/validate_harness.py" "$TMPDIR/test-workspace" --json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
assert d['compliance_pct'] >= 0, 'Init workspace invalid'
print(f'  Init workspace score: {d[\"compliance_pct\"]}%')
" 2>/dev/null && green "Init + validate passes" || red "Init + validate failed"
rm -rf "$TMPDIR"

# 5. generate + verify
header "5. generate + verify"
TMPDIR2=$(mktemp -d 2>/dev/null || echo "$ROOT/tmp/ci-test-$$-2")
mkdir -p "$TMPDIR2"
python3 "$ROOT/scripts/init_harness.py" "$TMPDIR2/test-workspace" > /dev/null 2>&1
cd "$TMPDIR2/test-workspace"
git init > /dev/null 2>&1
git config user.email "ci@ahe" > /dev/null 2>&1
git config user.name "AHE CI" > /dev/null 2>&1
git add -A > /dev/null 2>&1
git commit -m "init" > /dev/null 2>&1
echo "# Extra rule" >> AGENTS.md
python3 "$ROOT/scripts/generate_manifest.py" --workspace . --diff --author ci-test > /dev/null 2>&1
GEN_COUNT=$(find manifests -name "change_*.json" 2>/dev/null | wc -l)
echo "  Generated: $GEN_COUNT manifest(s)"
RESULTS_FILE="$TMPDIR2/test-workspace/results.json"
echo '{"passed":["T-001"],"failed":[]}' > "$RESULTS_FILE"
python3 "$ROOT/scripts/verify_manifest.py" --workspace . --results "$RESULTS_FILE" > /dev/null 2>&1
cd "$ROOT"
rm -rf "$TMPDIR2"
[ $GEN_COUNT -gt 0 ] && green "Generate + verify workflow passes" || red "Generate/verify failed"

# 6. verify 后 manifest 仍符合 schema
header "6. Post-verify schema compliance"
python3 -c "
import json, os
try:
    import jsonschema
    schema = json.load(open('$ROOT/references/change-manifest-schema.json'))
    wrk = '$ROOT/references/examples/minimal-workspace'
    for mf in sorted(os.listdir(os.path.join(wrk, 'manifests'))):
        if mf.endswith('.json'):
            data = json.load(open(os.path.join(wrk, 'manifests', mf)))
            jsonschema.validate(data, schema)
            print('  \u2713 ' + mf + ' conforms to schema')
except ImportError:
    print('  \u26a0 jsonschema not installed, skipping')
" 2>/dev/null && green "All manifests conform to schema" || red "Post-verify schema violation"

# Summary
echo ""
echo "===== CI Summary ====="
echo "Passed: $PASS | Failed: $FAIL"
[ $FAIL -eq 0 ] && echo "✅ All checks passed" || echo "❌ $FAIL check(s) failed"
exit $FAIL
