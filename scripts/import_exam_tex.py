"""考研数学真题 LaTeX 源（kysx）导入 CLI。

数据源：``docs/kysx/year/<年>/<年>P<卷>.tex``（P1/P2/P3 = 数学一/二/三），
结构化 ``problem`` 环境 + ``makepart`` 分节，原生 LaTeX 零 OCR 损耗。
解析来自 ``docs/03.*真题详解`` 的 OCR 缓存（由 import_paper_book ocr 生成）。

用法（从项目根目录运行）::

    python3 -m scripts.import_exam_tex build [--start-year 1987] [--end-year 2009]
    python3 -m scripts.import_exam_tex import [--apply]
    python3 -m scripts.import_exam_tex papers [--apply]
    python3 -m scripts.import_exam_tex match
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from mathbank.paths import PROJECT_ROOT, SYSTEM_GENERATED_DIR, UPLOADS_DIR

KYSX_DIR = PROJECT_ROOT / "docs" / "kysx"
GRAPHICS_DIR = KYSX_DIR / "graphics"
BUILD_DIR = SYSTEM_GENERATED_DIR / "book_import" / "build"
OCR_CACHE_DIR = SYSTEM_GENERATED_DIR / "book_import" / "ocr"

TRACK_BY_FILE = {"P1": ("数学一", "数一"), "P2": ("数学二", "数二"), "P3": ("数学三", "数三")}

MAKEPART_RE = re.compile(r"^\\makepart\{([^}]*)\}\{([^}]*)\}")
PROBLEM_BEGIN_RE = re.compile(r"^\\begin\{problem\}(?:\[points=(\d+)\])?")
ABCDBEGIN_RE = re.compile(r"^\\begin\{abcd\*?\}")
INCLUDEGRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
FILLIN_RE = re.compile(r"\\fillin\s*\{")
SCORE_PER_RE = re.compile(r"每小题\s*(\d+)\s*分")
SCORE_TOTAL_RE = re.compile(r"本题满分\s*(\d+)\s*分")

QTYPE_MAP = {
    "填空题": "fill_in_blank",
    "选择题": "single_choice",
    "判断题": "single_choice",
    "解答题": "detailed_answer",
    "计算题": "detailed_answer",
    "证明题": "detailed_answer",
    "": "detailed_answer",
}

# kysx/jnuexam 数学宏 → 系统等价 LaTeX（KaTeX 兼容）。
MACRO_TABLE: list[tuple[str, str]] = [
    (r"\\e\b", r"\\mathrm{e}"),
    (r"\\R\b", r"\\mathbb{R}"),
    (r"\\diff\b", r"\\mathrm{d}"),
    (r"\\dx\b", r"\\mathrm{d}x"),
    (r"\\dy\b", r"\\mathrm{d}y"),
    (r"\\dz\b", r"\\mathrm{d}z"),
    (r"\\du\b", r"\\mathrm{d}u"),
    (r"\\dv\b", r"\\mathrm{d}v"),
    (r"\\dr\b", r"\\mathrm{d}r"),
    (r"\\ds\b", r"\\mathrm{d}s"),
    (r"\\dt\b", r"\\mathrm{d}t"),
    (r"\\dS\b", r"\\mathrm{d}S"),
    (r"\\d\s+(?=[a-zA-Z])", r"\\mathrm{d} "),
    # 通用偏导宏（如 \pd^2 z）必须先规范为 KaTeX 可识别的 \partial。
    (r"\\pd\b", r"\\partial"),
    (r"\\pdf\b", r"\\partial f"),
    (r"\\pdg\b", r"\\partial g"),
    (r"\\pdh\b", r"\\partial h"),
    (r"\\pdl\b", r"\\partial l"),
    (r"\\pdn\b", r"\\partial n"),
    (r"\\pdu\b", r"\\partial u"),
    (r"\\pdv\b", r"\\partial v"),
    (r"\\pdx\b", r"\\partial x"),
    (r"\\pdy\b", r"\\partial y"),
    (r"\\pdz\b", r"\\partial z"),
    (r"\\pdF\b", r"\\partial F"),
    (r"\\pdL\b", r"\\partial L"),
    (r"\\pdP\b", r"\\partial P"),
    (r"\\pdQ\b", r"\\partial Q"),
    (r"\\pdR\b", r"\\partial R"),
    (r"\\va\b", r"\\vec{a}"),
    (r"\\vb\b", r"\\vec{b}"),
    (r"\\vc\b", r"\\vec{c}"),
    (r"\\vd\b", r"\\vec{d}"),
    (r"\\ve\b", r"\\vec{e}"),
    (r"\\vi\b", r"\\vec{i}"),
    (r"\\vj\b", r"\\vec{j}"),
    (r"\\vk\b", r"\\vec{k}"),
    (r"\\vn\b", r"\\vec{n}"),
    (r"\\vr\b", r"\\vec{r}"),
    (r"\\vu\b", r"\\vec{u}"),
    (r"\\vv\b", r"\\vec{v}"),
    (r"\\vw\b", r"\\vec{w}"),
    (r"\\vx\b", r"\\vec{x}"),
    (r"\\vy\b", r"\\vec{y}"),
    (r"\\vz\b", r"\\vec{z}"),
    (r"\\Int\b", r"\\int"),
    (r"\\widebar\b", r"\\bar"),
    (r"\\division\b", r"\\div"),
    (r"\\div\b", r"\\operatorname{div}"),
    (r"\\arccot\b", r"\\operatorname{arccot}"),
    (r"\\Corr\b", r"\\operatorname{\\rho}"),
    (r"\\Cov\b", r"\\operatorname{Cov}"),
    (r"\\diag\b", r"\\operatorname{diag}"),
    (r"\\grad\b", r"\\operatorname{grad}"),
    (r"\\Prj\b", r"\\operatorname{Prj}"),
    (r"\\tr\b", r"\\operatorname{tr}"),
    (r"\\Var\b", r"\\operatorname{Var}"),
    (r"\\limit\b", r"\\lim\\limits"),
    (r"\\smash\[t\]", ""),
    (r"\\smash\[b\]", ""),
    (r"\\smash\b", ""),
    (r"\\cdotfill\b", ""),
    # 不能用 \\par\s*：它会匹配 \\partial 的前缀并破坏偏导符号。
    (r"\\par(?![A-Za-z])\s*", "\n"),
    (r"\\centerline\{", "{"),
    (r"\\nobreak\b|\\unskip\b|\\leavevmode\b", ""),
]


def read_braced(text: str, start: int) -> tuple[str, int]:
    """返回 text[start] 处 ``{`` 所括起的完整内容（处理嵌套）。"""

    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{" and text[index - 1] != "\\":
            depth += 1
        elif text[index] == "}" and text[index - 1] != "\\":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    raise ValueError("花括号不匹配")


def sanitize_latex(text: str) -> str:
    for pattern, replacement in MACRO_TABLE:
        text = re.sub(pattern, replacement, text)
    return text


def strip_answer_macros(text: str) -> tuple[str, str]:
    """清除 ``\\fillin{答案}``/``\\pickout{}``/``\\tickout{}``，返回 (正文, 填空答案)。"""

    answers: list[str] = []
    index = 0
    while True:
        match = FILLIN_RE.search(text, index)
        if not match:
            break
        param, end = read_braced(text, match.end() - 1)
        param = param.strip()
        if param:
            answers.append(param)
        text = text[: match.start()] + "\\fillin" + text[end:]
        index = match.start() + len("\\fillin")
    text = re.sub(r"\\pickout\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\tickout\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\\tickin\s*\{[^{}]*\}", "", text)
    return text, "；".join(answers)


def parse_exam_tex(tex: str) -> list[dict]:
    """把一份真题 TeX 解析为有序事件流（part/own/use）。

    own 事件携带题干与可选 points；use 事件是对 ``loadproblems`` 载入卷的
    ``(载入序号, 大题序号, 题序号)`` 引用，由 resolve_paper 统一组装。
    """

    events: list[dict] = []
    current_qtype = "detailed_answer"
    per_score = None
    part_total = None
    part_count = 0
    part_index = 0
    in_problem = False
    body_lines: list[str] = []
    points_attr = None

    for raw_line in tex.splitlines():
        line = raw_line.rstrip()

        load_match = re.match(r"^\\loadproblems\{(\d+)\}\{([^}]+)\}", line.strip())
        if load_match and not in_problem:
            events.append({"kind": "load", "slot": int(load_match.group(1)), "file": load_match.group(2).strip()})
            continue

        use_match = re.match(r"^\\useproblem(?:\[([^\]]*)\])?\{(\d+)\}\{(\d+)\}\{(\d+)\}", line.strip())
        if use_match and not in_problem:
            part_count += 1
            use_points = None
            if use_match.group(1) and "points=" in use_match.group(1):
                point_match = re.search(r"points=(\d+)", use_match.group(1))
                if point_match:
                    use_points = int(point_match.group(1))
            events.append({
                "kind": "use",
                "slot": int(use_match.group(2)),
                "part": int(use_match.group(3)),
                "q": int(use_match.group(4)),
                "use_points": use_points,
            })
            continue

        part_match = MAKEPART_RE.match(line.strip())
        if part_match and not in_problem:
            current_qtype = QTYPE_MAP.get(part_match.group(1).strip(), "detailed_answer")
            note = part_match.group(2)
            per_match = SCORE_PER_RE.search(note)
            total_match = SCORE_TOTAL_RE.search(note)
            per_score = int(per_match.group(1)) if per_match else None
            part_total = int(total_match.group(1)) if total_match else None
            part_count = 0
            part_index += 1
            events.append({
                "kind": "part",
                "index": part_index,
                "qtype": current_qtype,
                "per_score": per_score,
                "part_total": part_total,
            })
            continue

        problem_match = PROBLEM_BEGIN_RE.match(line.strip())
        if problem_match:
            in_problem = True
            body_lines = []
            points_attr = int(problem_match.group(1)) if problem_match.group(1) else None
            continue
        if re.match(r"^\\end\{problem\}", line.strip()):
            part_count += 1
            events.append({
                "kind": "own",
                "part": part_index,
                "part_count": part_count,
                "qtype": current_qtype,
                "per_score": per_score,
                "part_total": part_total,
                "points": points_attr,
                "body": "\n".join(body_lines).strip(),
            })
            in_problem = False
            continue
        if in_problem:
            body_lines.append(line)

    return events


_PAPER_CACHE: dict[tuple[int, str], dict] = {}


def resolve_paper(year: int, suffix: str) -> dict:
    """解析一份卷（含跨卷复用引用），返回 {events, own_index}。

    ``own_index`` 的键与模板存储一致：``<2004`` 年为大题内题号，
    ``>=2004`` 年为全卷连续题号（载入卷里的复用引用同样参与计数）。
    """

    cache_key = (year, suffix)
    if cache_key in _PAPER_CACHE:
        return _PAPER_CACHE[cache_key]
    tex_path = KYSX_DIR / "year" / str(year) / f"{year}{suffix}.tex"
    events = parse_exam_tex(tex_path.read_text(encoding="utf-8"))
    own_index: dict[tuple[int, int], dict] = {}
    q_total = 0
    q_in_part = 0
    for event in events:
        if event["kind"] == "part":
            q_in_part = 0
        elif event["kind"] in ("own", "use"):
            q_total += 1
            q_in_part += 1
            if event["kind"] == "own":
                event["q_in_part"] = q_in_part
                event["q_total"] = q_total
                key = (
                    (event["part"], q_in_part)
                    if year < 2004
                    else (event["part"], q_total)
                )
                own_index[key] = event
    resolved = {"events": events, "own_index": own_index, "file": f"{year}{suffix}"}
    _PAPER_CACHE[cache_key] = resolved
    return resolved


def resolve_use(year: int, slot_file: str, part: int, q: int) -> dict:
    """取 (载入卷, 大题号, 题号) 对应的原生题目事件（递归追踪复用链）。"""

    suffix = slot_file[-2:]
    paper = resolve_paper(year, suffix)
    if q == 0:
        # 模板约定题号为 0 表示该大题第 1 题。
        q = 1
    event = paper["own_index"].get((part, q))
    if event is None:
        raise ValueError(f"复用引用不存在: {year}{slot_file} 第{part}大题第{q}题")
    return event


def extract_options(body: str) -> tuple[str | None, str]:
    """把 ``abcd`` 环境转换为系统 ``choices`` 环境，返回 (选项行或 None, 题干)。"""

    env_match = re.search(r"\\begin\{abcd\*?\}([\s\S]*?)\\end\{abcd\*?\}", body)
    if not env_match:
        return None, body
    inner = env_match.group(1).strip()
    options = [opt.strip() for opt in re.split(r"\\item\b", inner) if opt.strip()]
    stem = (body[: env_match.start()] + body[env_match.end():]).strip()
    if not options or len(options) < 2:
        return None, body
    choices = "\\begin{choices}\n" + "\n".join(
        f"\\item {opt}" for opt in options
    ) + "\n\\end{choices}"
    return choices, stem


def materialize_graphics(
    text: str, name_prefix: str, stats: dict[str, int]
) -> tuple[str, list[str]]:
    """把 ``\\includegraphics`` 引用复制进 uploads 并替换为系统 Markdown 图。"""

    from mathbank.asset_security import (
        InvalidImageError,
        normalize_raster_image,
        normalize_upload_asset_references,
    )

    urls: list[str] = []

    def _replace(match: re.Match) -> str:
        ref = match.group(1).strip()
        source = GRAPHICS_DIR / (Path(ref).name + ".png")
        if not source.is_file():
            for candidate in GRAPHICS_DIR.glob(Path(ref).name + ".*"):
                if candidate.is_file():
                    source = candidate
                    break
        if not source.is_file():
            stats["graphics_missing"] = stats.get("graphics_missing", 0) + 1
            return match.group(0)
        try:
            normalized = normalize_raster_image(source.read_bytes())
        except (InvalidImageError, OSError) as exc:
            print(f"[exam] 图片校验失败 {ref}: {exc}", file=sys.stderr)
            stats["graphics_invalid"] = stats.get("graphics_invalid", 0) + 1
            return match.group(0)
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOADS_DIR / f"{name_prefix}-{len(urls)}{normalized.extension}"
        target.write_bytes(normalized.data)
        try:
            url = normalize_upload_asset_references(
                [target.name], uploads_dir=UPLOADS_DIR, url_prefix="static/uploads"
            )[0]
        except Exception:
            stats["graphics_invalid"] = stats.get("graphics_invalid", 0) + 1
            return match.group(0)
        urls.append(url)
        return f"![题目插图]({url})"

    return INCLUDEGRAPHICS_RE.sub(_replace, text), urls


def compose_question(event: dict, stats: dict[str, int]) -> dict:
    """把一个原生题目事件转换为成品记录（清洗宏、转 choices、落图）。"""

    body = sanitize_latex(event["body"])
    body, fill_answer = strip_answer_macros(body)
    choices, stem = extract_options(body)
    origin = event["_origin"]
    prefix = "exam-{year}-{file}-p{part}-q{q}".format(**origin)
    stem, urls1 = materialize_graphics(stem, prefix, stats)
    content = stem
    urls2: list[str] = []
    if choices:
        choices, urls2 = materialize_graphics(choices, prefix, stats)
        content = stem + "\n\n" + choices
    flags = list(event.get("flags", []))
    if "\\pickout" in body:
        flags.append("残留pickout")
    answer = f"**答案** {fill_answer}" if fill_answer else ""
    return {
        "content": content,
        "question_type": event["qtype"],
        "image_urls": list(dict.fromkeys(urls1 + urls2)),
        "answer_markdown": answer,
        "flags": flags,
    }


def build_records(start_year: int, end_year: int) -> tuple[list[dict], list[dict]]:
    """返回 (题目记录列表, 套卷组装列表)。

    跨卷复用的题（useproblem）只入库一次，来源归属首个定义它的卷，
    其余卷的套卷直接引用同一题，并在题的 tags 里记录共用卷别。
    """

    stats: dict[str, int] = {}
    records: list[dict] = []
    question_by_origin: dict[tuple, dict] = {}
    papers: list[dict] = []

    def own_origin(year: int, file: str, event: dict) -> dict:
        return {
            "year": year,
            "file": file,
            "part": event["part"],
            "q": event.get("q_in_part") if year < 2004 else event.get("q_total"),
        }

    for year in range(start_year, end_year + 1):
        year_dir = KYSX_DIR / "year" / str(year)
        if not year_dir.is_dir():
            continue
        for suffix, (exam_track, track_short) in TRACK_BY_FILE.items():
            tex_path = year_dir / f"{year}{suffix}.tex"
            if not tex_path.is_file():
                print(f"[exam] 缺少 {tex_path.name}，跳过", file=sys.stderr)
                continue
            paper = resolve_paper(year, suffix)
            loads: dict[int, str] = {}
            for event in paper["events"]:
                if event["kind"] == "load":
                    loads[event["slot"]] = event["file"]
            paper_items: list[dict] = []
            per_score = None
            part_total = None
            part_count = 0
            current_qtype = "detailed_answer"
            for event in paper["events"]:
                if event["kind"] == "load":
                    continue
                if event["kind"] == "part":
                    current_qtype = event["qtype"]
                    per_score = event["per_score"]
                    part_total = event["part_total"]
                    part_count = 0
                    continue
                part_count += 1
                if event["kind"] == "own":
                    event.setdefault("_origin", own_origin(year, paper["file"], event))
                    origin = event["_origin"]
                    qtype = event["qtype"]
                    body_event = event
                else:
                    slot_file = loads.get(event["slot"])
                    if not slot_file:
                        raise ValueError(f"复用引用未载入: {year}{suffix} slot={event['slot']}")
                    src = resolve_use(year, slot_file, event["part"], event["q"])
                    src.setdefault(
                        "_origin",
                        own_origin(year, slot_file, src),
                    )
                    origin = src["_origin"]
                    qtype = current_qtype
                    body_event = src
                points = event.get("points") if event["kind"] == "own" else event.get("use_points")
                if points is not None:
                    score = points
                elif per_score is not None:
                    score = per_score
                elif part_total is not None and part_count == 1:
                    score = part_total
                else:
                    score = 4 if current_qtype != "detailed_answer" else 10
                key = (origin["year"], origin["file"], origin["part"], origin["q"])
                if key not in question_by_origin:
                    record = compose_question(body_event, stats)
                    record.update(
                        {
                            "origin": key,
                            "source_name": f"{origin['year']}{TRACK_BY_FILE[origin['file'][-2:]][1]}",
                            "source_number": 0,
                            "shared_tracks": [],
                            "subject": "",
                            "topic": "",
                            "solution_found": False,
                        }
                    )
                    question_by_origin[key] = record
                    records.append(record)
                record = question_by_origin[key]
                if (origin["year"], origin["file"]) != (year, paper["file"]):
                    shared = f"{year}{track_short}"
                    if shared not in record["shared_tracks"]:
                        record["shared_tracks"].append(shared)
                paper_items.append({"record": record, "score": score})
            for index, item in enumerate(paper_items):
                if item["record"]["source_number"] == 0:
                    item["record"]["source_number"] = index + 1
            # 同题被多个卷共享时，source_number 取首个成卷编号；数一卷先组装，
            # 其余卷引用同一记录，编号以定义卷为准。
            papers.append(
                {
                    "year": year,
                    "exam_track": exam_track,
                    "items": [
                        {"source_name": item["record"]["source_name"],
                         "source_number": item["record"]["source_number"],
                         "score": item["score"]}
                        for item in paper_items
                    ],
                }
            )
    if stats:
        print(f"[exam] 图片处理统计: {stats}")
    return records, papers


def out_dir() -> Path:
    path = BUILD_DIR / "exam-papers"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cmd_build(args: argparse.Namespace) -> int:
    records, papers = build_records(args.start_year, args.end_year)
    questions_path = out_dir() / "questions.json"
    questions_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir() / "papers.json").write_text(
        json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"题目总数: {len(records)}（跨卷复用已去重）"]
    lines.append(f"套卷数: {len(papers)}")
    for paper in sorted(papers, key=lambda x: (x["year"], x["exam_track"])):
        items = paper["items"]
        lines.append(
            f"  {paper['year']}{paper['exam_track']}: {len(items)} 题，"
            f"分值合计 {sum(i['score'] for i in items)}"
        )
    shared = [r for r in records if r.get("shared_tracks")]
    lines.append(f"跨卷共用题: {len(shared)}")
    flagged = [r for r in records if r["flags"]]
    lines.append(f"带异常标记题目: {len(flagged)}")
    for r in flagged[:30]:
        lines.append(f"  - {r['source_name']}({r['source_number']}): {','.join(r['flags'])}")
    no_choice = [
        r for r in records
        if r["question_type"] == "single_choice" and "\\begin{choices}" not in r["content"]
    ]
    lines.append(f"选择题缺选项环境: {len(no_choice)}")
    for r in no_choice[:10]:
        lines.append(f"  - {r['source_name']}({r['source_number']})")
    (out_dir() / "build-report.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"[exam] 产物: {questions_path}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from mathbank.database import Question, SessionLocal, Source, init_db
    from mathbank.asset_security import normalize_upload_asset_references

    records = json.loads((out_dir() / "questions.json").read_text(encoding="utf-8"))
    init_db()
    db = SessionLocal()
    created = skipped = failed = updated = 0
    try:
        for record in records:
            # 来源名形如 1987数一，卷别即考试方向；subject/topic 待分类阶段回填。
            track_short = record["source_name"][-2:]
            exam_track = {"数一": "数学一", "数二": "数学二", "数三": "数学三"}[track_short]
            source = (
                db.query(Source).filter(Source.name == record["source_name"]).first()
            )
            if source is None:
                source = Source(name=record["source_name"], series="考研数学真题", note="真题导入")
                db.add(source)
                db.flush()
            existing = (
                db.query(Question)
                .filter(
                    Question.source_id == source.id,
                    Question.source_scope == "",
                    Question.source_number == record["source_number"],
                )
                .first()
            )
            if existing is not None:
                if not existing.answer_markdown and record["answer_markdown"]:
                    existing.answer_markdown = record["answer_markdown"]
                    db.commit()
                    updated += 1
                else:
                    skipped += 1
                continue
            try:
                question = Question(
                    content=record["content"],
                    question_type=record["question_type"],
                    exam_track=exam_track,
                    subject="待分类",
                    topic="待分类",
                    difficulty="standard",
                    source_id=source.id,
                    source_number=record["source_number"],
                    source_scope="",
                    answer_markdown=record["answer_markdown"],
                )
                if record["image_urls"]:
                    question.image_paths = normalize_upload_asset_references(
                        record["image_urls"],
                        uploads_dir=UPLOADS_DIR,
                        url_prefix="static/uploads",
                    )
                db.add(question)
                db.commit()
                created += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                print(
                    f"[exam] 写入失败 {record['source_name']}({record['source_number']}): {exc}",
                    file=sys.stderr,
                )
    finally:
        db.close()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[exam:import:{mode}] 入库 {created}，更新 {updated}，跳过 {skipped}，失败 {failed}")
    if not args.apply:
        print("[exam:import:dry-run] 未写库。加 --apply 执行实际导入。")
    return 1 if failed else 0


def cmd_papers(args: argparse.Namespace) -> int:
    from mathbank.database import Paper, PaperQuestion, Question, SessionLocal, Source, init_db

    papers = json.loads((out_dir() / "papers.json").read_text(encoding="utf-8"))
    init_db()
    db = SessionLocal()
    created = skipped = failed = 0
    try:
        for paper in sorted(papers, key=lambda x: (x["year"], x["exam_track"])):
            year = paper["year"]
            exam_track = paper["exam_track"]
            items = paper["items"]
            title = f"{year}年{exam_track}真题"
            existing = db.query(Paper).filter(Paper.title == title).first()
            if existing is not None:
                skipped += 1
                continue
            try:
                questions = []
                for record in items:
                    source = (
                        db.query(Source)
                        .filter(Source.name == record["source_name"])
                        .first()
                    )
                    question = (
                        db.query(Question)
                        .filter(
                            Question.source_id == source.id,
                            Question.source_scope == "",
                            Question.source_number == record["source_number"],
                        )
                        .first()
                    )
                    if question is None:
                        raise ValueError(f"题目未入库: {record['source_name']}({record['source_number']})")
                    questions.append((question, record["score"]))
                paper = Paper(
                    title=title,
                    subtitle=f"{year}年全国硕士研究生入学统一考试{exam_track}试题",
                    paper_type="kaoyan",
                    total_score=sum(score for _q, score in questions),
                    metadata_json=json.dumps({"imported_from": "kysx"}, ensure_ascii=False),
                )
                db.add(paper)
                db.flush()
                for index, (question, score) in enumerate(questions):
                    db.add(
                        PaperQuestion(
                            paper_id=paper.id,
                            question_id=question.id,
                            order_index=index + 1,
                            score=score,
                        )
                    )
                db.commit()
                created += 1
            except Exception as exc:
                db.rollback()
                failed += 1
                print(f"[exam:paper] 失败 {title}: {exc}", file=sys.stderr)
    finally:
        db.close()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[exam:paper:{mode}] 建卷 {created}，跳过 {skipped}，失败 {failed}")
    if not args.apply:
        print("[exam:paper:dry-run] 未写库。加 --apply 执行实际导入。")
    return 1 if failed else 0




def cmd_classify(args: argparse.Namespace) -> int:
    """用 PREFER_CLASSIFY_MODEL 为真题题目标注科目与考点（含 K 版镜像回填）。"""

    import os

    from concurrent.futures import ThreadPoolExecutor

    from dotenv import load_dotenv

    from mathbank.ai_http import post_chat_completion
    from mathbank.ai_json import parse_ai_json
    from mathbank.ai_providers import (
        apply_bailian_thinking_policy,
        inject_reasoning_effort,
        resolve_text_provider,
    )
    from mathbank.curriculums import load_curriculum
    from mathbank.database import Question, QuestionCurriculum, SessionLocal, init_db
    from mathbank.prompts import build_classification_system_prompt

    load_dotenv()
    curriculum = load_curriculum()
    system_prompt = build_classification_system_prompt(curriculum)

    model = os.getenv("PREFER_CLASSIFY_MODEL") or os.getenv("PREFER_PARSE_MODEL") or ""
    provider = resolve_text_provider(model)
    if not provider.api_key:
        print("未配置分类模型 API Key，无法执行 AI 分类。", file=sys.stderr)
        return 2

    def build_payload(content: str) -> dict:
        data = {
            "model": provider.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"题目内容:\n{content}"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 512,
        }
        data = inject_reasoning_effort(data, provider.reasoning_effort)
        return apply_bailian_thinking_policy(
            data, provider_code=provider.provider_code, model_name=provider.model_name, task="classify"
        )

    init_db()
    db = SessionLocal()
    try:
        pending = (
            db.query(Question)
            .filter(Question.subject == "待分类")
            .order_by(Question.id.asc())
            .all()
        )
        meta = [(q.id, q.exam_track, q.content[:1500]) for q in pending]
    finally:
        db.close()
    print(f"[exam:classify] 待分类题目: {len(meta)}，模型: {provider.model_name}")

    def ask(item: tuple) -> tuple:
        qid, track, content = item
        try:
            response = post_chat_completion(provider, build_payload(content), timeout=120.0)
            payload = parse_ai_json(response)
            return qid, track, payload or {}
        except Exception as exc:
            print(f"[exam:classify] 题目 {qid} 分类失败: {exc}", file=sys.stderr)
            return qid, track, {}

    updated = failed = 0
    db = SessionLocal()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for qid, track, payload in pool.map(ask, meta):
                subject = str(payload.get("subject") or "").strip()
                topic = str(payload.get("topic") or "").strip()
                ai_track = str(payload.get("exam_track") or "").strip()
                valid_track = ai_track if ai_track in curriculum else track
                if subject not in curriculum.get(valid_track, {}) and track in curriculum:
                    valid_track = track
                subjects = curriculum.get(valid_track, {})
                if subject not in subjects:
                    failed += 1
                    continue
                if topic not in subjects[subject]:
                    topic = subjects[subject][-1] if subjects[subject] else subject
                question = db.query(Question).filter(Question.id == qid).first()
                if question is None:
                    continue
                question.exam_track = valid_track
                question.subject = subject
                question.topic = topic
                mirror = (
                    db.query(QuestionCurriculum)
                    .filter(
                        QuestionCurriculum.question_id == qid,
                        QuestionCurriculum.version_code == "K",
                    )
                    .first()
                )
                if mirror is None:
                    db.add(
                        QuestionCurriculum(
                            question_id=qid,
                            version_code="K",
                            exam_track=valid_track,
                            subject=subject,
                            topic=topic,
                        )
                    )
                else:
                    mirror.exam_track = valid_track
                    mirror.subject = subject
                    mirror.topic = topic
                if args.apply:
                    db.commit()
                else:
                    db.rollback()
                updated += 1
                if updated % 100 == 0:
                    print(f"[exam:classify] 进度 {updated}/{len(meta)}")
    finally:
        if args.apply:
            db.commit()
        db.close()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[exam:classify:{mode}] 更新 {updated}，无效 {failed}")
    return 0




SOLUTION_CACHE_LAYOUT = {
    "数一": lambda y: [f"jie-s1-{y}"],
    "数二": lambda y: [f"jie-s2-1989-2004" if 1989 <= y <= 2004 else f"jie-s2-{y}"],
    "数三": lambda y: [f"jie-s3-{y}"],
}
SOLUTION_YEAR_HEADING_RE = re.compile(r"^#{1,4}\s*((?:19|20)\d{2})\s*年")
SOLUTION_NUMBER_RE = re.compile(r"^(?:[一二三四五六七八九十]+、\s*)?[（(](\d{1,2})[)）]")
# 早期卷解答大题一题一大题，用「五、【解】」中文数字直接做条目标记。
SOLUTION_CN_MARKER_RE = re.compile(
    r"^[一二三四五六七八九十]+、\s*【(?:答案|解|解析|解答|证明|详解|分析)】"
)





def parse_solution_cache_for_year(cache_key: str, year: int) -> list[dict]:
    """解析缓存并只保留目标年份的解析条目（按文档顺序）。"""

    cache_dir = OCR_CACHE_DIR / cache_key
    if not cache_dir.is_dir():
        return []
    items: list[dict] = []
    active = False
    current: dict | None = None
    markers = ("【答案】", "【解】", "【解析】", "【解答】", "【证明】", "【详解】", "【分析】")

    def flush() -> None:
        nonlocal current
        if current is not None:
            text = "\n".join(current["lines"]).strip()
            if text:
                items.append({"number": current["number"], "text": text})
        current = None

    for page in sorted(cache_dir.glob("p*.md")):
        for raw_line in page.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            heading = SOLUTION_YEAR_HEADING_RE.match(line)
            if heading:
                flush()
                active = int(heading.group(1)) == year
                continue
            if active and SOLUTION_CN_MARKER_RE.match(line):
                flush()
                current = {"number": 0, "lines": [line]}
                continue
            number = SOLUTION_NUMBER_RE.match(line)
            if number and active:
                rest = line[number.end():].lstrip()
                if rest.startswith(markers):
                    flush()
                    current = {"number": int(number.group(1)), "lines": [line]}
                    continue
            if current is not None and line and "nocode.host" not in line:
                current["lines"].append(line)
    flush()
    return items


LATEX_STRIP_RE = re.compile(r"\$[^$]*\$|\\[a-zA-Z]+|[0-9\s，。；：、（）()\[\]{},.!?\u3000]+")


def _fingerprint(text: str) -> set[str]:
    """提取汉字 bigram 指纹（剥离 LaTeX 与数字标点）用于题干-解析关联。"""

    plain = LATEX_STRIP_RE.sub("", text or "")
    return {plain[i : i + 2] for i in range(len(plain) - 1) if plain[i : i + 2].strip()}


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def segmented_align(items: list[dict], total: int) -> dict[int, str]:
    """分段顺序对齐：编号重启与中文大题标记是段边界，段内顺序即卷面顺序。"""

    segments: list[list[dict]] = []
    for item in items:
        if not segments or (item["number"] != 0 and item["number"] <= segments[-1][-1]["number"]):
            segments.append([item])
        else:
            segments[-1].append(item)

    result: dict[int, str] = {}
    next_number = 1
    for segment in segments:
        for item in segment:
            if next_number > total:
                result[total] = result.get(total, "") + "\n" + item["text"]
                continue
            result[next_number] = item["text"]
            next_number += 1
    return result


def monotonic_align(items: list[dict], stems: list[str]) -> dict[int, str]:
    """锚点式单调对齐：高分指纹配对作锚点，锚点间等量区间顺序填充。"""

    total = len(stems)
    prints = [_fingerprint(stem) for stem in stems]
    item_prints = [_fingerprint(item["text"][:400]) for item in items]

    candidates: list[tuple[float, int, int]] = []
    window = 4
    for j in range(total):
        for i in range(max(0, j - window), min(len(items), j + window + 1)):
            score = _similarity(item_prints[i], prints[j])
            if score >= 0.10:
                candidates.append((score, i, j))
    candidates.sort(reverse=True)

    anchors: list[tuple[int, int]] = []
    last_i = last_j = -1
    for score, i, j in candidates:
        if i > last_i and j > last_j:
            anchors.append((i, j))
            last_i, last_j = i, j
    anchors.sort()

    if not anchors:
        return segmented_align(items, total)

    result: dict[int, str] = {}

    def fill(i_start: int, j_start: int, i_end: int, j_end: int) -> None:
        span_items = i_end - i_start
        span_questions = j_end - j_start
        if span_items >= span_questions and span_questions > 0:
            # 锚点已钉住区间两端；条目多于题数（误切）时并入末题。
            for offset in range(span_questions):
                result[j_start + offset + 1] = items[i_start + offset]["text"]
            if span_items > span_questions:
                extra = "\n".join(item["text"] for item in items[i_start + span_questions:i_end])
                result[j_start + span_questions] += "\n" + extra
        elif span_questions == 1:
            merged = "\n".join(item["text"] for item in items[i_start:i_end])
            result[j_start + 1] = merged

    prev_i = prev_j = -1
    for i, j in anchors + [(len(items), total)]:
        fill(prev_i + 1, prev_j + 1, i, j)
        if i < len(items) and j < total:
            result[j + 1] = items[i]["text"]
        prev_i, prev_j = i, j
    return result



def cmd_match(args: argparse.Namespace) -> int:
    """把解析缓存按（年，卷，题号）匹配回填到已导入真题。"""

    from mathbank.database import Question, SessionLocal, Source, init_db

    init_db()
    db = SessionLocal()
    matched = missing_solution = missing_question = 0
    report: list[str] = []
    try:
        for year in range(args.start_year, args.end_year + 1):
            for track_short in ("数一", "数二", "数三"):
                source = db.query(Source).filter(Source.name == f"{year}{track_short}").first()
                if source is None:
                    continue
                questions = (
                    db.query(Question)
                    .filter(Question.source_id == source.id)
                    .order_by(Question.source_number.asc())
                    .all()
                )
                if not questions:
                    continue
                items: list[dict] = []
                for cache_key in SOLUTION_CACHE_LAYOUT[track_short](year):
                    items.extend(parse_solution_cache_for_year(cache_key, year))
                stems = [q.content or "" for q in questions]
                solutions = monotonic_align(items, stems)
                paper_matched = 0
                for question in questions:
                    solution = solutions.get(question.source_number)
                    if not solution:
                        missing_solution += 1
                        report.append(f"  - {year}{track_short}({question.source_number}): 无解析")
                        continue
                    if question.answer_markdown and not question.answer_markdown.startswith("**答案**"):
                        continue
                    question.answer_markdown = solution
                    paper_matched += 1
                    matched += 1
                db.commit()
                report.insert(0, f"{year}{track_short}: 匹配 {paper_matched}/{len(questions)}")
    finally:
        db.close()
    print("\n".join(report[:40]))
    print(f"[exam:match] 回填 {matched}，缺解析 {missing_solution}，缺题目 {missing_question}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.import_exam_tex",
        description="考研数学真题 LaTeX（kysx）导入：build → import → papers → match",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="解析 kysx TeX 源生成题目产物")
    build.add_argument("--start-year", type=int, default=1987)
    build.add_argument("--end-year", type=int, default=2009)
    build.set_defaults(handler=cmd_build)

    imp = sub.add_parser("import", help="题目入库（默认 dry-run）")
    imp.add_argument("--apply", action="store_true")
    imp.set_defaults(handler=cmd_import)

    papers = sub.add_parser("papers", help="按年按卷创建套卷（默认 dry-run）")
    papers.add_argument("--apply", action="store_true")
    papers.set_defaults(handler=cmd_papers)

    match = sub.add_parser("match", help="把解析缓存回填到已导入真题")
    match.add_argument("--start-year", type=int, default=1987)
    match.add_argument("--end-year", type=int, default=2009)
    match.set_defaults(handler=cmd_match)

    classify = sub.add_parser("classify", help="AI 批量分类真题科目与考点")
    classify.add_argument("--apply", action="store_true")
    classify.add_argument("--workers", type=int, default=4)
    classify.set_defaults(handler=cmd_classify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
