#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
convert_vocab.py — 将 word-list-*.yaml 词汇文件转换为适合 A4 黑白打印的 Markdown 词汇手册。

用法:
    python convert_vocab.py                     # 转换当前目录下全部 word-list-*.yaml
    python convert_vocab.py word-list-01.yaml   # 只转换指定文件
    python convert_vocab.py --per-page 40       # 自定义每页词数（默认 25）

输出 (output/ 目录):
    output/vocabulary-print-01.md ... vocabulary-print-48.md   每个分组一份
    output/vocabulary-print.md            word-list.yaml 合并版（含分节标题）

表格列序:
    第1遍 | 第2遍 | 第3遍 | 英文 | 抄写1 | 抄写2 | 中文释义 | 音标

特性:
    - 保留 YAML 原有词条顺序
    - 自动提取英文单词、音标、中文释义（去除释义后附带的英文注解，保持简洁）
    - 每 WORDS_PER_PAGE 个单词插入 "---" + "## Page N" 分页标记
    - 每行含 3 个复习打卡复选框 (□) 与 2 个等长抄写栏
    - 纯 Markdown 表格，无 emoji、无彩色，兼容 Typora 导出 PDF / A4 打印
    - 不依赖第三方库；对源数据中的格式瑕疵（重复键、错拼、全角空格、
      双 text 键、多行释义块等）做了容错处理
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "output"

WORDS_PER_PAGE = 25          # 每页词数（一个分页块）
COPY = "__________"          # 抄写栏，长度统一
BOX = "□"                    # 复习打卡复选框

TABLE_HEADER = "| 第1遍 | 第2遍 | 第3遍 | 英文 | 抄写1 | 抄写2 | 中文释义 | 音标 |"
TABLE_SEP = "|--------|--------|--------|--------|--------|--------|--------|--------|"

# ---------------------------------------------------------------- 容错解析 ----

_KEY_LINE_RE = re.compile(r"^ {2,}(title|text|example\w*)\s*:\s*(.*)$")
_TOP_KEY_RE = re.compile(r"^([^:]+?)\s*:\s*(.*)$")
_SECTION_RE = re.compile(r"^#\s*(Word List \d+)\s*$", re.I)
_BLOCK_MARKERS = ("|", "|-", "|+", ">", ">-", ">+")
# text 块内嵌的例句行（合并版 word-list.yaml 独有：1. ... 2. ... 或 eg. ...）
_EXAMPLE_LINE_RE = re.compile(r"^(?:[（(]?\d+[)）.、]\s*|e\.?g\.?\s)", re.I)


def parse_yaml(path):
    """容错解析 word-list YAML 文件。

    返回 (entries, sections)：
      entries  — 按序词条列表，每项 {'key', 'title', 'lines'}
                 lines 为 [(内容, 是否块内续行), ...]
      sections — [(词条索引, 'Word List NN'), ...]，来自 # Word List NN 注释
    """
    text = path.read_text(encoding="utf-8-sig")
    entries, sections = [], []
    cur, last_key = None, None
    for line in text.split("\n"):
        s = line.rstrip()
        stripped = s.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = _SECTION_RE.match(stripped)
            if m:
                sections.append((len(entries), m.group(1)))
            continue
        if not s.startswith((" ", "\t")):
            m = _TOP_KEY_RE.match(s)
            if m:
                cur = {"key": m.group(1).strip(), "title": "", "lines": []}
                entries.append(cur)
                last_key = "key"
                v = m.group(2).strip()
                if v:
                    cur["lines"].append((v, False))
                    last_key = "text"
            continue
        m = _KEY_LINE_RE.match(s)
        if m and cur is not None:
            k, v = m.group(1), m.group(2).strip()
            if k == "title":
                if not cur["title"]:
                    cur["title"] = v
                last_key = k
            elif k == "text":
                last_key = "text"
                if v and v not in _BLOCK_MARKERS:
                    # 同一词条出现多个 text 键时追加而非覆盖（如 inland 的双 text）
                    cur["lines"].append((v, False))
            else:
                last_key = k  # example / example0 / example1 ... 全部忽略
            continue
        # 文本块续行：仅当上一条键是 text 时并入
        if cur is not None and last_key == "text" and stripped:
            if _EXAMPLE_LINE_RE.match(stripped):
                continue  # 块内嵌例句（1. ... / eg. ...）不进入释义
            cur["lines"].append((stripped, True))
    return entries, sections


def merge_duplicates(entries):
    """合并同 key 的重复词条（数据中仅 contaminate 一例），保持首次出现顺序。

    返回 (merged, index_map)，index_map[原索引] = 新索引。
    """
    seen, merged, index_map = {}, [], {}
    for orig, e in enumerate(entries):
        if e["key"] in seen:
            target = seen[e["key"]]
            merged[target]["lines"].extend(e["lines"])
            if not merged[target]["title"]:
                merged[target]["title"] = e["title"]
            index_map[orig] = target
        else:
            seen[e["key"]] = len(merged)
            index_map[orig] = len(merged)
            merged.append(e)
    return merged, index_map


# ------------------------------------------------------------- 释义行解析 ----

_MARKER_RE = re.compile(r"_+([^_\n]+?)_+")          # __word__ / _word_
_LEAD_PHON_RE = re.compile(r"^\[([^\[\]\n]+?)\]+")  # 词后紧跟的音标（兼容 ]]] 等多余右括号）
_LEAD_ASCII_RE = re.compile(r"^([A-Za-z][A-Za-z ;'/.\-&]*?)(?=$|[\[一-鿿])")
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

_POS_WORD = r"(?:adj|adv|excl|interj|int|abbr|conj|prep|pron|aux|art|det|num|vi|vt|ad|a|v|n)\."
_POS_CHAIN = rf"(?:{_POS_WORD}/)*{_POS_WORD}"
_POS_FIND_RE = re.compile(rf"(?<![A-Za-z.])({_POS_CHAIN})(?![A-Za-z])")


def _phon_like(s):
    """判断括号内容是否像音标（含 IPA 字符，或纯小写简化注音），
    区别于 [pl.]、[the R-]、[医] 等语法/领域标签。"""
    if re.search(r"[^A-Za-z0-9 ,;./'()\-]", s):
        return True
    return bool(re.fullmatch(r"[a-z]{2,15}", s))


def _trim_gloss(meaning):
    """去掉中文释义后附带的英文注解（如 "to have too little of something"）。"""
    meaning = meaning.strip()
    idx = -1
    for i, ch in enumerate(meaning):
        if _CJK_RE.match(ch):
            idx = i
    if idx >= 0:
        tail = meaning[idx + 1:]
        if tail and re.fullmatch(r"[\sA-Za-z,.;:()'\-/]*", tail):
            meaning = meaning[: idx + 1]
    return meaning.strip()


def _split_senses(tail):
    """把释义拆成 (词性, 释义) 段，支持一行多个词性（如 n. ... a. ...）。"""
    matches = list(_POS_FIND_RE.finditer(tail))
    if not matches:
        return [("", tail)] if tail.strip() else []
    segs = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tail)
        segs.append((m.group(1), tail[m.end():end]))
    pre = tail[: matches[0].start()].strip()
    if pre and segs and segs[0][0]:
        # 词性前的文字（如 [医]）并入其后释义
        segs[0] = (segs[0][0], (pre + " " + segs[0][1]).strip())
    return segs


def _same_word(a, key):
    """行首提取的词与词条 key 是否一致（连字符等价空格）。"""
    if not key:
        return False
    a = (a or "").strip().lower()
    k = key.strip().lower()
    return a == k or a == k.replace("-", " ")


def _parse_sense_line(line, allow_ascii_word=True, key=None):
    """解析一行释义，返回 (候选词, 音标, 词性释义段列表)。"""
    line = line.replace("　", " ").strip()  # 全角空格归一化
    # 词中混入的下划线（如 __militar_y_）：合并字母间的下划线
    line = re.sub(r"([A-Za-z])_+([A-Za-z])", r"\1\2", line)
    line_word, phonetic = None, ""

    markers = _MARKER_RE.findall(line)
    if markers:
        parts = [m.strip() for m in markers if m.strip(" _")]
        if parts:
            line_word = " ".join(parts)
        rest = _MARKER_RE.sub(" ", line).strip()
    else:
        # ____ 之类占位标记（如 revolution）：下划线后跟空白时整段剥除
        line = re.sub(r"^_+\s+", "", line)
        m = _LEAD_ASCII_RE.match(line)
        if m:
            extracted = m.group(1).strip()
            # 块内续行仅在提取词与词条 key 一致时才采纳
            # （避免把英文释义续行误当词；如 post-mortem 块内首行）
            if allow_ascii_word or _same_word(extracted, key):
                line_word = extracted
                rest = line[m.end():].strip()
            else:
                rest = line
        else:
            rest = line

    m = _LEAD_PHON_RE.match(rest)
    if m:
        # 词后紧跟的括号一律视为音标（数据中词前位置不存在标签类括号；
        # [pl.]、[the R-] 等标签均出现在词性或汉字之后，不受影响）
        phonetic = m.group(1)
        rest = rest[m.end():].strip()
        # 并列的变体读音： [kənˈtrɪbjuːt] [ˈkɒntrɪbjuːt] 或 [..];[..] —— 只保留首个
        rest = re.sub(r"^[;，,]\s*", "", rest)
        while True:
            m2 = _LEAD_PHON_RE.match(rest)
            if m2 and _phon_like(m2.group(1)):
                rest = rest[m2.end():].strip()
                rest = re.sub(r"^[;，,]\s*", "", rest)
            else:
                break

    senses = []
    for pos, meaning in _split_senses(rest):
        meaning = _trim_gloss(meaning)
        # 释义尾部的读音标注（如 把……归于, [ˈætrɪbjuːt]）：移除
        m = re.search(r"\s*[，,]?\s*\[([^\[\]\n]+)\]\s*$", meaning)
        if m and _phon_like(m.group(1)):
            meaning = meaning[: m.start()].rstrip().rstrip("，,;；")
        if meaning or pos:  # 保留带词性的空释义段（换行释义会被并入）
            senses.append((pos, meaning))
    return line_word, phonetic, senses


# ----------------------------------------------------------------- 词条解析 ----

def parse_entry(entry):
    """把一条 YAML 词条解析为 {'word', 'phonetic', 'senses'}。"""
    key = entry["key"]
    title = (entry["title"] or "").strip()

    word, phonetic, senses = None, "", []
    phrase_hint = False
    for raw, is_cont in entry["lines"]:
        lw, ph, sens = _parse_sense_line(raw, allow_ascii_word=not is_cont, key=key)
        if lw:
            if " " in lw:
                phrase_hint = True
            if word is None:
                word = lw
        if ph and not phonetic:
            phonetic = ph
        if sens and senses and all(not p for p, _ in sens):
            # 纯释义续行（如换行的中文释义）并入上一条带词性的空释义段
            attached = False
            for j in range(len(senses) - 1, -1, -1):
                p, m = senses[j]
                if p and not m:
                    senses[j] = (p, sens[0][1])
                    attached = True
                    break
            if not attached:
                senses.extend(sens)
        else:
            senses.extend(sens)

    # 词条英文：以 key 为准（源数据中 title/text 存在较多拼写错误）；
    # 词组类（文本或 title 中带空格）把 key 的连字符换成空格。
    if title and " " in title:
        phrase_hint = True
    if phrase_hint and "-" in key:
        display = key.replace("-", " ")
    else:
        display = key
    if not display:
        display = word or title or key

    return {"word": display, "phonetic": phonetic, "senses": senses or [("", "")]}


def to_row(rec):
    """生成一行 Markdown 表格（列序：□×3 | 英文 | 抄写×2 | 中文释义 | 音标）。"""
    phonetic = f"/{rec['phonetic']}/" if rec["phonetic"] else ""
    cells = []
    for pos, m in rec["senses"]:
        if not m:
            continue
        cells.append(f"{pos} {m}".strip() if pos else m)
    meaning = "<br>".join(cells)
    return f"| {BOX} | {BOX} | {BOX} | {rec['word']} | {COPY} | {COPY} | {meaning} | {phonetic} |"


# ----------------------------------------------------------------- 生成文档 ----

def render_chunks(records, page_start=1, per_page=WORDS_PER_PAGE):
    """把词条按每页词数切成块。

    返回 (chunks, next_page)：
      chunks — [(页码或 None, 词条列表), ...]，None 表示紧跟标题的首块不插分页标记
      next_page — 下一块应使用的页码（合并版跨节连续编号用）
    """
    chunks = []
    page = page_start
    for i in range(0, len(records), per_page):
        chunks.append((None if i == 0 else page, records[i:i + per_page]))
        page += 1
    return chunks, page


def chunk_to_md(label, chunk):
    """一个分页块 → Markdown 表格文本。"""
    body = []
    if label is not None:
        body.append(f"\n---\n\n## Page {label}\n")
    body.append(TABLE_HEADER)
    body.append(TABLE_SEP)
    body.extend(to_row(r) for r in chunk)
    return "\n".join(body)


def render_md(title, records, per_page=WORDS_PER_PAGE):
    """records → 完整 Markdown 文档（单列表文件用）。"""
    chunks, _ = render_chunks(records, per_page=per_page)
    parts = [f"# {title}", ""]
    parts.extend(chunk_to_md(label, chunk) for label, chunk in chunks)
    return "\n".join(parts) + "\n"


def output_name(yaml_path):
    """word-list-01.yaml → vocabulary-print-01.md；word-list.yaml → vocabulary-print.md"""
    if yaml_path.stem == "word-list":
        return "vocabulary-print.md"
    return f"vocabulary-print-{yaml_path.stem.split('-')[-1]}.md"


def render_master(records, sections, index_map, per_page=WORDS_PER_PAGE):
    """合并版：按 # Word List NN 注释分节，页码全书连续。"""
    parts = ["# IELTS 雅思词汇手册", ""]
    starts = [(index_map[i], name) for i, name in sections if i in index_map]
    page = 1
    for si, (start, name) in enumerate(starts):
        stop = starts[si + 1][0] if si + 1 < len(starts) else len(records)
        seg = records[start:stop]
        if not seg:
            continue
        parts.append(f"# {name}")
        parts.append("")
        chunks, page = render_chunks(seg, page_start=page, per_page=per_page)
        parts.extend(chunk_to_md(label, chunk) for label, chunk in chunks)
    return "\n".join(parts) + "\n"


def convert_file(yaml_path, per_page=WORDS_PER_PAGE):
    entries, sections = parse_yaml(yaml_path)
    entries, index_map = merge_duplicates(entries)
    records = [parse_entry(e) for e in entries]

    if yaml_path.stem == "word-list":
        md = render_master(records, sections, index_map, per_page=per_page)
    else:
        m = re.search(r"(\d+)$", yaml_path.stem)
        title = f"Word List {m.group(1)}" if m else yaml_path.stem
        md = render_md(title, records, per_page=per_page)

    out_path = OUT_DIR / output_name(yaml_path)
    out_path.write_text(md, encoding="utf-8")
    return out_path, len(records)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="将 word-list YAML 转换为 A4 打印版 Markdown 词汇手册")
    ap.add_argument("files", nargs="*", help="要转换的 yaml 文件（默认全部）")
    ap.add_argument("--per-page", type=int, default=WORDS_PER_PAGE,
                    help=f"每页词数（默认 {WORDS_PER_PAGE}）")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(exist_ok=True)
    if args.files:
        targets = [ROOT / a for a in args.files]
    else:
        targets = sorted(ROOT.glob("word-list-*.yaml")) + [ROOT / "word-list.yaml"]
    total = 0
    for p in targets:
        if not p.is_file():
            print(f"[跳过] 文件不存在: {p}")
            continue
        out, n = convert_file(p, per_page=args.per_page)
        total += n
        print(f"[完成] {p.name:>18} ({n:>4} 词) -> {out.relative_to(ROOT)}")
    print(f"\n共 {total} 个词条，每页 {args.per_page} 词，输出目录: {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
