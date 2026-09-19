#!/bin/sh
# ─────────────────────────────────────────────────────────────────────
# 门禁公共逻辑
#
#   $1 = commit  普通提交（dev 上）：拒绝在 main 上提交 + 跑快速回归
#   $1 = merge   合并提交（进 main）：允许在 main 上 + 跑完整回归
#   $1 = push    推送：完整回归（pre-push 传入）
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
gitdir=$(git rev-parse --git-dir 2>/dev/null || echo ".git")

# ── 0. 合并进行中 → 按合并模式处理 ───────────────────────────────────
# 为什么需要这段（实测踩到过）：
#   完成合并有两条路径 —— `git merge --continue` 与直接 `git commit`。
#   前者调用 pre-merge-commit（模式已是 merge）；
#   后者调用的是 **pre-commit**（模式为 commit），于是分支守卫会以
#   「拒绝在 main 上提交」为由把合并拦下，且理由看起来与合并无关，很难查。
#   这里的 MERGE_HEAD 判断是可靠的：pre-commit 被调用时它**已经写入**
#   （而 pre-merge-commit 阶段它尚未写入，所以那种场景不能用 —— 见 pre-merge-commit 的注释）。
if [ "$MODE" = "commit" ] && [ -f "$gitdir/MERGE_HEAD" ]; then
    MODE="merge"
fi

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
# 两套测试都要过：
#   core —— 财务口径回归（夹具；合并/推送时跑生产库全量对账）
#   api  —— 只读看板接口（含 401/403 越权与口径一致性）
#
# 2026-09-19：core 的目标从「只跑 test_profit_golden」改成「整个 core/tests」。
# 起因是新增的 test_sqlite_source_cutoff.py（窗口右端回归，复现过一个真实
# 事故：days=7 返回 0 单）**不在门禁范围内** —— 一个没人跑的回归测试等于没有。
# 现在合并/推送跑 `discover -s core/tests`（口径 + Excel 导入 + 窗口边界），
# 提交仍只跑夹具快跑档（约 2 秒），但会额外跑一遍窗口边界（约 0.1 秒）。
if [ "$MODE" = "push" ]; then
    echo "[gate] 推送到远端 —— 完整回归（core 全量 + API）"
    CORE_ARGS="discover -s core/tests -t ."
elif [ "$MODE" = "merge" ]; then
    echo "[gate] 合并进 $MAIN_BRANCH —— 完整回归（core 全量 + API）"
    CORE_ARGS="discover -s core/tests -t ."
else
    echo "[gate] $branch 分支提交 —— 快速回归（core 夹具 + 窗口边界 + API）"
    CORE_ARGS="core.tests.test_profit_golden.GoldenFixtureTest core.tests.test_sqlite_source_cutoff"
fi

rc=0

echo "[gate] $PY -m unittest $CORE_ARGS"
"$PY" -m unittest $CORE_ARGS
rc=$?

echo "[gate] $PY -m unittest discover -s api/tests -t ."
"$PY" -m unittest discover -s api/tests -t .
if [ $? -ne 0 ]; then
    [ "$rc" -eq 0 ] && rc=1
fi

if [ "$rc" -ne 0 ]; then
    cat >&2 <<EOF

  ✗ 测试未通过（退出码 $rc），操作被拒绝。

    请先修好再提交。确实要提交未完成的工作：
        git commit --no-verify
    合并被拒时请先清理现场：
        git merge --abort
    API 测试失败时可直接单跑：
        $PY -m unittest discover -s api/tests -t .

EOF
    exit "$rc"
fi

echo
echo "[gate] ✓ 测试通过"
exit 0
