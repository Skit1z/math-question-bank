"""教辅导入 CLI（scripts/import_paper_book）解析逻辑的单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.import_paper_book import (
    BookSectionParser,
    build_choices_env,
    cn_chapter_to_int,
    compose_question_record,
    convert_figure_markup,
    materialize_figures,
    normalize_fillin_macro,
    page_cache_complete,
    referenced_images,
    split_choice_options,
    strip_empty_paren_tail,
    OCR_CACHE_DIR,
)

PROFILE = {
    "exam_track": "数学一",
    "subject": "高等数学",
    "blocks": {
        "基础题": {"source": "880基础篇", "difficulty": "basic"},
        "综合题": {"source": "880综合篇", "difficulty": "comprehensive"},
        "拓展题": {"source": "880拓展篇", "difficulty": "advanced"},
    },
    "question_types": {
        "选择题": "single_choice",
        "填空题": "fill_in_blank",
        "解答题": "detailed_answer",
    },
    "chapter_topics": {"1": "函数、极限与连续", "2": "一元函数微分学"},
}


def feed(parser, page, text):
    for line in text.splitlines():
        parser.feed(page, line)


def test_chinese_chapter_numbers():
    assert cn_chapter_to_int("一") == 1
    assert cn_chapter_to_int("九") == 9
    assert cn_chapter_to_int("十") == 10
    assert cn_chapter_to_int("十二") == 12
    assert cn_chapter_to_int("二十") == 20


def test_parser_tracks_sections_and_numbering():
    parser = BookSectionParser()
    feed(
        parser,
        3,
        """
# 第一章 函数、极限、连续

## 基础题

## 一、 选择题

(1) 函数 $ f(x)=|x\\sin x|e^{\\cos x} $，是 ( ).

A. 单调函数 B. 周期函数 C. 偶函数 D. 有界函数

(2) 设函数 $ f(x)=\\cos(\\sin x) $，则 ( )。

A. 增 B. 减 C. 增且减 D. 不增不减

## 二、 填空题

(1) 当 $ x \\to 0 $ 时，$ a= $ ___.

## 三、 解答题

(1) 求下列极限:

(I) $ \\lim_{x \\to 0} \\frac{x^2-1}{x} $;

(II) $ \\lim_{x \\to 1} \\frac{x^2-1}{x-1} $.

这是一条为了防止被小坏蛋拿去转卖抹掉的水印，发出来的资料都是免费获取(这里 https://nocode.host/hjr2pw)

公众号：做题本集结地整理
""",
    )

    assert len(parser.items) == 4
    first = parser.items[0]
    assert (first["chapter"], first["block"], first["qtype"], first["number"]) == (
        1, "基础题", "选择题", 1,
    )
    fill = parser.items[2]
    assert (fill["qtype"], fill["number"]) == ("填空题", 1)
    detailed = parser.items[3]
    assert detailed["number"] == 1
    body = "\n".join(detailed["body_lines"])
    assert "(I)" in body and "(II)" in body  # 罗马子项保留在题干内
    assert "小坏蛋" not in body and "做题本集结地" not in body  # 水印被过滤


def test_parser_flags_number_regression_instead_of_splitting():
    parser = BookSectionParser()
    feed(
        parser,
        5,
        """
# 第二章 一元函数微分学

## 综合题

## 三、 解答题

(5) 证明不等式 $ e^x > 1 + x $.

(2) 讨论函数单调性.
""",
    )
    # (2) 是 (5) 的疑似子项/编号回退，不应拆成新题
    assert len(parser.items) == 1
    assert any("编号回退" in flag for flag in parser.items[0]["flags"])


def test_split_choice_options_inline_and_multiline():
    options, stem = split_choice_options(
        "设 $ f(x) $ 单调，则 ( ).\nA. 单调函数 B. 周期函数 C. 偶函数 D. 有界函数"
    )
    assert options == ["单调函数", "周期函数", "偶函数", "有界函数"]
    assert stem.endswith("则 ( ).")

    options2, stem2 = split_choice_options(
        "题干（ ）\nA.  $ a=2 $\n\nB.  $ a=1 $\n\nC.  $ a=3 $\n\nD.  $ a=4 $"
    )
    assert options2 is not None and len(options2) == 4
    assert options2[0].strip() == "$ a=2 $"

    # 无法构成 ABCD 序列时返回 None 并保留原文
    options3, stem3 = split_choice_options("纯题干没有选项")
    assert options3 is None


def test_build_choices_env_and_stem_cleanup():
    env = build_choices_env(["甲", "乙", "丙", "丁"])
    assert env.startswith("\\begin{choices}")
    assert "\\item 甲" in env
    assert env.endswith("\\end{choices}")

    assert strip_empty_paren_tail("函数 $ f(x) $ 是 ( ).") == "函数 $ f(x) $ 是"
    assert strip_empty_paren_tail("则（）。") == "则"
    assert strip_empty_paren_tail("区间 $ (0,1) $") == "区间 $ (0,1) $"  # 非空括号保留


def test_normalize_fillin_macro_matches_backend_rules():
    assert normalize_fillin_macro("则 $ a= $ ___") == "则 $ a= $ \\fillin"
    assert normalize_fillin_macro("\\fillin[2cm]") == "\\fillin"
    assert normalize_fillin_macro("\\underline{xx}") == "\\fillin"


def test_compose_question_record_full_flow():
    parser = BookSectionParser()
    feed(parser, 3, "# 第一章 函数、极限、连续\n\n## 基础题\n\n## 一、 选择题\n")
    feed(parser, 3, "(1) 函数 $ f(x) $ 是 ( ).\nA. 甲 B. 乙 C. 丙 D. 丁")

    solution = {
        "chapter": 1,
        "block": "基础题",
        "qtype": "选择题",
        "number": 1,
        "body_lines": ["C.", "解 因为 $ f(-x)=f(x) $，故为偶函数."],
        "pages": [7],
        "flags": [],
    }
    record = compose_question_record(parser.items[0], PROFILE, solution)

    assert record["question_type"] == "single_choice"
    assert record["difficulty"] == "basic"
    assert record["source_name"] == "880基础篇"
    assert record["source_scope"] == "第一章·选择"
    assert record["number"] == 1
    assert record["topic"] == "函数、极限与连续"
    assert "\\begin{choices}" in record["content"]
    assert "( )." not in record["content"]
    assert record["answer_markdown"].startswith("**答案** C")
    assert record["solution_pages"] == [7]
    assert record["solution_found"] is True

    missing = compose_question_record(parser.items[0], PROFILE, None)
    assert missing["solution_found"] is False
    assert "无匹配解析" in missing["flags"]


def test_convert_figure_markup_replaces_html_wrappers():
    html = '<div style="text-align: center;"><img src="imgs/img_in_image_box_911_300_1086_465.jpg" alt="Image" width="14%" /></div>'
    assert (
        convert_figure_markup(html)
        == "![题目插图](imgs/img_in_image_box_911_300_1086_465.jpg)"
    )
    bare = '<img src="imgs/fig_2.jpg" />'
    assert convert_figure_markup(bare) == "![题目插图](imgs/fig_2.jpg)"
    assert convert_figure_markup("无图文本 $ (x) $") == "无图文本 $ (x) $"


def test_referenced_images_and_cache_completeness(tmp_path, monkeypatch):
    cache_root = tmp_path / "ocr"
    monkeypatch.setattr("scripts.import_paper_book.OCR_CACHE_DIR", cache_root)
    text = "题干\n\n![题目插图](imgs/fig_a.jpg)\n\n![题目插图](imgs/fig_b.png)"
    assert referenced_images(text) == ["imgs/fig_a.jpg", "imgs/fig_b.png"]
    # 空白页（空 md）视为完整缓存，避免每次重跑都重识别
    assert page_cache_complete("book1", 3, "") is True
    # 引用的图缺一张都不算缓存命中
    asset = cache_root / "book1" / "assets" / "p0003" / "fig_a.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"x")
    assert page_cache_complete("book1", 3, text) is False
    (cache_root / "book1" / "assets" / "p0003" / "fig_b.png").write_bytes(b"y")
    assert page_cache_complete("book1", 3, text) is True


def _tiny_png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_materialize_figures_writes_uploads_and_rewrites_refs(tmp_path, monkeypatch):
    cache_root = tmp_path / "ocr"
    uploads = tmp_path / "uploads"
    monkeypatch.setattr("scripts.import_paper_book.OCR_CACHE_DIR", cache_root)
    asset = cache_root / "book1" / "assets" / "p0003" / "fig_a.jpg"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(_tiny_png_bytes())

    stats: dict[str, int] = {}
    text, urls = materialize_figures(
        "题干\n\n![题目插图](imgs/fig_a.jpg)\n\n尾部",
        cache_key="book1",
        pages=[3],
        name_prefix="mb880-c2-b-q14-c",
        uploads_root=uploads,
        stats=stats,
    )
    assert urls == ["/static/uploads/mb880-c2-b-q14-c-0.png"]
    assert "![题目插图](/static/uploads/mb880-c2-b-q14-c-0.png)" in text
    assert (uploads / "mb880-c2-b-q14-c-0.png").is_file()
    assert stats == {}

    # 缺失的引用保持原样并计数
    text2, urls2 = materialize_figures(
        "![题目插图](imgs/gone.jpg)",
        cache_key="book1",
        pages=[9],
        name_prefix="mb880-c2-b-q15-c",
        uploads_root=uploads,
        stats=stats,
    )
    assert urls2 == []
    assert "imgs/gone.jpg" in text2
    assert stats == {"figures_missing": 1}
