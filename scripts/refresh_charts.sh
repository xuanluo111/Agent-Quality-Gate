#!/usr/bin/env bash
# 刷新入库图表：把"非本次批次"的 run 归档移走，只用保留前缀匹配的真实 run 重新生成
# reports/charts，最后打印待提交 diff 供人工 review。
#
# 底线：
#   - 只 mv 归档、绝不 rm 真实数据（归档目录 .reports-archive-* 已在 .gitignore 内）。
#   - 没有任何 run 匹配保留前缀时直接报错退出，拒绝把全部 run 清光。
#   - 不自动 commit / push，只给出建议命令。
#
# 用法:
#   scripts/refresh_charts.sh <保留run前缀> [--dry-run]
# 例:
#   scripts/refresh_charts.sh 20261029            # 只保留今晚全量批, 其余归档
#   scripts/refresh_charts.sh 20261029 --dry-run  # 预览将归档哪些, 不动手
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PY:-.venv/bin/python}"

usage() { echo "用法: $0 <保留run前缀> [--dry-run]  (例: $0 20261029)" >&2; exit 2; }

[ $# -ge 1 ] || usage
KEEP="$1"; shift
DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $a" >&2; usage ;;
  esac
done

RUNS="$ROOT/reports/runs"
[ -d "$RUNS" ] || { echo "reports/runs 不存在: $RUNS" >&2; exit 1; }
if [ ! -x "$PY" ] && [ ! -f "$PY" ]; then
  echo "找不到解释器 $PY（可用 PY=python3 $0 ... 覆盖）" >&2; exit 1
fi

# 收集：保留喂图的 run / 将归档移走的 run
keep_list=()
archive_list=()
while IFS= read -r d; do
  name="$(basename "$d")"
  if [[ "$name" == "$KEEP"* ]]; then
    keep_list+=("$name")
  else
    archive_list+=("$name")
  fi
done < <(find "$RUNS" -mindepth 1 -maxdepth 1 -type d | sort)

echo "== 保留喂图的 run (${#keep_list[@]}) =="
if [ ${#keep_list[@]} -gt 0 ]; then printf '   %s\n' "${keep_list[@]}"; else echo "   (无)"; fi
echo "== 将归档移走的 run (${#archive_list[@]}) =="
if [ ${#archive_list[@]} -gt 0 ]; then printf '   %s\n' "${archive_list[@]}"; else echo "   (无)"; fi

# 护栏：匹配为空 = 前缀写错或批次没产出，拒绝归档全部 run
if [ ${#keep_list[@]} -eq 0 ]; then
  echo "错误: 没有 run 匹配前缀 '$KEEP'，拒绝归档全部 run。检查前缀或批次是否已跑完。" >&2
  exit 1
fi

# 提醒：保留的 run 若缺 summary.json 则不会进图（多半没跑完/被判 invalid）
for n in "${keep_list[@]}"; do
  if [ ! -f "$RUNS/$n/summary.json" ]; then
    echo "警告: $n 缺 summary.json，可能未跑完，将不进图。" >&2
  fi
done

if [ "$DRY" -eq 1 ]; then
  echo "[--dry-run] 未做任何改动。去掉 --dry-run 实际执行。"
  exit 0
fi

# 归档：移出 reports/runs，保留在 .reports-archive-<ts>/（数据不丢）
if [ ${#archive_list[@]} -gt 0 ]; then
  ARC="$ROOT/.reports-archive-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$ARC"
  echo "== 归档到 $ARC =="
  for n in "${archive_list[@]}"; do
    mv "$RUNS/$n" "$ARC/"
  done
fi

echo "== 重新生成图表 =="
"$PY" scripts/visualize_reports.py

echo
echo "== 待提交的 tracked 改动（charts + ledger）=="
git status --short -- reports/charts reports/risk_ledger.json
echo
echo "肉眼核对 reports/charts/dashboard.html（7 个模型是否都在、有无 invalid）后，手动提交："
echo "  git add reports/charts reports/risk_ledger.json && git commit -m 'chore(reports): ${KEEP} 真实结果重生图表 + 换代台账'"
echo "（本脚本不会自动 commit / push；旧 run 已安全存于归档目录，未删除）"
