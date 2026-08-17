import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from collections import OrderedDict
from io import BytesIO
from sqlalchemy.orm import Session
from mathbank.database import Question, Paper, PaperQuestion, QuestionCurriculum
from mathbank.latex_diagnostics import build_local_latex_diagnostic
from mathbank.asset_security import AssetSecurityError, resolve_upload_asset

# In-memory LRU cache for compiled PDF bytes
_PDF_CACHE_LOCK = threading.Lock()
_PDF_CACHE = OrderedDict()  # key -> (pdf_bytes, log_or_err)
_MAX_PDF_CACHE_SIZE = 20

# Only these known, offline TeX Live packages may be inserted automatically.
# A model response can never add a package to this allowlist at runtime.
_AUTO_LATEX_PACKAGES = frozenset({
    "amsmath",
    "cancel",
    "extarrows",
    "graphicx",
    "mathtools",
    "mhchem",
    "siunitx",
    "yhmath",
})
_MAX_AUTO_PACKAGE_REPAIRS = 3


def build_restricted_tex_environment(output_dir: str) -> dict[str, str]:
    """Build a minimal kpathsea policy for compiling untrusted question TeX."""

    env = os.environ.copy()
    # ``p`` forbids absolute and parent-directory input/output while keeping
    # normal TeX tree package lookup available.  TEXMFOUTPUT pins generated
    # artifacts to the per-request temporary directory.
    env["openin_any"] = "p"
    env["openout_any"] = "p"
    env["TEXMFOUTPUT"] = os.path.abspath(output_dir)
    return env

# Constants for question type labels
TYPE_LABELS = {
    "single_choice": "单项选择题",
    "fill_in_blank": "填空题",
    "detailed_answer": "解答题"
}

TYPE_ORDER = ["single_choice", "fill_in_blank", "detailed_answer"]

def clean_choice_stem_parentheses(text: str) -> str:
    """清洗选择题末尾的空括号，并保持数学定界符平衡。

    OCR 可能会把空括号识别为 ``$(\\quad)$`` 或 ``（$\\quad$）``。
    这些定界符必须和括号一起移除，否则会遗留孤立的 ``$$``
    并让 XeLaTeX 后续一直处于数学模式。
    """
    if not text:
        return ""
    text = text.strip()

    blank = r'(?:\\quad|\\qquad|\\hspace\{[^{}]*\}|[\s\xa0\u3000_])*'
    plain_parens = rf'[\(（]\s*{blank}\s*[\)）]'
    math_wrapped_parens = rf'\$\s*{plain_parens}\s*\$'
    math_inside_parens = rf'[\(（]\s*\$\s*{blank}\s*\$\s*[\)）]'
    pattern = (
        rf'(?:[\s\xa0\u3000]*(?:{math_wrapped_parens}|'
        rf'{math_inside_parens}|{plain_parens}))+[\s\xa0\u3000]*$'
    )
    cleaned = re.sub(pattern, '', text).strip()
    cleaned = re.sub(r'\\paren\b', '', cleaned).strip()

    # 仅在未闭合的数学内容后补 "$"。若末尾本身就是孤立 "$"，
    # 直接移除；追加另一个 "$" 会把它变成未闭合的行间数学定界符 "$$"。
    dollars = list(re.finditer(r'(?<!\\)\$', cleaned))
    if len(dollars) % 2 != 0:
        last_dollar = dollars[-1]
        if cleaned[last_dollar.end():].strip():
            cleaned += "$"
        else:
            cleaned = cleaned[:last_dollar.start()].rstrip()

    return cleaned

def format_stem_paragraphs(stem_text: str) -> str:
    r"""
    Safely assemble question stem lines into coherent LaTeX paragraphs.
    1. Preserves explicit double line breaks (empty lines) intended by the user.
    2. Identifies sub-question & note starts ONLY at line start (e.g. (1), (2), (a), (b), ①, ②, 注：, 提示：),
       placing each into a separate LaTeX paragraph with standard 2em indentation.
    3. Keeps block LaTeX environments (e.g. \begin{...}, \end{...}, $$) intact without inline space merging.
    4. Recombines single line breaks inside the same paragraph into a space for continuous TeX wrapping.
    """
    if not stem_text:
        return ""
        
    lines_list = stem_text.split('\n')
    paragraphs = []
    current_para = []

    for line in lines_list:
        line_str = line.strip()
        
        # 遇到显式空行，代表原作者故意留下的独立段落，直接收尾当前段落
        if not line_str:
            if current_para:
                paragraphs.append(" ".join(current_para))
                current_para = []
            continue
            
        # 判定是否为小问或独立提示/注记开头
        is_sub_start = bool(re.match(r'^([（(]?[0-9a-zA-Z一二三四五六七八九十]+[）\.\)]|①|②|③|④|⑤|【|注[:：]|提示[:：])', line_str))
        # 判定是否为独立块级 TeX 环境
        is_block_env = bool(re.match(r'^(\$\||\\begin\{|\\end\{)', line_str))
        
        if (is_sub_start or is_block_env) and current_para:
            paragraphs.append(" ".join(current_para))
            current_para = [line_str]
        else:
            current_para.append(line_str)

    if current_para:
        paragraphs.append(" ".join(current_para))

    return "\n\n".join(paragraphs)

def clean_content_for_latex(content: str, q_type: str = "", is_answer: bool = False) -> str:
    r"""
    Clean markdown/LaTeX question content for exam-zh LaTeX document export based on 试卷类模板.tex.
    Preserves math delimiters $...$ and $$...$$, tikz code, and handles choices & \paren environments.
    """
    if not content:
        return ""
    
    text = content.strip()
    
    # 如果是选择题，先清洗题干末尾残留的全角/半角供填答空括号，避免与右侧 \paren 生成括号重叠
    if q_type == "single_choice" or r"\begin{choices}" in text or re.search(r'^\s*[-*]?\s*[A-D][\.、\s]', text, re.MULTILINE):
        text = clean_choice_stem_parentheses(text)
    
    # If TikZ code is present in text, remove ANY markdown image tags ![](...) to avoid duplicate image rendering
    if r"\begin{tikzpicture}" in text:
        text = re.sub(r'!\[.*?\]\([^)]+\)', '', text)

    # Convert Markdown images ![](/static/uploads/xxx.png) or ![](uploads/xxx.png) to \includegraphics{...}
    def replace_img(match):
        img_path = match.group(1) or match.group(2)
        base_name = os.path.basename(img_path)
        return f"\n\\begin{{center}}\n\\includegraphics[max width=0.85\\linewidth]{{{base_name}}}\n\\end{{center}}\n"
    
    text = re.sub(r'!\[.*?\]\((?:/static/uploads/|static/uploads/|/uploads/|uploads/)?([^)]+)\)', replace_img, text)
    
    # Check choices formatting: if markdown list like - A. xx - B. xx, convert to \begin{choices}
    if r"\begin{choices}" not in text and re.search(r'^\s*[-*]?\s*[A-D][\.、\s]', text, re.MULTILINE):
        lines = text.split('\n')
        stem_lines = []
        choices_items = []
        for line in lines:
            m = re.match(r'^\s*[-*]?\s*([A-D])[\.、\s]+(.*)', line)
            if m:
                choices_items.append(f"    \\item {m.group(2).strip()}")
            else:
                if not choices_items:
                    stem_lines.append(line)
                else:
                    stem_lines.append(line)
        if choices_items:
            choices_block = "  \\begin{choices}\n" + "\n".join(choices_items) + "\n  \\end{choices}"
            text = "\n".join(stem_lines).strip() + "\n" + choices_block

    # For single-choice questions, ensure \paren is present before choices.
    if q_type == "single_choice":
        if r"\begin{choices}" in text:
            parts = text.split(r"\begin{choices}", 1)
            stem = clean_choice_stem_parentheses(parts[0])
            rest = r"\begin{choices}" + parts[1]
            stem += r" \paren"
            text = stem + "\n  " + rest
        else:
            stem = clean_choice_stem_parentheses(text)
            text = stem + r" \paren"

    if is_answer:
        # 解答/解析步骤（is_answer=True）：在带小问/解答步骤前自动补全空行
        text = re.sub(r'([^\n])\n(?=[（(]?[0-9一二三四五六七八九十]+[）\.\)]|解[:：]|证明[:：]|因为|所以|故|又因为|由|综上|因此|得|【)', r'\1\n\n', text)
    else:
        # 题干文本处理：安全地按行首拆分与段落重组
        text = format_stem_paragraphs(text)

    return text

def build_latex_document(
    title: str,
    subtitle: str,
    paper_type: str,
    questions_data: list,
    include_answers: bool = False,
    show_secret: bool = True,
    show_notice: bool = True
) -> str:
    """
    Generate LaTeX source code based on exam-zh document class matching 试卷类模板.tex.
    questions_data: list of dicts with keys 'question' (Question dict) and 'score' (int).
    """
    paper_title = title.strip() or "考研数学模拟试题"
    sub_title = subtitle.strip()
    
    total_q_count = len(questions_data)
    total_score_sum = sum(q.get("score", 5) for q in questions_data)
    
    lines = []
    lines.append(r"\let\stop\empty")
    # Fandol ships with TeX Live and avoids platform-specific ctex font names
    # such as STHeiti, which XeLaTeX may fail to resolve on macOS.
    lines.append(r"\documentclass[fontset=fandol]{exam-zh}")
    # Stable graduate-math baseline. Keep packages that alter core
    # math semantics (for example physics/unicode-math) out of this default.
    lines.append(r"\usepackage{amsmath,mathtools,cancel,cases,mhchem,siunitx,extarrows}")
    # exam-zh uses unicode-math, which rejects the legacy bm package. Preserve
    # imported \bm{...} formulas through the native unicode-math equivalent.
    lines.append(r"\providecommand{\bm}[1]{\symbf{#1}}")
    # Tables: standard/aligned columns, three-line tables, adaptive width,
    # long tables, merged cells, diagonal headers, colored cells and tblr.
    lines.append(r"\usepackage{array,booktabs,tabularx,longtable,multirow,makecell,diagbox,colortbl,tabularray,threeparttable}")
    lines.append(r"\UseTblrLibrary{booktabs}")
    lines.append(r"\usepackage{tkz-euclide}")
    lines.append(r"\usepackage{lastpage}")
    lines.append(r"\usepackage{caption}")
    lines.append(r"\usepackage{wrapfig}")
    lines.append(r"\usepackage{graphicx}")
    # Exported source packages keep figures in images/.  The ./ fallback keeps
    # the same source compatible with the in-app compiler's temporary layout.
    lines.append(r"\graphicspath{{images/}{./}}")
    # clean_content_for_latex emits the adjustbox-provided `max width` key for
    # Markdown images.  `export` makes that key available to \includegraphics.
    lines.append(r"\usepackage[export]{adjustbox}")
    lines.append(r"\examsetup{")
    lines.append(r"  page/size=a4paper,")
    lines.append(r"  paren/show-paren=true,")
    if include_answers:
        lines.append(r"  paren/show-answer=true,")
        lines.append(r"  fillin/show-answer=true,")
        lines.append(r"  solution/show-solution=show-stay,")
    else:
        lines.append(r"  paren/show-answer=false,")
        lines.append(r"  fillin/show-answer=false,")
    lines.append(r"  font      = times,")
    lines.append(r"  math-font = xits,")
    lines.append(r"  fillin/no-answer-type=none")
    lines.append(r"}")
    lines.append(r"\newcommand{\customcurve}[1][1]{%")
    lines.append(r"  \tikz[baseline,yshift=3,scale=0.05,thick,line width=0.8pt,")
    lines.append(r"        line cap=round, domain=-1:2.832, samples=300]{")
    lines.append(r"    \draw plot (\x,{sqrt(16/(\x+2)^2 - (\x-2)^2)});")
    lines.append(r"    \draw plot (\x,{-sqrt(16/(\x+2)^2 - (\x-2)^2)});")
    lines.append(r"    }")
    lines.append(r"}")
    lines.append(r"\everymath{\displaystyle}")
    lines.append("")
    lines.append(f"\\title{{{paper_title}}}")
    lines.append(r"\subject{数学}")
    lines.append("")
    lines.append(r"\begin{document}")
    lines.append(r"\raggedbottom")
    lines.append("")
    
    is_exam_style = paper_type == "kaoyan"
    if is_exam_style:
        if show_secret:
            lines.append(r"\secret")
        else:
            lines.append(r"% \secret")
        lines.append("")
    
    lines.append(r"\maketitle")
    
    if sub_title:
        tex_sub_title = re.sub(r' {2,}', lambda m: r'\ ' * len(m.group(0)), sub_title)
        lines.append(rf"\begin{{center}}\large\bfseries {tex_sub_title}\end{{center}}")
        
    if is_exam_style:
        lines.append(r"\begin{center}")
        lines.append(f"    本试卷共 \\pageref{{LastPage}} 页，{total_q_count} 题。全卷满分 {total_score_sum} 分。考试用时 180 分钟。")
        lines.append(r"\end{center}")
        lines.append("")
        if show_notice:
            lines.append(r"\begin{notice}")
            lines.append(r"  \item 答卷前，请按要求填写姓名、考生编号等信息。")
            lines.append(r"  \item 选择题请将所选答案填涂在答题卡相应位置；非选择题请在答题区域内作答。")
            lines.append(r"  \item 请保持答题卡整洁，考试结束后按监考人员要求交回试卷和答题卡。")
            lines.append(r"\end{notice}")
        else:
            lines.append(r"% \begin{notice}")
            lines.append(r"%   \item 答卷前，请按要求填写姓名、考生编号等信息。")
            lines.append(r"%   \item 选择题请将所选答案填涂在答题卡相应位置；非选择题请在答题区域内作答。")
            lines.append(r"%   \item 请保持答题卡整洁，考试结束后按监考人员要求交回试卷和答题卡。")
            lines.append(r"% \end{notice}")
        lines.append("")

    # Group questions by question_type
    grouped = {}
    for item in questions_data:
        q = item.get("question", {})
        q_type = q.get("question_type", "single_choice")
        if q_type not in grouped:
            grouped[q_type] = []
        grouped[q_type].append(item)
        
    for q_type in TYPE_ORDER:
        if q_type not in grouped or not grouped[q_type]:
            continue
        items = grouped[q_type]
        count = len(items)
        sec_score = sum(it.get("score", 5) for it in items)
        unit_score = items[0].get("score", 5) if count > 0 else 5
        
        if paper_type == "quiz":
            if q_type == "single_choice":
                section_header = "单选题"
            elif q_type == "fill_in_blank":
                section_header = "填空题"
            else:
                section_header = "解答题"
        else:
            if q_type == "single_choice":
                section_header = f"选择题：本题共 {count} 小题，每小题 {unit_score} 分，共 {sec_score} 分。\n  在每小题给出的四个选项中，只有一项是符合题目要求的。"
            elif q_type == "fill_in_blank":
                section_header = f"填空题：本题共 {count} 小题，每小题 {unit_score} 分，共 {sec_score} 分。"
            else:
                section_header = f"解答题：本题共 {count} 小题，共 {sec_score} 分。解答应写出文字说明、证明过程或演算步骤。"
            
        lines.append(f"\\section{{\n  {section_header}\n}}")
        lines.append("")
        
        for item in items:
            q = item.get("question", {})
            raw_content = q.get("content", "")
            q_score = item.get("score", 5)
            tikz = q.get("tikz_code", "").strip()

            fig_body = ""
            cleaned_raw = raw_content
            tikz_code = ""

            if tikz or r"\begin{tikzpicture}" in raw_content:
                tikz_code = tikz
                if not tikz_code and r"\begin{tikzpicture}" in raw_content:
                    m = re.search(r'(\\begin\{tikzpicture\}[\s\S]*?\\end\{tikzpicture\})', raw_content)
                    if m:
                        tikz_code = m.group(1)
                        cleaned_raw = cleaned_raw.replace(tikz_code, '').strip()
                
                cleaned_raw = re.sub(r'!\[.*?\]\([^)]+\)', '', cleaned_raw).strip()
            fig_elements = []
            if tikz_code:
                if r"\begin{tikzpicture}" not in tikz_code:
                    tikz_code = f"\\begin{{tikzpicture}}\n{tikz_code}\n\\end{{tikzpicture}}"
                fig_elements.append(f"\\resizebox{{4.5cm}}{{!}}{{{tikz_code}}}")

            img_matches = re.findall(r'!\[.*?\]\((?:/static/uploads/|static/uploads/|/uploads/|uploads/)?([^)]+)\)', raw_content)
            if img_matches:
                for img_path in img_matches:
                    img_filename = os.path.basename(img_path)
                    # 避免在已经输出了矢量 tikz_code 时二次重叠渲染同名生成图 tikz_xxx.png
                    if tikz_code and img_filename.startswith("tikz_"):
                        continue
                    img_w = "3.8cm" if (len(img_matches) > 1 and not (tikz_code and img_filename.startswith("tikz_"))) or (tikz_code and not img_filename.startswith("tikz_")) else "5.0cm"
                    fig_elements.append(f"\\includegraphics[width={img_w}]{{{img_filename}}}")
                cleaned_raw = re.sub(r'!\[.*?\]\([^)]+\)', '', cleaned_raw).strip()

            if fig_elements:
                fig_body = "\n\\vspace{2pt}\n".join(fig_elements)

            cleaned_content = clean_content_for_latex(cleaned_raw, q_type=q_type)
            env_name = "problem" if q_type == "detailed_answer" else "question"
            points_arg = f"[points = {q_score}]" if q_type == "detailed_answer" else ""

            default_fig_align = "bottom_right" if paper_type == "quiz" else "right"
            fig_align = q.get("figure_align")
            if not fig_align or (paper_type == "quiz" and fig_align == "right" and not q.get("custom_figure_align")):
                fig_align = default_fig_align
            # 多张插图且原设定为右侧时，默认自动优化为下方居中 (center)
            if len(fig_elements) > 1 and fig_align == "right":
                fig_align = "center"

            question_id = q.get("id")
            if question_id is not None:
                lines.append(f"% MathBank-Question-ID: {question_id}")
            lines.append(f"\\begin{{{env_name}}}{points_arg}")
            if fig_body:
                choices_part = ""
                if r"\begin{choices}" in cleaned_content:
                    parts = cleaned_content.split(r"\begin{choices}", 1)
                    stem_text = parts[0].strip()
                    choices_part = r"\begin{choices}" + parts[1]
                else:
                    stem_text = cleaned_content

                sol_space = item.get("solution_space") or q.get("solution_space") or "0.0"
                try:
                    space_val = float(sol_space)
                except Exception:
                    space_val = 0.0
                is_sol_spaced = (q_type == "detailed_answer" and not include_answers and space_val > 0)

                if fig_align == "center":
                    lines.append(stem_text)
                    lines.append(r"\begin{center}")
                    lines.append(r"  \vspace*{-0.4em}")
                    lines.append(f"  {fig_body}")
                    lines.append(r"\end{center}")
                elif fig_align == "bottom_right":
                    lines.append(stem_text)
                    lines.append(r"\begin{flushright}")
                    lines.append(r"  \vspace*{-0.4em}")
                    lines.append(f"  {fig_body}")
                    lines.append(r"\end{flushright}")
                else:  # default "right"
                    lines.append(r"\noindent\begin{minipage}[t]{\dimexpr\linewidth-5.8cm\relax}")
                    lines.append(r"  \setlength{\parindent}{2em}")
                    lines.append(r"  \hangindent=0pt")
                    lines.append(r"  \hangafter=0")
                    lines.append(f"  {stem_text}")
                    lines.append(r"\end{minipage}%")
                    lines.append(r"\hfill")
                    lines.append(r"\begin{minipage}[t]{5.2cm}")
                    lines.append(r"  \vspace{-2.0em}")
                    lines.append(r"  \raggedleft")
                    lines.append(f"  \\adjustbox{{valign=t}}{{{fig_body}}}")
                    lines.append(r"\end{minipage}")

                if choices_part:
                    lines.append(choices_part)
            else:
                lines.append(cleaned_content)
            
            # Inject solution space for detailed_answer questions on papers
            if q_type == "detailed_answer" and not include_answers:
                sol_space = item.get("solution_space") or q.get("solution_space") or "0.0"
                try:
                    space_val = float(sol_space)
                except Exception:
                    space_val = 0.0
                if space_val > 0:
                    if fig_body and fig_align in ["bottom_right", "center"]:
                        net_space = max(space_val - 3.2, 0.5)
                        lines.append(f"\\vspace*{{{net_space:.1f}cm}}")
                    else:
                        lines.append(f"\\vspace*{{{space_val:.1f}cm}}")

            lines.append(f"\\end{{{env_name}}}")

            if include_answers:
                ans_text = q.get("answer_markdown", "").strip()
                if ans_text:
                    lines.append(r"\begin{solution}")
                    lines.append(clean_content_for_latex(ans_text))
                    lines.append(r"\end{solution}")
            lines.append("")

    lines.append(r"\end{document}")
    return "\n".join(lines)

def collect_referenced_images(
    questions_data: list,
    uploads_dir: str,
    upload_url_prefix: str = "static/uploads",
) -> list:
    """
    Find all referenced image file paths in static/uploads directory.
    Returns list of absolute image file paths.
    """
    image_paths = []

    def add_reference(reference: str) -> None:
        try:
            path = resolve_upload_asset(
                reference,
                uploads_dir=uploads_dir,
                url_prefix=upload_url_prefix,
            )
        except (AssetSecurityError, TypeError):
            return
        absolute = str(path)
        if absolute not in image_paths:
            image_paths.append(absolute)

    for item in questions_data:
        q = item.get("question", {})
        imgs = q.get("image_paths", [])
        if isinstance(imgs, list):
            for img_rel in imgs:
                add_reference(img_rel)
        
        # Also parse content for Markdown image paths
        content = q.get("content", "") + " " + q.get("answer_markdown", "")
        found = re.findall(r'!\[.*?\]\((?:/static/uploads/|static/uploads/|/uploads/|uploads/)?([^)]+)\)', content)
        for fname in found:
            add_reference(fname)

    return image_paths


def _latex_failure_message(processes: list, temp_dir: str) -> str:
    """Collect the first actionable TeX error plus the final log summary."""
    log_parts = []
    for proc in processes:
        if proc is None:
            continue
        log_parts.extend((getattr(proc, "stdout", "") or "", getattr(proc, "stderr", "") or ""))
    log_path = os.path.join(temp_dir, "paper.log")
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
            log_parts.append(log_file.read())
    log_txt = "\n".join(part for part in log_parts if part)
    first_error = log_txt.find("\n!")
    if len(log_txt) <= 8000:
        diagnostic = log_txt
    elif first_error >= 0:
        diagnostic = f"{log_txt[max(0, first_error - 1000):first_error + 3000]}\n...\n{log_txt[-4000:]}"
    else:
        diagnostic = f"{log_txt[:2000]}\n...\n{log_txt[-6000:]}"
    return f"编译失败，无法生成完整 PDF:\n{diagnostic}"


def _has_latex_package(tex_content: str, package: str) -> bool:
    for match in re.finditer(r"\\(?:usepackage|RequirePackage)(?:\[[^\]]*\])?\{([^}]*)\}", tex_content):
        packages = {item.strip() for item in match.group(1).split(",")}
        if package in packages:
            return True
    return False


def _inject_latex_package(tex_content: str, package: str) -> str | None:
    """Insert an allowlisted package without changing generated source line numbers."""
    if package not in _AUTO_LATEX_PACKAGES or _has_latex_package(tex_content, package):
        return None
    document_class = re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}", tex_content)
    if not document_class:
        return None
    insertion = document_class.group(0) + rf"\usepackage{{{package}}}"
    return tex_content[:document_class.start()] + insertion + tex_content[document_class.end():]


def _clear_latex_intermediates(temp_dir: str) -> None:
    for suffix in ("aux", "log", "out", "toc", "xdv", "pdf"):
        path = os.path.join(temp_dir, f"paper.{suffix}")
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def compile_tex_to_pdf(tex_content: str, image_paths: list = None) -> tuple:
    """
    Compiles LaTeX string into PDF using system xelatex.
    Features an in-memory LRU cache based on TeX content and image mtime hashes.
    Returns (pdf_bytes, log_output_or_error).
    """
    if image_paths is None:
        image_paths = []

    # 1. Compute MD5 Cache Key from TeX content & image modification times
    img_signatures = []
    for img_p in image_paths:
        if os.path.exists(img_p):
            img_signatures.append(f"{img_p}:{os.path.getmtime(img_p)}")
    
    cache_raw_str = f"{tex_content}||{'|'.join(img_signatures)}"
    cache_key = hashlib.md5(cache_raw_str.encode("utf-8")).hexdigest()

    # 2. Check LRU Cache
    with _PDF_CACHE_LOCK:
        if cache_key in _PDF_CACHE:
            result = _PDF_CACHE.pop(cache_key)
            _PDF_CACHE[cache_key] = result
            if result[0] is not None:
                print(f"[PDF_CACHE_HIT] Reusing cached PDF. Key: {cache_key[:10]}... (Total Cached: {len(_PDF_CACHE)})", flush=True)
                return result

    # 3. Cache Miss: Perform xelatex compilation
    print(f"[PDF_CACHE_MISS] Compiling TeX via xelatex. Key: {cache_key[:10]}...", flush=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        tex_path = os.path.join(temp_dir, "paper.tex")
        # Copy images into temp_dir
        for img_p in image_paths:
            if os.path.exists(img_p):
                try:
                    shutil.copy(img_p, temp_dir)
                except Exception:
                    pass

        # Try compiling with xelatex (requires 2 passes to resolve \pageref{LastPage} and .aux references)
        try:
            cmd = [
                "xelatex",
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "-file-line-error",
                "paper.tex",
            ]
            current_tex = tex_content
            auto_loaded_packages = []
            failure_message = ""

            for repair_round in range(_MAX_AUTO_PACKAGE_REPAIRS + 1):
                _clear_latex_intermediates(temp_dir)
                with open(tex_path, "w", encoding="utf-8") as tex_file:
                    tex_file.write(current_tex)

                # Pass 1: Generate .aux file and initial layout.
                first_proc = subprocess.run(
                    cmd,
                    cwd=temp_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=build_restricted_tex_environment(temp_dir),
                )
                if first_proc.returncode != 0:
                    failure_message = _latex_failure_message([first_proc], temp_dir)
                    local = build_local_latex_diagnostic(failure_message, current_tex)
                    package = str(local.get("package") or "")
                    repaired_tex = _inject_latex_package(current_tex, package)
                    if repair_round < _MAX_AUTO_PACKAGE_REPAIRS and repaired_tex is not None:
                        current_tex = repaired_tex
                        auto_loaded_packages.append(package)
                        continue
                    if auto_loaded_packages:
                        failure_message = (
                            f"[MathBank 自动修复] 已尝试加载宏包：{', '.join(auto_loaded_packages)}。\n"
                            + failure_message
                        )
                    return (None, failure_message)

                # Pass 2: Resolve \pageref{LastPage} and cross references from .aux.
                proc = subprocess.run(
                    cmd,
                    cwd=temp_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=build_restricted_tex_environment(temp_dir),
                )
                pdf_path = os.path.join(temp_dir, "paper.pdf")
                if proc.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                    with open(pdf_path, "rb") as pf:
                        pdf_bytes = pf.read()

                    # Save to LRU Cache on success, using the user's original source key.
                    with _PDF_CACHE_LOCK:
                        if cache_key in _PDF_CACHE:
                            _PDF_CACHE.pop(cache_key)
                        _PDF_CACHE[cache_key] = (pdf_bytes, proc.stdout)
                        while len(_PDF_CACHE) > _MAX_PDF_CACHE_SIZE:
                            _PDF_CACHE.popitem(last=False)

                    return (pdf_bytes, proc.stdout)

                failure_message = _latex_failure_message([first_proc, proc], temp_dir)
                local = build_local_latex_diagnostic(failure_message, current_tex)
                package = str(local.get("package") or "")
                repaired_tex = _inject_latex_package(current_tex, package)
                if repair_round < _MAX_AUTO_PACKAGE_REPAIRS and repaired_tex is not None:
                    current_tex = repaired_tex
                    auto_loaded_packages.append(package)
                    continue
                if auto_loaded_packages:
                    failure_message = (
                        f"[MathBank 自动修复] 已尝试加载宏包：{', '.join(auto_loaded_packages)}。\n"
                        + failure_message
                    )
                return (None, failure_message)

            return (None, failure_message or "LaTeX 自动修复后仍未能生成 PDF。")
        except FileNotFoundError:
            return (None, "系统未检测到 xelatex 编译器，请确保已安装 TeX Live / MiKTeX / MacTeX 并加入 PATH。")
        except subprocess.TimeoutExpired:
            return (None, "LaTeX 编译超时（超过 30 秒）。")
        except Exception as e:
            return (None, f"编译过程异常: {str(e)}")

def extract_figures_for_answer_sheet(content: str) -> str:
    """Extract TikZ code or images from question content for answer sheet embedding."""
    if not content:
        return ""
    figs = []
    # 1. TikZ blocks
    tikz_blocks = re.findall(r'(\\begin\{tikzpicture\}[\s\S]*?\\end\{tikzpicture\})', content)
    for t in tikz_blocks:
        figs.append(t)
    # 2. Markdown image ![...](path)
    img_matches = re.findall(r'!\[.*?\]\((?:/static/uploads/|static/uploads/|/uploads/|uploads/)?([^)]+)\)', content)
    for img_name in img_matches:
        base_n = os.path.basename(img_name)
        if tikz_blocks and base_n.startswith("tikz_"):
            continue
        img_tag = f"\\includegraphics[max width=3.4cm]{{{base_n}}}"
        if img_tag not in figs:
            figs.append(img_tag)
    # 3. Direct \includegraphics
    direct_imgs = re.findall(r'(\\includegraphics(?:\[.*?\])?\{.*?\})', content)
    for d in direct_imgs:
        if d not in figs:
            figs.append(d)
    return "\n".join(figs)

def clear_pdf_cache():
    """Clear all entries in PDF compilation LRU cache."""
    with _PDF_CACHE_LOCK:
        _PDF_CACHE.clear()
        print("[PDF_CACHE] PDF LRU memory cache cleared.", flush=True)

def build_answer_sheet_latex(title: str, subtitle: str, questions_data: list) -> str:
    """Generate a clean graduate-math answer sheet for the selected questions."""
    groups = {
        "single_choice": [],
        "fill_in_blank": [],
        "detailed_answer": [],
    }
    for item in questions_data:
        question = item.get("question", {}) or {}
        groups.setdefault(question.get("question_type", "single_choice"), []).append(item)

    paper_title = title.strip() or "考研数学模拟试题"
    lines = [
        r"\documentclass[UTF8,fontset=fandol,11pt]{ctexart}",
        r"\usepackage[a4paper,margin=1.8cm]{geometry}",
        r"\usepackage{amsmath,amssymb,graphicx,enumitem}",
        r"\usepackage[export]{adjustbox}",
        r"\graphicspath{{images/}{./}}",
        r"\usepackage{fancyhdr}",
        r"\pagestyle{fancy}",
        r"\fancyhf{}",
        r"\rhead{考研数学答题卡}",
        r"\cfoot{\thepage}",
        r"\setlength{\parindent}{0pt}",
        r"\setlength{\parskip}{0.45em}",
        r"\begin{document}",
        r"\begin{center}",
        rf"\LARGE\bfseries {paper_title}",
        r"\par",
        r"\large\bfseries 考研数学答题卡",
        r"\par",
        rf"{subtitle.strip()}",
        r"\end{center}",
        r"\noindent 姓名：\underline{\hspace{4cm}}\quad 考生编号：\underline{\hspace{6cm}}",
        r"\par\medskip",
    ]

    if groups["single_choice"]:
        lines.extend([
            r"\section*{一、选择题}",
            r"请将每题唯一正确选项填入答题卡相应位置。",
        ])
        for number, _item in enumerate(groups["single_choice"], 1):
            lines.append(
                rf"\noindent {number}.\quad A\ \underline{{\hspace{{0.7cm}}}}\quad "
                rf"B\ \underline{{\hspace{{0.7cm}}}}\quad C\ \underline{{\hspace{{0.7cm}}}}\quad "
                rf"D\ \underline{{\hspace{{0.7cm}}}}"
            )

    if groups["fill_in_blank"]:
        lines.extend([
            r"\section*{二、填空题}",
            r"请将每题答案填写在横线上。",
        ])
        for number, _item in enumerate(groups["fill_in_blank"], 1):
            lines.append(rf"\noindent {number}.\quad \underline{{\hspace{{10cm}}}}")

    if groups["detailed_answer"]:
        lines.extend([
            r"\section*{三、解答题}",
            r"请写出必要的文字说明、证明过程或演算步骤。",
        ])
        for number, item in enumerate(groups["detailed_answer"], 1):
            score = item.get("score", 0)
            lines.append(rf"\noindent {number}.\quad（{score}分）")
            figure_code = extract_figures_for_answer_sheet(
                (item.get("question", {}) or {}).get("content", "")
            )
            if figure_code:
                lines.append(r"\begin{center}")
                lines.append(rf"\resizebox{{0.35\linewidth}}{{!}}{{{figure_code}}}")
                lines.append(r"\end{center}")
            try:
                height = max(2.0, min(12.0, float(item.get("solution_space") or 4.0)))
            except (TypeError, ValueError):
                height = 4.0
            lines.append(rf"\vspace*{{{height:.1f}cm}}")
            lines.append(r"\hrule")
            lines.append(r"\medskip")

    lines.append(r"\end{document}")
    return "\n".join(lines) + "\n"


def create_tex_zip_package(title: str, tex_content: str, ans_tex_content: str, image_paths: list, answer_sheet_tex: str = None) -> bytes:
    """
    Creates an in-memory ZIP package containing paper.tex, answer.tex, answer_sheet.tex (if present), and referenced images.
    """
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("试卷正文.tex", tex_content.encode("utf-8"))
        if ans_tex_content:
            zf.writestr("参考答案与解析.tex", ans_tex_content.encode("utf-8"))
        if answer_sheet_tex:
            zf.writestr("答题卡.tex", answer_sheet_tex.encode("utf-8"))
            
        for img_p in image_paths:
            if os.path.exists(img_p):
                fname = os.path.basename(img_p)
                zf.write(img_p, arcname=f"images/{fname}")

    return zip_buffer.getvalue()

def create_full_bundle_zip_package(
    title: str,
    tex_content: str,
    ans_tex_content: str,
    image_paths: list,
    answer_sheet_tex: str = None,
    main_pdf_bytes: bytes = None,
    ans_pdf_bytes: bytes = None,
    answer_sheet_pdf_bytes: bytes = None
) -> bytes:
    """
    Creates an in-memory ZIP package containing LaTeX TeX files, compiled PDF files, and referenced images.
    """
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. TeX files
        zf.writestr("试卷正文.tex", tex_content.encode("utf-8"))
        if ans_tex_content:
            zf.writestr("参考答案与解析.tex", ans_tex_content.encode("utf-8"))
        if answer_sheet_tex:
            zf.writestr("答题卡.tex", answer_sheet_tex.encode("utf-8"))
            
        # 2. PDF files
        if main_pdf_bytes:
            zf.writestr("试卷正文.pdf", main_pdf_bytes)
        if ans_pdf_bytes:
            zf.writestr("参考答案与解析.pdf", ans_pdf_bytes)
        if answer_sheet_pdf_bytes:
            zf.writestr("答题卡.pdf", answer_sheet_pdf_bytes)
            
        # 3. Referenced images
        for img_p in image_paths:
            if os.path.exists(img_p):
                fname = os.path.basename(img_p)
                zf.write(img_p, arcname=f"images/{fname}")

    return zip_buffer.getvalue()
