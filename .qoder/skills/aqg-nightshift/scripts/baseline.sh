#!/usr/bin/env bash
# AQG 夜跑零污染护栏：对「评测事实 + 数据集 + 配置」做内容指纹快照与复验。
# 只读脚本：不写仓库任何文件，快照落在 /tmp。
#   用法： baseline.sh snap    开工前打快照
#         baseline.sh check   每项收尾复验；有变化则退出码 1 并打印差异
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
BASE=/tmp/aqg_nightshift_baseline.txt

scan() {
  # 1) 评测事实与数据集：逐文件内容指纹（顺序稳定）
  find reports cases -type f -print0 2>/dev/null | sort -z | while IFS= read -r -d '' f; do
    shasum -a 256 "$f"
  done
  # 2) 运行配置（models*.json 不入库，但改动会影响后续批次，必须一并钉住）
  for f in models.json models.attack_probe.json models.example.json .env; do
    [ -f "$f" ] && shasum -a 256 "$f" || true
  done
  # 3) git 事实：夜跑期间 HEAD 与分支不得移动，且不允许出现暂存内容
  printf 'HEAD %s\n' "$(git rev-parse HEAD)"
  printf 'BRANCH %s\n' "$(git branch --show-current)"
  printf 'STAGED %s\n' "$(git diff --cached --name-only | tr '\n' ',')"
}

case "${1:-}" in
  snap)
    scan > "$BASE"
    echo "snapshot: $(grep -c . "$BASE") 条指纹 -> $BASE"
    echo "HEAD: $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-40))"
    ;;
  check)
    [ -f "$BASE" ] || { echo "没有快照，先跑：baseline.sh snap"; exit 2; }
    scan > "$BASE.now"
    if diff -q "$BASE" "$BASE.now" >/dev/null; then
      echo "✅ 零污染：评测事实／数据集／配置／HEAD／暂存区 全部与快照一致"
    else
      echo "🛑 出现变化（左=快照 右=现在）："
      diff "$BASE" "$BASE.now" || true
      exit 1
    fi
    ;;
  *)
    echo "usage: baseline.sh snap|check"; exit 64
    ;;
esac
