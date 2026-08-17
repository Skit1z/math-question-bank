"""Conservative question-form detection for the national graduate exam."""

import re


QUESTION_FORM_SINGLE_CHOICE = "single_choice"
QUESTION_FORM_FILL_IN_BLANK = "fill_in_blank"
QUESTION_FORM_DETAILED_ANSWER = "detailed_answer"
QUESTION_FORM_UNKNOWN = "unknown"


_CHOICES_ENV_PATTERN = re.compile(r"\\begin\s*\{\s*choices\s*\}", re.IGNORECASE)
_FILLIN_PATTERN = re.compile(r"\\fillin\b", re.IGNORECASE)


def detect_structured_question_form(content: str) -> str | None:
    """Use unambiguous LaTeX structure before consulting an AI model."""

    normalized = str(content or "")
    if _CHOICES_ENV_PATTERN.search(normalized):
        return QUESTION_FORM_SINGLE_CHOICE
    if _FILLIN_PATTERN.search(normalized):
        return QUESTION_FORM_FILL_IN_BLANK
    return None


def normalize_ai_question_form(value: object) -> str:
    """Collapse model output to one of the three national-exam forms."""

    normalized = str(value or "").strip().lower()
    mapping = {
        "single_choice": QUESTION_FORM_SINGLE_CHOICE,
        "fill_in_blank": QUESTION_FORM_FILL_IN_BLANK,
        "detailed_answer": QUESTION_FORM_DETAILED_ANSWER,
        "unknown": QUESTION_FORM_UNKNOWN,
    }
    return mapping.get(normalized, QUESTION_FORM_UNKNOWN)
