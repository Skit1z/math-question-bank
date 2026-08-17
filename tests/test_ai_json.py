import json
from typing import get_type_hints

import pytest

from mathbank.ai_json import parse_ai_json
from mathbank.prompts import (
    build_classification_system_prompt,
    build_import_parse_system_prompt,
    build_pdf_parse_system_prompt,
)


def test_parse_ai_json_type_hints_resolve():
    hints = get_type_hints(parse_ai_json)

    assert "raw_markdown" in hints


def test_parse_ai_json_keeps_valid_json_unchanged():
    original = {
        "questions": [
            {
                "content": "第一行\n第二行 $\\frac{1}{2}$",
                "answer_markdown": "",
            }
        ]
    }

    assert parse_ai_json(json.dumps(original, ensure_ascii=False)) == original


def test_parse_ai_json_accepts_json_null():
    assert parse_ai_json("null") is None


def test_parse_ai_json_removes_markdown_fence_and_prose():
    raw = """```json
以下是结果：
{"questions": []}
```"""

    assert parse_ai_json(raw) == {"questions": []}


def test_parse_ai_json_repairs_literal_controls_and_latex_backslashes():
    raw = r"""{
  "questions": [
    {
      "content": "第一行
第二行 $\frac{1}{2}$ 与 \textbf{重点}<TAB>完成",
      "answer_markdown": ""
    }
  ]
}""".replace("<TAB>", "\t")

    parsed = parse_ai_json(raw)

    content = parsed["questions"][0]["content"]
    assert content == "第一行\n第二行 $\\frac{1}{2}$ 与 \\textbf{重点}\t完成"


def test_parse_ai_json_preserves_latex_that_looks_like_valid_json_escape():
    raw = r'{"content":"\\textbf{重点} 与 \\nabla f"}'

    parsed = parse_ai_json(raw)

    assert parsed["content"] == "\\textbf{重点} 与 \\nabla f"


def test_parse_ai_json_rejects_structurally_invalid_output():
    with pytest.raises(json.JSONDecodeError):
        parse_ai_json('{"questions": nope}')


def test_paper_prompts_require_valid_json_escaping():
    curriculum = {"数学一": {"高等数学": ["函数、极限与连续"]}}
    prompts = (
        build_pdf_parse_system_prompt(curriculum, False),
        build_import_parse_system_prompt(curriculum),
    )

    for prompt in prompts:
        assert "JSON 转义序列 `\\n`" in prompt
        assert "LaTeX 命令的反斜杠必须按 JSON 规范转义为双反斜杠" in prompt
        assert "字符串内部换行直接输出真实回车" not in prompt


def test_classification_prompts_prefer_later_curriculum_module():
    curriculum = {
        "数学一": {"高等数学": ["函数、极限与连续"]},
        "数学二": {"线性代数": ["矩阵"]},
    }
    prompts = (
        build_classification_system_prompt(curriculum),
        build_pdf_parse_system_prompt(curriculum, False),
        build_import_parse_system_prompt(curriculum),
    )

    for prompt in prompts:
        assert "选择位置最靠后的模块作为最终分类" in prompt
        assert "先比较考试方向从上到下的顺序" in prompt
        assert "若属于同一考试方向，再比较科目从前到后的顺序" in prompt
        assert "数学二" in prompt
        assert "线性代数" in prompt
        assert "仅作为背景而不参与解题" in prompt


def test_single_question_classification_prompt_requests_only_coarse_question_form():
    prompt = build_classification_system_prompt({"数学一": {"高等数学": ["函数、极限与连续"]}})

    assert '"exam_track"' in prompt
    assert '"subject"' in prompt
    assert '"topic"' in prompt
    assert '"question_form"' in prompt
    assert "question_type" not in prompt
    assert "只能包含 exam_track、subject、topic、question_form 四个 key" in prompt
    assert "single_choice" in prompt
    assert "multi_choice" not in prompt
