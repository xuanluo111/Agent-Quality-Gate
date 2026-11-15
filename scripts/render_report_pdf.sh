#!/usr/bin/env bash
#
# 把提交用 Markdown 转成 PDF（A4）。
#
# 为什么不用 LaTeX：本机没有 TeX 引擎（xelatex / pdflatex / lualatex / tectonic /
# typst 均未安装），pandoc 只能产出 HTML；wkhtmltopdf 与 weasyprint 也没有。
# 唯一可用的排版引擎是 Chrome，所以走：
#
#     pandoc(gfm -> html5) -> Chrome headless --print-to-pdf
#
# 中文字体用 macOS 自带的 PingFang SC，不需要额外安装字体。
#
# 用法：
#   scripts/render_report_pdf.sh deliverables/qwen-2610/01-评测报告.md
#   scripts/render_report_pdf.sh deliverables/qwen-2610/*.md
#
# 退出码：
#   0  全部转换成功，且正文没有遗留「待本人填写」占位
#   1  参数、依赖或转换本身失败
#   2  转换成功，但正文仍有「待本人填写」占位（定稿前必现，不算错误但必须看见）

set -euo pipefail

CHROME="${CHROME_BIN:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSS="${HERE}/assets/report.css"
PLACEHOLDER="【待本人填写】"

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <file.md> [file.md ...]" >&2
  exit 1
fi
[[ -x "$CHROME" ]] || { echo "找不到 Chrome: $CHROME" >&2; exit 1; }
[[ -f "$CSS" ]] || { echo "找不到样式: $CSS" >&2; exit 1; }

# 临时文件放项目内而不是 /tmp：Chrome 要读 file:// 的 HTML、写 PDF，
# 同盘同目录能避开权限与跨桌面的偶发失败。结束时整个目录删掉。
WORK="$(cd "$HERE/.." && pwd)/.tmp-pdf"
rm -rf "$WORK" && mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

pending_total=0

for md in "$@"; do
  [[ -f "$md" ]] || { echo "找不到文件: $md" >&2; exit 1; }

  out="${md%.md}.pdf"
  tmp_html="$WORK/$(basename "${md%.md}").html"
  tmp_profile="$WORK/profile"

  # --metadata pagetitle 只写进 <title> 标签，不会在正文里再生成一个 h1
  # （我们的 md 自带一级标题，用 --metadata title 会变成两个标题）。
  pandoc "$md" \
    --from gfm \
    --to html5 \
    --standalone \
    --metadata pagetitle="$(basename "${md%.md}")" \
    --css "$CSS" \
    --output "$tmp_html"

  # 占位符加黄色底，肉眼一眼能看出没填。定稿后占位清零，高亮自然消失。
  python3 - "$tmp_html" "$PLACEHOLDER" <<'PY'
import sys

path, holder = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    html = fh.read()
html = html.replace(holder, f'<mark class="todo">{holder}</mark>')
with open(path, "w", encoding="utf-8") as fh:
    fh.write(html)
PY

  # --disable-dev-shm-usage / --user-data-dir 两项都是必需的：
  # 前者不加，Chrome 在这台机器上启动即 SIGTRAP 崩溃（实测 Trace/BPT trap: 5）。
  # 后者不给独立目录，本机 Chrome 已在运行时 headless 会静默不产出文件。
  #
  # 不要加 --virtual-time-budget：实测它在 headless=new 下同样引发 SIGTRAP，
  # 而我们的字体与样式全在本机，没有需要等待的网络资源。
  # stderr 收到文件，失败时再回显——Chrome 会往 stderr 刷大量无害的
  # CVDisplayLink 报错，直接打到屏幕上会把真正的问题冲掉。
  chrome_log="$WORK/chrome.log"
  "$CHROME" \
    --headless=new \
    --disable-gpu \
    --no-sandbox \
    --disable-dev-shm-usage \
    --no-pdf-header-footer \
    --user-data-dir="$tmp_profile" \
    --print-to-pdf="$out" \
    "file://${tmp_html}" >"$chrome_log" 2>&1 || true

  [[ -s "$out" ]] || {
    echo "转换失败，未生成: $out" >&2
    tail -15 "$chrome_log" >&2
    exit 1
  }
  # PDF 魔数校验：防止 Chrome 把错误页（HTML）存成 .pdf
  head -c 5 "$out" | grep -q '%PDF-' || {
    echo "产出不是 PDF: $out" >&2
    exit 1
  }

  pages="$(python3 - "$out" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
# 数 /Type /Page（不含 /Pages）估算页数，够用于打印进度，不追求精确
print(len(re.findall(rb"/Type\s*/Page[^s]", raw)) or "?")
PY
)"

  n_pending="$(grep -c "$PLACEHOLDER" "$md" || true)"
  pending_total=$((pending_total + n_pending))

  printf '%-46s -> %-46s %s 页' "$(basename "$md")" "$(basename "$out")" "$pages"
  if [[ "$n_pending" -gt 0 ]]; then
    printf '，待填 %s 处\n' "$n_pending"
  else
    printf '，占位已清零\n'
  fi
done

if [[ "$pending_total" -gt 0 ]]; then
  echo
  echo "注意：全文仍有 ${pending_total} 处「${PLACEHOLDER}」，PDF 中以黄色高亮标出。"
  echo "这一版只能内部预览，不能作为提交件。"
  exit 2
fi
