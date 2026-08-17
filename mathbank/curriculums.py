"""Single source of truth for the graduate entrance mathematics profile."""

from copy import deepcopy
from functools import lru_cache
import json

from mathbank.paths import CURRICULUMS_DIR


KAOYAN_VERSION = "K"
CURRICULUM_NAMES = {KAOYAN_VERSION: "考研数学"}

DEFAULT_QUESTION_TYPES = [
    {"value": "single_choice", "label": "选择题"},
    {"value": "fill_in_blank", "label": "填空题"},
    {"value": "detailed_answer", "label": "解答题"},
]

DEFAULT_DIFFICULTIES = [
    {
        "value": "basic",
        "label": "基础巩固",
        "color": "text-green-600 bg-green-50 border-green-200",
    },
    {
        "value": "standard",
        "label": "真题常规",
        "color": "text-blue-600 bg-blue-50 border-blue-200",
    },
    {
        "value": "comprehensive",
        "label": "综合提升",
        "color": "text-red-600 bg-red-50 border-red-200",
    },
    {
        "value": "advanced",
        "label": "压轴拔高",
        "color": "text-purple-600 bg-purple-50 border-purple-200",
    },
]


def normalize_version_code(version: str = KAOYAN_VERSION) -> str:
    code = str(version or KAOYAN_VERSION).strip().upper()
    if code != KAOYAN_VERSION:
        raise ValueError(f"不支持的考研数学大纲版本: {version}")
    return code


@lru_cache(maxsize=1)
def _load_curriculum_cached(version: str) -> dict:
    code = normalize_version_code(version)
    path = CURRICULUMS_DIR / f"{code}.json"
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"考研数学大纲资源格式错误: {path}")
    return data


def load_curriculum(version: str = KAOYAN_VERSION) -> dict:
    """Return an isolated copy of the exam-track -> subject -> topic tree."""

    return deepcopy(_load_curriculum_cached(normalize_version_code(version)))


def build_default_metadata(version: str = KAOYAN_VERSION) -> dict:
    """Build the editable metadata used on first boot and by the browser."""

    code = normalize_version_code(version)
    return {
        "domain": "kaoyan_math",
        "profile_name": "考研数学题库",
        "curriculum_version": code,
        "question_types": deepcopy(DEFAULT_QUESTION_TYPES),
        "difficulties": deepcopy(DEFAULT_DIFFICULTIES),
        "curriculum": load_curriculum(code),
        "paper_defaults": {
            "paper_type": "kaoyan",
            "total_score": 150,
            "duration_minutes": 180,
        },
    }


def get_curriculum_preset(version: str = KAOYAN_VERSION) -> dict:
    code = normalize_version_code(version)
    return {
        "version": code,
        "name": CURRICULUM_NAMES[code],
        "metadata": build_default_metadata(code),
    }
