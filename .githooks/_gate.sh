#!/bin/sh
# ─────────────────────────────────────────────────────────────────────
# 门禁公共逻辑
#
#   $1 = commit  普通提交（dev 上）：拒绝在 main 上提交 + 跑快速回归
#   $1 = merge   合并提交（进 main）：允许在 main 上 + 跑完整回归
#
# 为什么把模式做成显式参数，而不是靠 MERGE_HEAD 判断：
#   实测在 pre-merge-commit 执行的那一刻 .git/MERGE_HEAD 尚未写入，
#   靠它判断会把合并误判成普通提交，结果是分支守卫先拦下来 ——
#   合并虽然失败了，但失败理由是错的，测试门禁根本没跑。
#   显式传参不依赖任何 Git 内部时序。
#
# 绕过方式：git commit --no-verify / git merge --no-verify
# ─────────────────────────────────────────────────────────────────────
set -u

MODE="${1:-commit}"
MAIN_BRANCH="main"

branch=$(git symbolic-ref --short -q HEAD 2>/dev/null || echo "")

# ── 1. 分支守卫：不允许在 main 上直接提交 ────────────────────────────
if [ "$MODE" = "commit" ] && [ "$branch" = "$MAIN_BRANCH" ]; then
    cat >&2 <<'EOF'

  ✗ 拒绝直接提交到 main 分支。

    main 只接受合并进入。请这样操作：

        git switch dev
        git add -A && git commit
        # 测试通过后：
        git switch main
        git merge --no-ff dev -m "merge: ..."

    确实要绕过：git commit --no-verify

EOF
    exit 1
fi

# ── 2. 选择 Python 解释器 ────────────────────────────────────────────
PY=""
if [ -n "${OZON_PYTHON:-}" ]; then
    PY="$OZON_PYTHON"
elif command -v python >/dev/null 2>&1; then
    PY="python"
elif [ -x "/c/Users/26060/anaconda3/python.exe" ]; then
    PY="/c/Users/26060/anaconda3/python.exe"
fi

if [ -z "$PY" ]; then
    cat >&2 <<'EOF'

  ✗ 门禁无法执行：找不到 python 解释器。

    请安装 Python，或显式指定：
        export OZON_PYTHON=/path/to/python
    确实要绕过：git commit --no-verify

EOF
    exit 1
fi

PYTHONIOENCODING=utf-8
export PYTHONIOENCODING

# ── 3. 测试门禁 ──────────────────────────────────────────────────────
if [ "$MODE" = "push" ]; then
    echo "[gate] 推送到远端 —— 完整回归（夹具 + 生产库全量对账）"
    echo "[gate] 推送是不可逆动作，一律跑全套，不做快速档"
    echo "[gate] $PY -m unittest core.tests.test_profit_golden"
    echo
    "$PY" -m unittest core.tests.test_profit_golden
    rc=$?
elif [ "$MODE" = "merge" ]; then
    echo "[gate] 合并进 $MAIN_BRANCH —— 完整回归（夹具 + 生产库全量对账）"
    echo "[gate] $PY -m unittest core.tests.test_profit_golden"
    echo
    "$PY" -m unittest core.tests.test_profit_golden
    rc=$?
else
    echo "[gate] $branch 分支提交 —— 快速回归（夹具）"
    echo "[gate] $PY -m unittest core.tests.test_profit_golden.GoldenFixtureTest"
    echo
    "$PY" -m unittest core.tests.test_profit_golden.GoldenFixtureTest
    rc=$?
fi

if [ "$rc" -ne 0 ]; then
    cat >&2 <<EOF

  ✗ 测试未通过（退出码 $rc），操作被拒绝。

    请先修好再提交。确实要提交未完成的工作：
        git commit --no-verify
    合并被拒时请先清理现场：
        git merge --abort

EOF
    exit "$rc"
fi

echo
echo "[gate] ✓ 测试通过"
exit 0
