#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
export_pdf.py — 将 output/ 下的词汇手册 Markdown 批量转换为 A4 PDF（黑白打印友好）。

用法:
    python export_pdf.py                       # 转换 output/ 下全部 md
    python export_pdf.py vocabulary-print-01.md   # 只转换指定文件（相对 output/ 的路径）

原理: 自带的极简 MD→HTML 渲染（仅处理本仓库词汇手册的固定格式：
H1/H2 标题、表格、---、<br>）+ Edge 无头模式打印为 A4 PDF。
每个 "## Page N" 分页标记强制换页（每块 25 词，见 convert_vocab.py）；
行高按每块内容自适应（6.5~10.5mm），25 行正好填满一页 A4，留出手写抄写空间。

输出: output/pdf/*.pdf
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"
PDF_DIR = OUT_DIR / "pdf"

EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]

CSS = """
@page { size: A4 portrait; margin: 10mm 9mm 10mm 9mm; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
    font-family: "Microsoft YaHei", "PingFang SC", "SimSun", sans-serif;
    font-size: 10pt; color: #000; background: #fff;
}
h1 { font-size: 15pt; text-align: center; margin: 0 0 4mm; }
h1:not(:first-child) { break-before: page; }        /* 合并版各 Word List 分节另起一页 */
h2.page { font-size: 12pt; margin: 0 0 3mm; break-before: page; }  /* Page N 每块一页 */
hr.marker { display: none; }                         /* --- 仅作源码分隔，PDF 中以换页体现 */
table { border-collapse: collapse; width: 100%; table-layout: fixed; }
thead { display: table-header-group; }               /* 表格跨页时重复表头 */
th, td { border: 0.5pt solid #000; padding: 1pt 3pt; line-height: 1.3; }
th { font-weight: 700; }
td:nth-child(1), td:nth-child(2), td:nth-child(3) {
    text-align: center; vertical-align: middle;
}
td:nth-child(5), td:nth-child(6) { text-align: center; letter-spacing: 1px; }
td:nth-child(8) { text-align: center; }
"""

COLGROUP = (
    '<colgroup>'
    '<col style="width:3.5%"><col style="width:3.5%"><col style="width:3.5%">'
    '<col style="width:15%"><col style="width:10%"><col style="width:10%">'
    '<col style="width:42.5%"><col style="width:12%">'
    '</colgroup>'
)

_HEADER_RE = re.compile(r"^\|\s*第1遍\s*\|")
_SEP_RE = re.compile(r"^\|[\s\-|]*\|$")

# 自适应行高参数（10pt / 行距 1.3 / padding 1pt / 边框 0.5pt）
# 数值经实测校准：微软雅黑实际行高约为 4.9mm/行，预算留 ~5mm 安全边距
PAGE_BUDGET_MM = 248.0     # 一页可用高度（A4 277mm 减去标题、表头、渲染误差）
ROW_BASE_MM = 1.5          # 每行 padding + 边框的固定高度
LINE_MM = 4.9              # 每行文字的实际高度（10pt × 1.333 × 1.3 ≈ 4.6mm，实测偏大）
ROW_MIN_MM = 6.5           # 行高下限（内容过多的块配合缩小字号）
ROW_MAX_MM = 10.5          # 行高上限（留白舒适，避免小块页行高过大）


def meaning_lines(meaning):
    """估算释义在释义列内占几行（列宽 42.5% ≈ 22 个中文字，留 1 字余量）。"""
    parts = meaning.split("<br>")
    return len(parts) + sum(max(0, (len(p) + 21) // 22 - 1) for p in parts)


def fit_chunk(chunk_rows):
    """为一块词条求 (统一行高mm, 字号缩放)。

    - 内容能放下：行高取能填满一页的最大值（6.5~10.5mm），字号不变
    - 内容过多：字号按需缩小（最低 ~5pt），行高取下限 6.5mm
    """
    lines = [meaning_lines(r[0]) for r in chunk_rows]
    n = len(lines)
    if not n:
        return ROW_MAX_MM, 1.0

    # 正常情况：迭代求填满页面的统一行高
    min_h = PAGE_BUDGET_MM / n
    for _ in range(4):
        over = sum(max(0.0, ROW_BASE_MM + LINE_MM * l - min_h) for l in lines)
        min_h = (PAGE_BUDGET_MM - over) / n
    if min_h >= ROW_MIN_MM:
        return max(ROW_MIN_MM, min(ROW_MAX_MM, round(min_h, 1))), 1.0

    # 内容过多：二分求字号缩放，使块恰好装入页面
    lo, hi = 0.5, 1.0
    if sum(max(ROW_MIN_MM, ROW_BASE_MM + LINE_MM * lo * l) for l in lines) > PAGE_BUDGET_MM:
        return ROW_MIN_MM, round(lo, 2)  # 极端块：接受溢出
    for _ in range(24):
        mid = (lo + hi) / 2
        total = sum(max(ROW_MIN_MM, ROW_BASE_MM + LINE_MM * mid * l) for l in lines)
        if total <= PAGE_BUDGET_MM:
            lo = mid
        else:
            hi = mid
    return ROW_MIN_MM, round(lo, 2)


def md_to_html(text):
    """仅针对本仓库词汇手册固定格式的极简 MD→HTML。
    每个分页块（table）单独计算自适应行高（必要时缩小字号），
    使每块 25 行恰好填满一页。"""
    out, in_table, header, chunk = [], False, None, []
    for raw in text.split("\n"):
        s = raw.rstrip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if _SEP_RE.match(s):
                continue
            if _HEADER_RE.match(s):
                in_table = True
                header = cells
                chunk = []
            else:
                chunk.append((cells[6] if len(cells) > 6 else "", cells))
            continue
        if in_table:
            out.extend(_render_table(header, chunk))
            in_table = False
        if s == "---":
            out.append('<hr class="marker">')
        elif s.startswith("## "):
            out.append(f'<h2 class="page">{s[3:]}</h2>')
        elif s.startswith("# "):
            out.append(f"<h1>{s[2:]}</h1>")
    if in_table:
        out.extend(_render_table(header, chunk))
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{CSS}</style></head><body>"
        + "\n".join(out)
        + "</body></html>"
    )


def _render_table(header, chunk):
    """渲染一个分页块的表格 HTML（含自适应行高与字号）。"""
    row_h, scale = fit_chunk(chunk)
    font = "" if scale >= 0.995 else f' style="font-size:{round(10 * scale, 1)}pt"'
    parts = [f"<table{font}>{COLGROUP}",
             "<thead><tr>" + "".join(f"<th>{c}</th>" for c in header) + "</tr></thead>"]
    for _, cells in chunk:
        parts.append("<tr>" + "".join(
            f'<td style="height:{row_h}mm">{c}</td>' for c in cells) + "</tr>")
    parts.append("</table>")
    return parts


def find_edge():
    for p in EDGE_CANDIDATES:
        if p.is_file():
            return str(p)
    edge = shutil.which("msedge") or shutil.which("msedge.exe")
    return edge


def html_to_pdf(html_path, pdf_path, edge):
    tmp_profile = tempfile.mkdtemp(prefix="edge_pdf_")
    cmd = [
        edge, "--headless", "--disable-gpu", "--no-first-run",
        "--no-default-browser-check", "--no-pdf-header-footer",
        f"--user-data-dir={tmp_profile}",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not pdf_path.is_file() or pdf_path.stat().st_size < 1000:
        raise RuntimeError(
            f"Edge 转换失败: {html_path.name}\n"
            + (proc.stderr or b"").decode("utf-8", "ignore")[-800:]
        )


def convert_one(md_path, edge):
    html = md_to_html(md_path.read_text(encoding="utf-8"))
    tmp_dir = Path(tempfile.mkdtemp(prefix="vocab_html_"))
    html_path = tmp_dir / (md_path.stem + ".html")
    html_path.write_text(html, encoding="utf-8")
    pdf_path = PDF_DIR / (md_path.stem + ".pdf")
    try:
        html_to_pdf(html_path, pdf_path, edge)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return pdf_path


def main(argv):
    edge = find_edge()
    if not edge:
        print("[错误] 未找到 Edge，请安装 Microsoft Edge 后重试。")
        return 1
    PDF_DIR.mkdir(exist_ok=True)
    if argv:
        targets = [OUT_DIR / a for a in argv]
    else:
        targets = sorted(OUT_DIR.glob("*.md"))
    n = 0
    for md in targets:
        if not md.is_file():
            print(f"[跳过] 文件不存在: {md}")
            continue
        try:
            pdf = convert_one(md, edge)
            n += 1
            print(f"[完成] {md.name:>26} -> {pdf.relative_to(ROOT)} ({pdf.stat().st_size/1024:.0f} KB)")
        except Exception as e:
            print(f"[失败] {md.name}: {e}")
    print(f"\n共转换 {n} 个文件，输出目录: {PDF_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
