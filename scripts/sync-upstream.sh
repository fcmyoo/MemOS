#!/usr/bin/env bash
# ============================================================
# MemOS 私有部署分支 — 上游同步脚本
# ============================================================
# 用途：把上游 MemTensor/MemOS 的最新改动同步到本地的私有部署分支，
#       同时保持 main 纯净（永远只跟踪上游，不含私有鉴权改动）。
#
# 工作流：
#   main（纯上游）─────────────► 私有分支（含鉴权加固）
#        │                              ▲
#        └──── git pull origin main ────┘
#
# 用法：
#   bash scripts/sync-upstream.sh            # 同步 + 合并到当前私有分支
#   bash scripts/sync-upstream.sh --rebase   # 用 rebase 保持线性历史
#
# 注意：
#   - 只在 私有分支（feat/api-auth-hardening）上运行，不要在 main 上运行
#   - 合并冲突时按常规解决：git status 看冲突文件，解决后 git add + git commit
# ============================================================

set -euo pipefail

PRIVATE_BRANCH="${1:-feat/api-auth-hardening}"
MODE="${2:-merge}"   # merge | rebase

cd "$(dirname "$0")/.."

echo "==> 当前分支: $(git branch --show-current)"
if [ "$(git branch --show-current)" = "main" ]; then
  echo "❌ 不要在 main 上运行同步！请先切到私有分支: git checkout ${PRIVATE_BRANCH}"
  exit 1
fi

echo "==> [1/3] 切到 main 并拉取上游最新"
git checkout main
git pull origin main

echo "==> [2/3] 切回私有分支 ${PRIVATE_BRANCH}"
git checkout "${PRIVATE_BRANCH}"

echo "==> [3/3] 把上游 main 合并进来"
if [ "${MODE}" = "rebase" ]; then
  git rebase main
else
  git merge main -m "chore: merge upstream main ($(git rev-parse --short main))"
fi

echo "==> 完成。当前 HEAD:"
git log --oneline -3

echo ""
echo "下一步："
echo "  1. 如有冲突：git status 查看并解决，然后 git add + git commit（或 git rebase --continue）"
echo "  2. 跑测试确认无回归: pytest tests/api/ -q"
echo "  3. 部署验证后推送: git push fork ${PRIVATE_BRANCH}"
