"""Central prompt builders for graduate entrance mathematics workflows."""

import json


COMMON_OCR_PROMPT = (
    "请精确识别考研数学题目图像中的全部文字、数学公式、矩阵、表格与题目来源，"
    "直接输出转录结果，不要添加前言或解释。\n"
    "【LaTeX 规范】:\n"
    "1. 仅对数学表达式使用行内 $...$ 或独立 $$...$$，不要把整段中文包进数学环境。\n"
    "2. 选择题选项使用 `\\begin{choices}`、`\\item` 和 `\\end{choices}`，保留四个选项的数学内容。\n"
    "3. 填空位置统一使用 `\\fillin`，不要保留答案或手写痕迹。\n"
    "4. 多行推导、证明步骤和小问之间使用空行分隔；矩阵、行列式、分段函数和积分上下限必须完整保留。\n"
    "5. 不确定的公式使用 `[公式待核对]` 标记，不要依据题意猜写。"
)

ILLUSTRATION_BOX_PROMPT = (
    "\n若存在几何、函数或坐标图像，请在文末追加归一化百分比包围框："
    "`[ILLUSTRATION_BOX: ymin, xmin, ymax, xmax]`（0-100 整数）；无图像时不要输出。"
)


CLASSIFICATION_PRIORITY_RULE = (
    "融合考点分类：先识别解题中实际参与推导的全部科目与考点；若有多个候选，"
    "选择位置最靠后的模块作为最终分类；先比较考试方向从上到下的顺序，"
    "若属于同一考试方向，再比较科目从前到后的顺序，最后比较考点顺序。"
    "仅作为背景而不参与解题的内容不计入分类。"
)


def build_curriculum_text(curriculum: dict) -> str:
    """Render the active exam-track -> subject -> topic tree compactly."""

    lines = []
    for exam_track, subjects in curriculum.items():
        lines.append(f"- {exam_track}:")
        if isinstance(subjects, dict):
            for subject, topics in subjects.items():
                lines.append(f"  - {subject}: {list(topics)}")
    return "\n".join(lines) + ("\n" if lines else "")


def build_classification_system_prompt(curriculum: dict) -> str:
    curriculum_text = build_curriculum_text(curriculum)
    return (
        "你是考研数学题库的分类专家。请根据题目的完整解题要求，把题目归入给定的考试方向、科目和考点。\n"
        "【可选分类树】:\n"
        f"{curriculum_text}\n"
        "【分类规则】:\n"
        "1. 必须选择分类树中的精确字符串，不要自造名称。\n"
        f"2. {CLASSIFICATION_PRIORITY_RULE}\n"
        "3. 题型只能是 `single_choice`、`fill_in_blank`、`detailed_answer` 或 `unknown`；出现 choices 环境时为 single_choice，出现 \\fillin 时为 fill_in_blank。\n"
        "4. 输出必须是严格 JSON，且只能包含 exam_track、subject、topic、question_form 四个 key。\n"
        "{\n"
        '  "exam_track": "数学一/数学二/数学三",\n'
        '  "subject": "科目名称",\n'
        '  "topic": "考点名称",\n'
        '  "question_form": "single_choice / fill_in_blank / detailed_answer / unknown"\n'
        "}\n"
        "不要输出 Markdown 代码块或任何解释文字。"
    )


def build_ai_solve_prompts(
    question_type: str,
    content: str,
    ocr_result: str = "",
    custom_prompt: str = "",
) -> tuple[str, str]:
    """Build a rigorous solution prompt scoped to the graduate exam syllabus."""

    type_mapping = {
        "single_choice": "选择题",
        "fill_in_blank": "填空题",
        "detailed_answer": "解答题",
    }
    type_str = type_mapping.get(question_type, "考研数学题")
    if question_type == "single_choice":
        first_header = r"\\textbf{【参考答案】}"
        format_rules = (
            "1. \\textbf{【参考答案】}：第一行给出唯一正确选项字母。\n"
            "2. \\textbf{【解析过程】}：逐项计算或证明，说明排除依据。\n"
            "3. \\textbf{【核心考点】}：列出使用的定义、定理和公式。"
        )
    elif question_type == "fill_in_blank":
        first_header = r"\\textbf{【参考答案】}"
        format_rules = (
            "1. \\textbf{【参考答案】}：第一行给出最终表达式或数值。\n"
            "2. \\textbf{【解析过程】}：写出关键推导与计算。\n"
            "3. \\textbf{【核心考点】}：列出使用的定义、定理和公式。"
        )
    else:
        first_header = r"\\textbf{【规范解答】}"
        format_rules = (
            "1. \\textbf{【规范解答】}：按小问写出完整演算、证明和结论，满足考研数学答题步骤要求。\n"
            "2. \\textbf{【解题思路】}：概括关键突破口、方法选择和易错点。\n"
            "3. \\textbf{【核心考点】}：列出使用的定义、定理和公式。"
        )

    system_prompt = (
        "你是一位严谨的考研数学命题与解析专家。请解答用户输入的考研数学题目。\n"
        "【解题纪律】\n"
        "1. 严禁超纲，只使用全国硕士研究生招生考试数学一/二/三大纲内的方法；若题目存在超出范围的条件，明确标注待核对，不要擅自补充。\n"
        "2. 推导必须逻辑完整、符号一致、结论明确，保留必要的定义域、收敛性、可逆性和条件判断。\n"
        f"3. 输出必须从 {first_header} 开始，不要有问候语、前言或尾注。\n"
        "【LaTeX 规范】行内公式使用 $...$，行间公式使用 $$...$$；标题使用 `\\textbf{...}`，不要使用 Markdown 双星号。\n"
        "【结构要求】\n"
        f"{format_rules}\n"
        "不同步骤、小问和自然段之间使用空行。"
    )
    user_prompt = f"题目类型: {type_str}\n"
    if ocr_result.strip():
        user_prompt += f"已有 OCR 解析草稿，请在核对后修正并完善：\n{ocr_result}\n\n"
    if custom_prompt.strip():
        user_prompt += f"补充要求: {custom_prompt}\n"
    user_prompt += f"题干内容:\n{content}"
    return system_prompt, user_prompt


def build_paper_selection_prompts(
    teacher_prompt: str,
    limit: int,
    candidates: list[dict],
    is_review_intent: bool,
) -> tuple[str, str]:
    review_hint = "优先选择 usage_count 大于 0 的真题或已复习题。" if is_review_intent else ""
    system_prompt = (
        "你是考研数学组卷专家。请依据组卷要求，从候选题中选择覆盖不同科目、考点和难度梯度的题目。\n"
        "严禁超纲或引入非考研数学知识；只使用考研数学一/二/三范围内的数学知识。\n"
        f"{review_hint}\n"
        "只返回严格 JSON：{\"selected_ids\": [1, 2], \"ai_analysis\": \"...\"}。"
    )
    user_prompt = (
        f"【组卷要求】: {teacher_prompt}\n"
        f"【题目数量】: {limit}\n"
        f"【候选题】: {json.dumps(candidates, ensure_ascii=False)}"
    )
    return system_prompt, user_prompt


def build_pdf_parse_system_prompt(curriculum: dict, generate_answers_bool: bool) -> str:
    curriculum_text = build_curriculum_text(curriculum)
    answer_rule = (
        "若原卷无答案，生成经过复核的标准解答。"
        if generate_answers_bool
        else "只提取原卷明确提供的答案；无答案时 answer_markdown 必须为空，不要现场解题。"
    )
    return (
        "你是考研数学试卷拆解专家。请把输入试卷拆成题目列表 JSON。\n"
        "【可选考试方向、科目与考点】:\n"
        f"{curriculum_text}\n"
        "【规则】\n"
        "1. 每题必须填写 exam_track、subject、topic、question_type、difficulty 和 source；分类值必须来自上方树。\n"
        "2. question_type 只能使用 single_choice / fill_in_blank / detailed_answer，difficulty 只能使用 basic / standard / comprehensive / advanced。\n"
        "3. 选择题用 choices，填空题用 \\fillin；完整保留公式（如 \\sqrt{...}、\\frac{...}{...}）、矩阵、行列式、积分、级数和图片引用。\n"
        "4. 跨页题目必须按上下文合并为同一道完整题目；页标 <!-- MATHBANK_PDF_PAGE:N --> 不是题目边界。公式待核对标记必须原样保留。\n"
        f"5. {answer_rule}\n"
        f"6. {CLASSIFICATION_PRIORITY_RULE}\n"
        "【JSON 格式】字符串换行使用 JSON 转义序列 `\\n`，LaTeX 命令的反斜杠必须按 JSON 规范转义为双反斜杠；不要输出代码块。\n"
        "{\n"
        '  "questions": [{\n'
        '    "content": "纯净题干",\n'
        '    "answer_markdown": "答案与解析",\n'
        '    "question_type": "single_choice / fill_in_blank / detailed_answer",\n'
        '    "exam_track": "数学一/数学二/数学三",\n'
        '    "subject": "科目名称",\n'
        '    "topic": "考点名称",\n'
        '    "difficulty": "basic / standard / comprehensive / advanced",\n'
        '    "source": "来源或 null",\n'
        '    "referenced_images": []\n'
        "  }]\n"
        "}"
    )


def build_import_parse_system_prompt(curriculum: dict) -> str:
    return build_pdf_parse_system_prompt(curriculum, generate_answers_bool=False) + (
        "\n【TeX 专项】忽略宏定义、页眉和排版命令，只识别题目结构；solution/answer/proof 必须挂回前一道题。"
    )


def build_latex_error_explanation_prompts(diagnostic: dict) -> tuple[str, str]:
    system_prompt = (
        "你是考研数学试卷 LaTeX 故障解释助手。只依据局部错误和源码，返回严格 JSON："
        "summary、cause、location、fixes、package、command；fixes 为 1 至 4 条短句。"
    )
    user_prompt = (
        "请解释一个 XeLaTeX 编译错误：\n"
        f"本地判断：{diagnostic.get('summary', '')}\n"
        f"技术错误：{diagnostic.get('technical_error', '')}\n"
        f"疑似命令：{diagnostic.get('command', '')}\n"
        f"疑似宏包：{diagnostic.get('package', '')}\n"
        f"位置：{diagnostic.get('location', '')}\n"
        f"源码片段：\n{diagnostic.get('source_context', '')}"
    )
    return system_prompt, user_prompt


def build_tikz_draw_prompt(latex_content: str = "", multimodal: bool = True) -> str:
    stem = latex_content or "暂无题干"
    image_hint = "第一张图是题目中的插图局部。" if multimodal else "请依据题干重建数学图形。"
    return (
        f"你是 LaTeX/TikZ 数学绘图专家。{image_hint}\n"
        f"题干：\n```latex\n{stem}\n```\n"
        "只输出可编译的 tikzpicture 代码，确保点、线、箭头、函数曲线、坐标和标注与题意一致。"
    )


def build_tikz_correction_prompt(
    tikz_code: str,
    compile_error_log: str | None = None,
    latex_content: str = "",
    user_guidance: str = "",
    multimodal: bool = False,
    rendered_comparison: bool = False,
) -> str:
    prompt = (
        "你是 LaTeX/TikZ 数学绘图修复专家。请修复下面代码的编译错误和几何逻辑问题。\n"
        f"题干：\n```latex\n{latex_content or '暂无'}\n```\n"
        f"当前代码：\n```latex\n{tikz_code}\n```\n"
        f"报错日志：\n```text\n{compile_error_log or '无编译错误日志，请重点核对图形逻辑。'}\n```\n"
        "只输出完整 tikzpicture 代码，不要解释文字。"
    )
    if rendered_comparison:
        prompt += "\n随后附有原始插图与当前渲染结果，请逐项比对并修正几何位置、标注和比例。"
    if user_guidance.strip():
        prompt += f"\n人工修改要求：{user_guidance.strip()}"
    return prompt
