"""通用教辅/真题 PDF 导入 CLI。

分阶段执行：``ocr``（页面级 PaddleOCR 识别缓存）→ ``build``（拆题与解析匹配）→
``import``（入库）→ ``report``（对账报告）。页面识别结果缓存于
``.system_generated/book_import/ocr/``，重复执行自动跳过已识别页面，支持断点续跑。

用法示例（从项目根目录运行）::

    python3 -m scripts.import_paper_book ocr \\
        --pdf "docs/【A4紧凑版】880数一高数篇做题本.pdf" \\
        --key 880-shuyi-gaoshu-zuotiben --start 2

    python3 -m scripts.import_paper_book build \\
        --profile scripts/profiles/880-math1-gaoshu.json

    python3 -m scripts.import_paper_book import \\
        --profile scripts/profiles/880-math1-gaoshu.json --apply
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

from mathbank.paths import SYSTEM_GENERATED_DIR, UPLOADS_DIR
from mathbank.paddle_ocr import PaddleOCRError, paddle_ocr_page_assets

RENDER_DPI = 150
OCR_CONCURRENCY = 4
OCR_RETRIES = 2
OCR_CACHE_DIR = SYSTEM_GENERATED_DIR / "book_import" / "ocr"
BUILD_DIR = SYSTEM_GENERATED_DIR / "book_import" / "build"

# 章标题行（允许 Markdown 标题前缀），用于识别分册边界；目录页的点线行会被排除。
CHAPTER_HEADING_RE = re.compile(r"^#{0,4}\s*第[一二三四五六七八九十百]+\s*章")
DOT_LEADER_RE = re.compile(r"\.{4,}")

# PaddleOCR-VL 的页面插图：markdown 中以 imgs/xxx 引用，图片本体是临时 URL。
IMG_REF_RE = re.compile(r"imgs/([A-Za-z0-9][A-Za-z0-9_.-]*)")
IMG_HTML_RE = re.compile(
    r"<div[^>]*>\s*<img[^>]*src=\"[^\"]*?imgs/([A-Za-z0-9][A-Za-z0-9_.-]*)[^\"]*\"[^>]*/?\s*>\s*</div>"
    r"|<img[^>]*src=\"[^\"]*?imgs/([A-Za-z0-9][A-Za-z0-9_.-]*)[^\"]*\"[^>]*/?\s*>"
)


def asset_cache_file(key: str, page: int, image_name: str) -> Path:
    return OCR_CACHE_DIR / key / "assets" / f"p{page:04d}" / Path(image_name).name


def referenced_images(text: str) -> list[str]:
    """按出现顺序返回文本中引用的全部 imgs/... 图片名。"""

    seen: list[str] = []
    for match in IMG_REF_RE.finditer(text or ""):
        name = f"imgs/{match.group(1)}"
        if name not in seen:
            seen.append(name)
    return seen


def page_cache_complete(key: str, page: int, text: str) -> bool:
    """缓存命中要求 md 存在且其中引用的每张图都已落盘；空白页视为完整。"""

    if not text:
        return True
    for name in referenced_images(text):
        if not asset_cache_file(key, page, name).is_file():
            return False
    return True

# ---------------- 教辅结构解析 ----------------

CHINESE_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CHAPTER_RE = re.compile(r"^#{0,4}\s*第([一二三四五六七八九十]+)\s*章")
PART_RE = re.compile(r"^#{0,4}\s*(基础题|综合题|拓展题)\s*$")
QTYPE_RE = re.compile(r"^#{0,4}\s*(?:[一二三][、.]\s*)?(选择题|填空题|解答题|证明题)\s*$")
QUESTION_NUM_RE = re.compile(r"^\s*[（(](\d{1,3})[)）]\s*")
# OCR 常把题号粘连在前一句末尾（如「错误。(4)D.」），按句末标点后的题号边界预切分。
GLUED_SPLIT_RE = re.compile(r"(?<=[。．;；.])\s*(?=[（(]\d{1,3}[)）])")
# 子小问常用罗马数字（含全角 Ⅰ Ⅱ Ⅲ …），不作为题号。
ROMAN_ITEM_RE = re.compile(r"^\s*[（(][IVX]+[)）]|^\s*[（(][ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[)）]")
NOISE_LINE_RE = re.compile(
    r"小坏蛋|nocode\.host|做题本集结地|公众号|第\s*\d+\s*页\s*[，,]?\s*共\s*\d+\s*页|^[.\s]*$"
)
OPTION_MARKER_RE = re.compile(r"(?:^|[\s\n])([ABCD])[.、．]\s*")
EMPTY_PAREN_TAIL_RE = re.compile(r"(?:\s*[，,]?\s*[（(]\s*[)）]\s*[.。,，；;]?\s*)+$")
CHOICE_ANSWER_RE = re.compile(r"^\s*([A-D])\s*[.、．]?\s*")

QTYPE_SHORT = {"选择题": "选择", "填空题": "填空", "解答题": "解答", "证明题": "解答"}


def cn_chapter_to_int(text: str) -> int | None:
    """把「一/二/…/九/十」章节序号转为整数。"""

    if text == "十":
        return 10
    if "十" in text:
        left, _, right = text.partition("十")
        tens = CHINESE_DIGIT.get(left, 1) if left else 1
        ones = CHINESE_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    return CHINESE_DIGIT.get(text)


def normalize_fillin_macro(text: str) -> str:
    """与 main.py 的同名规范化保持一致：任何下划线形态统一为 \\fillin。"""

    if not text:
        return text or ""
    text = re.sub(r"_{3,}", r"\\fillin", text)
    text = re.sub(r"\\fillin\s*\[[^\]]*?\](?:\[[^\]]*?\])?", r"\\fillin", text)
    text = re.sub(r"\\underline\s*\{[^}]*?\}", r"\\fillin", text)
    text = re.sub(r"\\fillin\}", r"\\fillin", text)
    return text


def strip_empty_paren_tail(stem: str) -> str:
    """移除题干末尾的全角/半角空括号（含后续标点），防止与 \\paren 宏重叠。"""

    return EMPTY_PAREN_TAIL_RE.sub("", stem).rstrip()


def split_choice_options(text: str) -> tuple[list[str] | None, str]:
    """从选择题文本中拆出 A–D 选项；返回 (选项列表或 None, 题干)。"""

    markers = [
        (m.start(), m.end(), m.group(1)) for m in OPTION_MARKER_RE.finditer(text)
    ]
    first_a = next((idx for idx, m in enumerate(markers) if m[2] == "A"), None)
    if first_a is None:
        return None, text
    tail = markers[first_a:]
    letters = [m[2] for m in tail]
    if letters[:4] != ["A", "B", "C", "D"] or len(tail) < 4:
        return None, text
    stem = text[: tail[0][0]].rstrip()
    options = []
    for i in range(4):
        seg_start = tail[i][1]
        seg_end = tail[i + 1][0] if i + 1 < len(tail) else len(text)
        options.append(text[seg_start:seg_end].strip())
    if any(not opt for opt in options):
        return None, text
    return options, stem


def convert_figure_markup(text: str) -> str:
    """把 PaddleOCR 的 HTML 插图包装行原位转换为系统 Markdown 图引用。"""

    def _replace(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return f"![题目插图](imgs/{name})"

    return IMG_HTML_RE.sub(_replace, text)


def build_choices_env(options: list[str]) -> str:
    body = "\n".join(f"\\item {opt}" for opt in options)
    return "\\begin{choices}\n" + body + "\n\\end{choices}"


def cache_file(key: str, page_index: int) -> Path:
    return OCR_CACHE_DIR / key / f"p{page_index:04d}.md"


def pdf_page_count(pdf_path: Path) -> int:
    import fitz

    doc = fitz.open(pdf_path)
    try:
        return doc.page_count
    finally:
        doc.close()


def render_page(pdf_path: Path, page_index: int, target: Path) -> None:
    import fitz

    doc = fitz.open(pdf_path)
    try:
        if not 0 <= page_index < doc.page_count:
            raise SystemExit(f"页码越界: {page_index}（共 {doc.page_count} 页）")
        doc[page_index].get_pixmap(dpi=RENDER_DPI).save(target)
    finally:
        doc.close()


def page_has_chapter_heading(text: str, marker: str) -> bool:
    """判断页面是否出现真正的章节大标题（排除目录页的点线行）。"""

    if not marker or not text:
        return False
    lines = text.splitlines()
    if sum(1 for line in lines if DOT_LEADER_RE.search(line)) >= 3:
        return False
    for line in lines:
        if DOT_LEADER_RE.search(line):
            continue
        stripped = line.strip()
        if marker in stripped and CHAPTER_HEADING_RE.match(stripped):
            return True
    return False


def download_page_assets(key: str, page: int, assets: dict[str, str]) -> None:
    """把 PaddleOCR 返回的临时图片 URL 下载到本地资产缓存。"""

    import requests

    for name, url in assets.items():
        safe_name = Path(name).name
        if not safe_name or safe_name.startswith("."):
            continue
        target = asset_cache_file(key, page, safe_name)
        if target.is_file() and target.stat().st_size > 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(OCR_RETRIES + 1):
            try:
                response = requests.get(url, timeout=120.0)
                response.raise_for_status()
                if not response.content:
                    raise RuntimeError("图片内容为空")
                target.write_bytes(response.content)
                break
            except Exception as exc:
                last_error = exc
                if attempt < OCR_RETRIES:
                    time.sleep(2.0 * (attempt + 1))
        else:
            if target.exists():
                target.unlink()
            raise RuntimeError(f"图片 {safe_name} 下载失败: {last_error}")


def ocr_page(pdf_path: Path, key: str, page_index: int, env: dict[str, str]) -> str:
    """识别单页并写入缓存；已缓存且图片资产齐全的页直接复用，失败页不落盘。"""

    cached = cache_file(key, page_index)
    if cached.is_file():
        text = cached.read_text(encoding="utf-8")
        if page_cache_complete(key, page_index, text):
            return text

    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp_png = cached.with_suffix(".png")
    last_error: Exception | None = None
    try:
        for attempt in range(OCR_RETRIES + 1):
            render_page(pdf_path, page_index, tmp_png)
            try:
                text, assets = paddle_ocr_page_assets(
                    str(tmp_png),
                    access_token=env["PADDLEOCR_ACCESS_TOKEN"],
                    base_url=env.get("PADDLEOCR_BASE_URL", ""),
                    model=env.get("PADDLEOCR_MODEL", "PaddleOCR-VL-1.6"),
                )
                # 临时 URL 会过期：先落盘全部图片，再写 markdown 缓存。
                download_page_assets(key, page_index, assets)
                missing = [
                    name
                    for name in referenced_images(text)
                    if not asset_cache_file(key, page_index, name).is_file()
                ]
                if missing:
                    raise RuntimeError(f"结果引用的图片未被提供: {missing[:3]}")
                cached.write_text(text, encoding="utf-8")
                return text
            except (PaddleOCRError, TimeoutError, OSError, RuntimeError) as exc:
                last_error = exc
                if attempt < OCR_RETRIES:
                    time.sleep(3.0 * (attempt + 1))
    finally:
        tmp_png.unlink(missing_ok=True)
    raise RuntimeError(f"第 {page_index} 页识别失败: {last_error}")


def cmd_ocr(args: argparse.Namespace) -> int:
    load_dotenv()
    env = {k: v for k, v in os.environ.items() if k.startswith("PADDLEOCR")}
    if not env.get("PADDLEOCR_ACCESS_TOKEN"):
        print("缺少 PADDLEOCR_ACCESS_TOKEN，请检查 .env 配置。", file=sys.stderr)
        return 2

    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"PDF 不存在: {pdf_path}", file=sys.stderr)
        return 2

    total = pdf_page_count(pdf_path)
    start = max(args.start, 0)
    end = min(args.end, total) if args.end else total
    pages = list(range(start, end))
    print(f"[ocr] {pdf_path.name} 共 {total} 页，识别范围 {start}~{end - 1}，并发 {args.workers}")

    done = failed = 0
    stop_page: int | None = None
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        index = 0
        while index < len(pages):
            wave = pages[index : index + args.workers]
            futures = {
                page: pool.submit(ocr_page, pdf_path, args.key, page, env)
                for page in wave
            }
            texts: dict[int, str] = {}
            for page, future in futures.items():
                try:
                    texts[page] = future.result()
                    done += 1
                except Exception as exc:  # 单页失败不阻断整体，未缓存页可重跑补齐
                    failed += 1
                    print(f"[ocr] 第 {page} 页失败: {exc}", file=sys.stderr)
            for page in sorted(texts):
                if args.stop_marker and page_has_chapter_heading(
                    texts[page], args.stop_marker
                ):
                    stop_page = page
                    break
            if stop_page is not None:
                print(f"[ocr] 第 {stop_page} 页检测到「{args.stop_marker}」章节标题，高数范围到此为止")
                break
            index += args.workers

    summary = f"[ocr] 完成 {done} 页，失败 {failed} 页"
    if stop_page is not None:
        summary += f"，边界页 p{stop_page}"
    print(summary)
    return 1 if failed else 0


class BookSectionParser:
    """按（章，难度块，题型，题号）解析做题本/解析分册的 Markdown 流。

    题号在每个题型块内从 1 重新计数；编号回退或跳跃视为当前题的延续并打标记，
    交由对账报告人工复核，不做静默丢弃。
    """

    def __init__(self) -> None:
        self.chapter: int | None = None
        self.chapter_label: str = ""
        self.block: str | None = None
        self.qtype: str | None = None
        self.expected = 0
        self.items: list[dict] = []
        self.current: dict | None = None

    def _flush(self) -> None:
        self.current = None

    def _start(self, number: int, page: int, first_line: str) -> None:
        self.current = {
            "chapter": self.chapter,
            "chapter_label": self.chapter_label,
            "block": self.block,
            "qtype": self.qtype,
            "number": number,
            "body_lines": [first_line],
            "pages": [page],
            "flags": [],
        }
        self.items.append(self.current)
        self.expected = number + 1

    def feed(self, page: int, raw_line: str) -> None:
        for segment in GLUED_SPLIT_RE.split(raw_line):
            self._feed_segment(page, segment)

    def _feed_segment(self, page: int, raw_line: str) -> None:
        line = raw_line.strip()
        if not line or NOISE_LINE_RE.search(line):
            return

        if DOT_LEADER_RE.search(line):
            return

        chapter_match = CHAPTER_RE.match(line)
        if chapter_match:
            self.chapter = cn_chapter_to_int(chapter_match.group(1))
            self.chapter_label = f"第{chapter_match.group(1)}章"
            self.block = None
            self.qtype = None
            self.expected = 0
            self._flush()
            return
        part_match = PART_RE.match(line)
        if part_match:
            self.block = part_match.group(1)
            self.qtype = None
            self.expected = 0
            self._flush()
            return
        qtype_match = QTYPE_RE.match(line)
        if qtype_match:
            self.qtype = qtype_match.group(1)
            self.expected = 1
            self._flush()
            return
        if not self.qtype:
            return

        number_match = QUESTION_NUM_RE.match(line)
        if number_match:
            number = int(number_match.group(1))
            if self.current is None:
                # 块内第一题：接受任意编号（OCR 可能漏识前文），异常时打标记。
                expected_before = self.expected
                rest = line[number_match.end():].strip()
                self._start(number, page, rest)
                if number != expected_before:
                    self.current["flags"].append(f"起始编号异常({number})")
                return
            if number >= self.expected:
                # 行首编号按预期推进；前向跳跃视为前题被粘连吞并后自愈开新题。
                expected_before = self.expected
                rest = line[number_match.end():].strip()
                self._start(number, page, rest)
                if number > expected_before:
                    self.current["flags"].append(
                        f"编号跳跃(缺{expected_before}~{number - 1})"
                    )
                return
            # 编号回退：视为当前题的子小问，保留在题干内并打标记供人工复核。
            self.current["flags"].append(f"编号回退({number})")
            self.current["body_lines"].append(line)
            if page not in self.current["pages"]:
                self.current["pages"].append(page)
            return

        if self.current is not None:
            self.current["body_lines"].append(line)
            if page not in self.current["pages"]:
                self.current["pages"].append(page)


def parse_cached_pages(cache_key: str, start: int, end: int) -> list[tuple[int, str]]:
    """读取 OCR 缓存页并返回 (页码, 行) 序列；缺失页自动跳过并告警。"""

    cache_dir = OCR_CACHE_DIR / cache_key
    cached_pages = sorted(
        int(path.stem[1:])
        for path in cache_dir.glob("p*.md")
        if path.stem[1:].isdigit()
    ) if cache_dir.is_dir() else []
    if not cached_pages:
        raise SystemExit(f"OCR 缓存目录为空或不存在: {cache_dir}，请先运行 ocr 阶段。")
    # 未显式指定结束页时，按缓存目录实际最大页封顶，避免遍历到默认大数。
    end = min(end, cached_pages[-1] + 1)

    sequence: list[tuple[int, str]] = []
    for page in range(start, end):
        cached = cache_file(cache_key, page)
        if not cached.is_file():
            print(f"[build] 警告: 缺少 OCR 缓存页 p{page:04d}，已跳过", file=sys.stderr)
            continue
        for raw_line in cached.read_text(encoding="utf-8").splitlines():
            sequence.append((page, raw_line))
    return sequence


def item_body(item: dict) -> str:
    return "\n".join(item["body_lines"]).strip()


def compose_question_record(item: dict, profile: dict, solution: dict | None) -> dict:
    block_map = profile["blocks"][item["block"]]
    qtype_map = profile["question_types"]
    body = item_body(item)
    content = convert_figure_markup(strip_empty_paren_tail(body))

    answer_markdown = ""
    if solution is not None:
        sol_body = item_body(solution)
        choice_letter = CHOICE_ANSWER_RE.match(sol_body)
        if item["qtype"] == "选择题" and choice_letter:
            answer_markdown = (
                f"**答案** {choice_letter.group(1)}\n\n"
                + sol_body[choice_letter.end():].strip()
            )
        else:
            answer_markdown = sol_body

    options, stem = (None, content)
    if item["qtype"] == "选择题":
        options, stem = split_choice_options(content)
        stem = strip_empty_paren_tail(stem)
        if options is None:
            item["flags"].append("选项拆分失败")
        else:
            content = stem + "\n\n" + build_choices_env(options)
    content = normalize_fillin_macro(content)

    chapter_scope = item.get("chapter_label") or f"第{item['chapter']}章"
    return {
        "chapter": item["chapter"],
        "block": item["block"],
        "qtype": item["qtype"],
        "number": item["number"],
        "question_type": qtype_map.get(item["qtype"], "detailed_answer"),
        "difficulty": block_map["difficulty"],
        "source_name": block_map["source"],
        "source_scope": f"{chapter_scope}·{QTYPE_SHORT.get(item['qtype'], item['qtype'])}",
        "exam_track": profile.get("exam_track", "数学一"),
        "subject": profile.get("subject", "高等数学"),
        "topic": profile.get("chapter_topics", {}).get(str(item["chapter"]), ""),
        "content": content,
        "answer_markdown": convert_figure_markup(normalize_fillin_macro(answer_markdown)),
        "pages": item["pages"],
        "solution_pages": solution["pages"] if solution else [],
        "flags": item["flags"] + (["无匹配解析"] if solution is None else []),
        "solution_found": solution is not None,
    }


def load_profile(path: str) -> dict:
    profile = json.loads(Path(path).read_text(encoding="utf-8"))
    for required in ("question_book", "solution_book", "blocks", "question_types"):
        if required not in profile:
            raise SystemExit(f"profile 缺少必需字段: {required}")
    return profile


def build_output_dir(profile_path: str) -> Path:
    stem = Path(profile_path).stem
    out_dir = BUILD_DIR / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def cmd_build(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    q_cfg = profile["question_book"]
    s_cfg = profile["solution_book"]

    book = BookSectionParser()
    for page, line in parse_cached_pages(
        q_cfg["cache_key"], q_cfg.get("start_page", 0), q_cfg.get("end_page", 10**9)
    ):
        book.feed(page, line)

    solutions_parser = BookSectionParser()
    for page, line in parse_cached_pages(
        s_cfg["cache_key"], s_cfg.get("start_page", 0), s_cfg.get("end_page", 10**9)
    ):
        solutions_parser.feed(page, line)

    solution_map = {
        (i["chapter"], i["block"], i["qtype"], i["number"]): i
        for i in solutions_parser.items
    }

    records = []
    for item in book.items:
        key = (item["chapter"], item["block"], item["qtype"], item["number"])
        records.append(compose_question_record(item, profile, solution_map.get(key)))

    unmatched_solutions = [
        (i["chapter"], i["block"], i["qtype"], i["number"])
        for i in solutions_parser.items
        if (i["chapter"], i["block"], i["qtype"], i["number"])
        not in {(q["chapter"], q["block"], q["qtype"], q["number"]) for q in book.items}
    ]

    out_dir = build_output_dir(args.profile)
    questions_path = out_dir / "questions.json"
    questions_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 对账报告：块级计数矩阵 + 异常清单
    lines = []
    lines.append(f"题目总数: {len(records)}（解析分册条目 {len(solution_map)}）")
    lines.append(f"匹配解析: {sum(1 for r in records if r['solution_found'])}")
    lines.append(f"未匹配解析: {sum(1 for r in records if not r['solution_found'])}")
    if unmatched_solutions:
        lines.append(f"解析分册多余条目: {len(unmatched_solutions)}")
        for key in unmatched_solutions[:40]:
            lines.append(f"  - {'·'.join(map(str, key))}")
    flagged = [r for r in records if r["flags"]]
    lines.append(f"带异常标记题目: {len(flagged)}")
    for r in flagged[:60]:
        label = f"{r['source_name']}·{r['source_scope']}({r['number']})"
        lines.append(f"  - {label}: {','.join(r['flags'])}")
    short = [r for r in records if len(r["content"]) < 15]
    lines.append(f"题干过短(<15字符): {len(short)}")
    figure_records = [
        r for r in records
        if "imgs/" in r["content"] or "imgs/" in (r["answer_markdown"] or "")
    ]
    lines.append(
        f"带插图题目: {len(figure_records)}"
        f"（题干 {sum(1 for r in records if 'imgs/' in r['content'])} / "
        f"解答 {sum(1 for r in records if 'imgs/' in (r['answer_markdown'] or ''))}）"
    )
    matrix: dict[tuple, list[int]] = {}
    for r in records:
        matrix.setdefault((r["chapter"], r["block"]), [0, 0, 0])
        matrix[(r["chapter"], r["block"])][
            {"选择题": 0, "填空题": 1, "解答题": 2}.get(r["qtype"], 2)
        ] += 1
    lines.append("块级题量矩阵 (章·块: 选择/填空/解答):")
    for key in sorted(matrix):
        c, b = key
        lines.append(f"  第{c}章·{b}: {'/'.join(map(str, matrix[key]))}")

    report_path = out_dir / "build-report.txt"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[build] 题目已写入 {questions_path}")
    print(f"[build] 报告已写入 {report_path}")
    return 0


BLOCK_LETTER = {"基础题": "b", "综合题": "c", "拓展题": "t"}


def locate_page_asset(cache_key: str, name: str, pages: list[int]) -> Path | None:
    """按页码优先定位图片资产缓存，找不到再全目录兜底。"""

    for page in pages or []:
        candidate = asset_cache_file(cache_key, page, name)
        if candidate.is_file():
            return candidate
    safe_name = Path(name).name
    for candidate in sorted((OCR_CACHE_DIR / cache_key / "assets").glob(f"p*/{safe_name}")):
        if candidate.is_file():
            return candidate
    return None


def materialize_figures(
    text: str,
    *,
    cache_key: str,
    pages: list[int],
    name_prefix: str,
    uploads_root: Path,
    stats: dict[str, int],
) -> tuple[str, list[str]]:
    """把文本中的 ``imgs/...`` 引用替换为 ``/static/uploads/...`` 并落盘图片。

    返回 (新文本, 规范化 URL 列表)；无法定位或校验失败的引用保持原样并计数。
    """

    from mathbank.asset_security import (
        InvalidImageError,
        normalize_raster_image,
        normalize_upload_asset_references,
    )

    urls: list[str] = []

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        source = locate_page_asset(cache_key, name, pages)
        if source is None:
            stats["figures_missing"] = stats.get("figures_missing", 0) + 1
            return match.group(0)
        try:
            normalized = normalize_raster_image(source.read_bytes())
        except (InvalidImageError, OSError) as exc:
            print(f"[import] 图片校验失败 {name}: {exc}", file=sys.stderr)
            stats["figures_invalid"] = stats.get("figures_invalid", 0) + 1
            return match.group(0)
        target = uploads_root / f"{name_prefix}-{len(urls)}{normalized.extension}"
        uploads_root.mkdir(parents=True, exist_ok=True)
        target.write_bytes(normalized.data)
        try:
            url = normalize_upload_asset_references(
                [target.name], uploads_dir=uploads_root, url_prefix="static/uploads"
            )[0]
        except Exception as exc:
            print(f"[import] 图片规范化失败 {name}: {exc}", file=sys.stderr)
            stats["figures_invalid"] = stats.get("figures_invalid", 0) + 1
            return match.group(0)
        urls.append(url)
        return url

    new_text = IMG_REF_RE.sub(_replace, text)
    return new_text, urls


def cmd_import(args: argparse.Namespace) -> int:
    from mathbank.curriculums import KAOYAN_VERSION
    from mathbank.database import (
        Question,
        QuestionCurriculum,
        SessionLocal,
        Source,
        init_db,
    )
    from mathbank.sync_helper import export_database_to_files

    profile = load_profile(args.profile)
    out_dir = build_output_dir(args.profile)
    questions_path = out_dir / "questions.json"
    if not questions_path.is_file():
        print(f"未找到构建产物 {questions_path}，请先运行 build。", file=sys.stderr)
        return 2
    records = json.loads(questions_path.read_text(encoding="utf-8"))

    init_db()
    from mathbank.asset_security import normalize_upload_asset_references

    db = SessionLocal()
    created = skipped = failed = updated = 0
    source_ids: dict[str, int] = {}
    figure_stats: dict[str, int] = {}
    question_cache_key = profile["question_book"]["cache_key"]
    solution_cache_key = profile["solution_book"]["cache_key"]
    uploads_root = Path(UPLOADS_DIR)

    def record_figure_prefix(record: dict) -> str:
        return (
            f"mb880-c{record['chapter']}-"
            f"{BLOCK_LETTER.get(record['block'], 'x')}-q{record['number']}"
        )

    def materialize_record(record: dict) -> tuple[str, str, list[str]]:
        prefix = record_figure_prefix(record)
        content, content_urls = materialize_figures(
            record["content"],
            cache_key=question_cache_key,
            pages=record.get("pages", []),
            name_prefix=f"{prefix}-c",
            uploads_root=uploads_root,
            stats=figure_stats,
        )
        answer, answer_urls = materialize_figures(
            record["answer_markdown"] or "",
            cache_key=solution_cache_key,
            pages=record.get("solution_pages", []),
            name_prefix=f"{prefix}-a",
            uploads_root=uploads_root,
            stats=figure_stats,
        )
        return content, answer, list(dict.fromkeys(content_urls + answer_urls))

    try:
        for record in records:
            source_name = record["source_name"]
            if source_name not in source_ids:
                existing = (
                    db.query(Source).filter(Source.name == source_name).first()
                )
                if existing is None:
                    if not args.apply:
                        source_ids[source_name] = -1
                    else:
                        existing = Source(
                            name=source_name,
                            series=profile.get("series", ""),
                            note="CLI 导入自动创建",
                        )
                        db.add(existing)
                        db.flush()
                        source_ids[source_name] = existing.id
                else:
                    source_ids[source_name] = existing.id

            if not record.get("topic"):
                print(
                    f"[import] 跳过: 第{record['chapter']}章缺少考点映射 "
                    f"({record['source_scope']} {record['number']})",
                    file=sys.stderr,
                )
                skipped += 1
                continue

            if args.apply:
                existing_question = (
                    db.query(Question)
                    .filter(
                        Question.source_id == source_ids[source_name],
                        Question.source_scope == record["source_scope"],
                        Question.source_number == record["number"],
                    )
                    .first()
                )
                if existing_question is not None:
                    # 补图回填：库内题目无图而新记录带图时更新配图与正文引用。
                    record_has_figures = "imgs/" in record["content"] or "imgs/" in (
                        record["answer_markdown"] or ""
                    )
                    if record_has_figures and not existing_question.image_paths:
                        try:
                            content, answer, urls = materialize_record(record)
                            existing_question.content = content
                            existing_question.answer_markdown = answer
                            existing_question.image_paths = (
                                normalize_upload_asset_references(
                                    urls,
                                    uploads_dir=uploads_root,
                                    url_prefix="static/uploads",
                                )
                            )
                            db.commit()
                            updated += 1
                        except Exception as exc:
                            db.rollback()
                            failed += 1
                            print(
                                f"[import] 补图失败 {record['source_scope']}({record['number']}): {exc}",
                                file=sys.stderr,
                            )
                    else:
                        skipped += 1
                    continue
                try:
                    content, answer, urls = materialize_record(record)
                    question = Question(
                        content=content,
                        question_type=record["question_type"],
                        exam_track=record["exam_track"],
                        subject=record["subject"],
                        topic=record["topic"],
                        difficulty=record["difficulty"],
                        source_id=source_ids[source_name],
                        source_number=record["number"],
                        source_scope=record["source_scope"],
                        answer_markdown=answer,
                    )
                    if urls:
                        question.image_paths = normalize_upload_asset_references(
                            urls,
                            uploads_dir=uploads_root,
                            url_prefix="static/uploads",
                        )
                    db.add(question)
                    db.flush()
                    db.add(
                        QuestionCurriculum(
                            question_id=question.id,
                            version_code=KAOYAN_VERSION,
                            exam_track=record["exam_track"],
                            subject=record["subject"],
                            topic=record["topic"],
                        )
                    )
                    db.commit()
                    created += 1
                except Exception as exc:
                    db.rollback()
                    failed += 1
                    print(
                        f"[import] 写入失败 {record['source_scope']}({record['number']}): {exc}",
                        file=sys.stderr,
                    )
            else:
                created += 1
    finally:
        db.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"[import:{mode}] 计划入库 {created}，补图更新 {updated}，"
        f"跳过 {skipped}，失败 {failed}"
    )
    if figure_stats:
        print(f"[import:{mode}] 图片处理统计: {figure_stats}")
    if not args.apply:
        print("[import:dry-run] 未写库。加 --apply 执行实际导入。")
        return 0
    if created:
        result = export_database_to_files()
        print(f"[import] 同步导出完成: {result.get('question_count')} 题")
    return 1 if failed else 0


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = build_output_dir(args.profile)
    report_path = out_dir / "build-report.txt"
    questions_path = out_dir / "questions.json"
    if report_path.is_file():
        print(report_path.read_text(encoding="utf-8"))
    profile = load_profile(args.profile)
    figure_pages = profile.get("figure_pages") or []
    if figure_pages:
        print(f"\n[report] 含插图做题本页面（需人工用 PDF 手动截图补图）: {figure_pages}")
    if questions_path.is_file():
        records = json.loads(questions_path.read_text(encoding="utf-8"))
        sampled = random.sample(records, min(args.samples, len(records)))
        print(f"\n[report] 随机抽查 {len(sampled)} 题:")
        for r in sampled:
            label = f"{r['source_name']}·{r['source_scope']}({r['number']})"
            print(f"\n===== {label} =====")
            print((r["content"][:280] + ("…" if len(r["content"]) > 280 else "")))
            answer = r.get("answer_markdown") or "(无解析)"
            print(f"--- 解析: {answer[:200]}{'…' if len(answer) > 200 else ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.import_paper_book",
        description="通用教辅/真题 PDF 导入工具（分阶段：ocr → build → import → report）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ocr = sub.add_parser("ocr", help="页面级 PaddleOCR 识别并缓存")
    ocr.add_argument("--pdf", required=True, help="PDF 文件路径")
    ocr.add_argument("--key", required=True, help="缓存分册标识（如 880-shuyi-gaoshu-zuotiben）")
    ocr.add_argument("--start", type=int, default=0, help="起始页码（0 基，默认 0）")
    ocr.add_argument("--end", type=int, default=0, help="结束页码（0 基、不含，默认到末页）")
    ocr.add_argument(
        "--stop-marker",
        default="",
        help="识别到该章节标题即停止（用于在合订解析分册中截取单科范围，如「第十章」）",
    )
    ocr.add_argument("--workers", type=int, default=OCR_CONCURRENCY, help="并发数（默认 4）")
    ocr.set_defaults(handler=cmd_ocr)

    build = sub.add_parser("build", help="解析 OCR 缓存，拆题并与解析分册匹配")
    build.add_argument("--profile", required=True, help="书册 profile JSON 路径")
    build.set_defaults(handler=cmd_build)

    imp = sub.add_parser("import", help="把构建产物导入题库（默认 dry-run）")
    imp.add_argument("--profile", required=True, help="书册 profile JSON 路径")
    imp.add_argument("--apply", action="store_true", help="实际写库（默认仅演练）")
    imp.set_defaults(handler=cmd_import)

    report = sub.add_parser("report", help="输出对账报告与随机抽查样本")
    report.add_argument("--profile", required=True, help="书册 profile JSON 路径")
    report.add_argument("--samples", type=int, default=5, help="随机抽查题目数（默认 5）")
    report.set_defaults(handler=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
