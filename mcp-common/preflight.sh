#!/usr/bin/env bash
# mcp-suite 部署前门禁 —— 每次 push 后、 rebuild 前跑一遍。
#
#   bash mcp-common/preflight.sh
#
# 做四件事：
#   1. 副本漂移检查    mcp-common/sync.py --check   （canonical 与各服务副本是否一致）
#   2. 语法/静态检查    py_compile + pyflakes        （未定义名、语法错误）
#   3. 单测            各仓已就绪的 pytest 套件
#   4. 公共库单测      mcp-common 的 94 个用例
#
# 任一步失败 → 退出码非 0，不要部署。

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

SERVICES=(astock-data-mcp global-data-mcp causal-mcp kronos-mcp factor-miner-mcp)
FAIL=0
step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1"; FAIL=1; }
pass() { printf '  \033[32m✓ %s\033[0m\n' "$1"; }

# 本地是兄弟目录，服务器是子目录 —— 与 sync.py 的解析规则保持一致
locate() {
    local s="$1"
    if [ -f "$ROOT/$s/Dockerfile" ]; then echo "$ROOT/$s"
    elif [ -f "$ROOT/../$s/Dockerfile" ]; then echo "$(cd "$ROOT/.." && pwd)/$s"
    else echo ""; fi
}

step "1/5 公共库副本漂移检查"
if out=$(python3 "$HERE/sync.py" --check 2>&1); then
    pass "$(echo "$out" | tail -1)"
else
    echo "$out" | grep '✗' | head -8
    fail "副本与 canonical 不一致 —— 先跑 python3 mcp-common/sync.py 并提交各服务仓库"
fi

step "2/5 语法与静态检查（未定义名 / 语法错误）"
python3 -c "import pyflakes" 2>/dev/null || {
    echo "  (pyflakes 未安装，只做 py_compile)"; }
# 只拦真问题。pyflakes 的 "assigned to but never used"、"imported but unused"、
# "f-string is missing placeholders" 都是良性提示，把它们当失败会让门禁被人无视。
SERIOUS='undefined name|syntax error|redefinition of|unable to detect undefined'
for s in "${SERVICES[@]}"; do
    d=$(locate "$s"); [ -z "$d" ] && { echo "  - $s 未找到，跳过"; continue; }
    bad=""
    while IFS= read -r f; do
        [ -f "$d/$f" ] || continue
        if ! err=$(python3 -m py_compile "$d/$f" 2>&1); then
            bad="$bad
$s/$f: $err"; continue
        fi
        if python3 -c "import pyflakes" 2>/dev/null; then
            hit=$(python3 -m pyflakes "$d/$f" 2>/dev/null | grep -E "$SERIOUS" \
                  | grep -v "may be undefined, or defined from star imports" \
                  | grep -v "unable to detect undefined names")
            [ -n "$hit" ] && bad="$bad
$hit"
        fi
    done < <(cd "$d" && git ls-files '*.py' 2>/dev/null)
    if [ -n "$bad" ]; then echo "$bad" | head -8; fail "$s 有静态问题"; else pass "$s"; fi
done

step "3/5 各服务单测"
for s in "${SERVICES[@]}"; do
    d=$(locate "$s"); [ -z "$d" ] && continue
    tests=$(cd "$d" && ls test_*.py factor_miner/test_*.py 2>/dev/null)
    if [ -z "$tests" ]; then echo "  - $s 无测试套件"; continue; fi
    if out=$(cd "$d" && python3 -m pytest $tests -q --no-header 2>&1 | tail -1); then
        pass "$s  $out"
    else
        echo "    $out"; fail "$s 测试失败"
    fi
done

step "4/5 管控层单测（quota-platform）"
if [ -f "$ROOT/quota-platform/tests/test_quota.py" ]; then
    if out=$(cd "$ROOT/quota-platform" && python3 -m pytest tests/test_quota.py -q --no-header 2>&1 | tail -1); then
        pass "$out"
    else
        echo "    $out"; fail "quota-platform 测试失败"
    fi
else
    echo "  - quota-platform 测试文件不存在"
fi

step "5/5 公共库单测"
if out=$(python3 -m pytest "$HERE/test_mcp_common.py" -q --no-header 2>&1 | tail -1); then
    pass "$out"
else
    echo "    $out"; fail "mcp-common 测试失败"
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    printf '\033[32m✓ 全部门禁通过 —— 可以部署\033[0m\n'
    printf '  下一步：服务器 git pull → docker compose up -d --build <svc> → docker restart mcphub\n'
else
    printf '\033[31m✗ 门禁未通过 —— 不要部署\033[0m\n'
fi
exit "$FAIL"
