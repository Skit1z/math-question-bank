"""HTTP client for the hosted PaddleOCR official API.

The official API is asynchronous: a local file is uploaded to a job endpoint,
the job is polled until it finishes, and the resulting JSONL document is then
downloaded.  PP-OCR models return ``ocrResults`` while PaddleOCR-VL models
return ``layoutParsingResults`` with Markdown output.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

import requests


DEFAULT_BASE_URL = "https://paddleocr.aistudio-app.com"
DEFAULT_MODEL = "PP-OCRv6"
PADDLEOCR_VL_MODELS = frozenset(
    {
        "paddleocr-vl",
        "paddleocr-vl-1.5",
        "paddleocr-vl-1.6",
    }
)
# Match the official SDK defaults: one HTTP request may take up to five
# minutes, while the complete asynchronous job may take up to ten minutes.
DEFAULT_REQUEST_TIMEOUT = 300.0
DEFAULT_POLL_TIMEOUT = 600.0
JOBS_PATH = "/api/v2/ocr/jobs"


class PaddleOCRError(RuntimeError):
    """Raised when PaddleOCR returns an unusable API response."""


def is_document_parsing_model(model: str) -> bool:
    """Return whether ``model`` uses PaddleOCR's document-parsing result shape."""

    return str(model or "").strip().casefold() in PADDLEOCR_VL_MODELS


def _optional_payload(model: str) -> dict[str, Any]:
    """Build model-specific options for the official API request."""

    if is_document_parsing_model(model):
        return {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }
    return {}


def _jobs_url(base_url: str) -> str:
    normalized = str(base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    if not normalized:
        normalized = DEFAULT_BASE_URL
    return f"{normalized}{JOBS_PATH}"


def _auth_headers(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _response_json(response: requests.Response, operation: str) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PaddleOCRError(
            f"PaddleOCR {operation}返回的内容不是有效 JSON。"
        ) from exc

    if not isinstance(payload, Mapping):
        raise PaddleOCRError(f"PaddleOCR {operation}返回的数据格式无效。")

    if not 200 <= response.status_code < 300:
        message = payload.get("message") or payload.get("errorMsg") or ""
        suffix = f": {message}" if message else "。"
        raise PaddleOCRError(
            f"PaddleOCR {operation}失败（HTTP {response.status_code}）{suffix}"
        )

    code = payload.get("code", 0)
    if code not in (0, None):
        message = payload.get("message") or payload.get("errorMsg") or ""
        suffix = f": {message}" if message else "。"
        raise PaddleOCRError(f"PaddleOCR {operation}失败（错误码 {code}）{suffix}")

    return payload


def _response_data(response: requests.Response, operation: str) -> Mapping[str, Any]:
    payload = _response_json(response, operation)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise PaddleOCRError(f"PaddleOCR {operation}返回中缺少 data 对象。")
    return data


def _result_jsonl(response: requests.Response) -> list[Mapping[str, Any]]:
    if not 200 <= response.status_code < 300:
        raise PaddleOCRError(
            f"PaddleOCR 结果下载失败（HTTP {response.status_code}）。"
        )

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        return [payload]

    records: list[Mapping[str, Any]] = []
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaddleOCRError("PaddleOCR 结果不是有效的 JSONL。") from exc
        if not isinstance(item, Mapping):
            raise PaddleOCRError("PaddleOCR JSONL 结果包含无效记录。")
        records.append(item)
    return records


def _append_text(value: Any, output: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            output.append(text)
        return

    if isinstance(value, (list, tuple)):
        for item in value:
            _append_text(item, output)
        return

    if isinstance(value, Mapping):
        for key in ("text", "rec_text", "transcription", "content"):
            if key in value:
                _append_text(value[key], output)
                return


def _extract_pruned_text(pruned_result: Any) -> list[str]:
    """Extract recognized text while ignoring PaddleOCR coordinates/scores."""

    if isinstance(pruned_result, str):
        return [pruned_result.strip()] if pruned_result.strip() else []
    if not isinstance(pruned_result, Mapping):
        return []

    output: list[str] = []
    for key in (
        "rec_texts",
        "texts",
        "text",
        "rec_text",
        "transcription",
        "ocrText",
        "ocr_text",
    ):
        if key in pruned_result:
            _append_text(pruned_result[key], output)

    if output:
        return output

    # A few self-hosted/older service wrappers nest the original result under
    # ``res``.  Recurse only through mappings so geometry arrays are ignored.
    for value in pruned_result.values():
        if isinstance(value, Mapping):
            output.extend(_extract_pruned_text(value))
    return output


def extract_ocr_text(records: list[Mapping[str, Any]]) -> str:
    """Convert official PaddleOCR JSONL records to newline-separated text."""

    output: list[str] = []
    for record in records:
        result = record.get("result", record)
        if not isinstance(result, Mapping):
            continue

        pages = result.get("ocrResults")
        if not isinstance(pages, list):
            pages = [result]

        for page in pages:
            if not isinstance(page, Mapping):
                continue
            output.extend(_extract_pruned_text(page.get("prunedResult", page)))

    text = "\n".join(item for item in output if item)
    if not text:
        raise PaddleOCRError("PaddleOCR 返回成功，但没有识别到文字。")
    return text


def extract_document_parsing_text(records: list[Mapping[str, Any]]) -> str:
    """Extract Markdown text from PaddleOCR-VL document-parsing results.

    PaddleOCR-VL returns one ``layoutParsingResults`` item per page.  The
    Markdown text is the useful OCR output for this application because it
    preserves document structure and formula markup.
    """

    pages_text: list[str] = []
    for record in records:
        result = record.get("result", record)
        if not isinstance(result, Mapping):
            continue

        pages = result.get("layoutParsingResults")
        if not isinstance(pages, list):
            continue

        for page in pages:
            if not isinstance(page, Mapping):
                continue

            markdown = page.get("markdown")
            markdown_text = ""
            if isinstance(markdown, Mapping):
                markdown_text = str(markdown.get("text") or "").strip()
            elif isinstance(markdown, str):
                markdown_text = markdown.strip()

            if markdown_text:
                pages_text.append(markdown_text)
                continue

            # Keep a useful fallback for wrappers that omit Markdown but
            # still return the pruned recognition result.
            pages_text.extend(
                _extract_pruned_text(page.get("prunedResult", page))
            )

    text = "\n\n".join(item for item in pages_text if item)
    if not text:
        raise PaddleOCRError(
            "PaddleOCR-VL 返回成功，但没有识别到可用的 Markdown 文本。"
        )
    return text


def _result_url(status_data: Mapping[str, Any]) -> str:
    result_url = status_data.get("resultUrl")
    if isinstance(result_url, Mapping):
        result_url = result_url.get("jsonUrl")
    if not isinstance(result_url, str) or not result_url.strip():
        raise PaddleOCRError("PaddleOCR 完成状态中缺少结果地址。")
    return result_url.strip()


def extract_markdown_assets(
    records: list[Mapping[str, Any]]
) -> tuple[str, dict[str, str]]:
    """Return ``(markdown_text, {image_name: image_url})`` from VL results.

    PaddleOCR-VL delivers page figures as temporary object-storage URLs under
    ``markdown.images`` keyed by the same ``imgs/...`` names referenced from
    the Markdown text, so callers must persist the assets themselves.
    """

    pages_text: list[str] = []
    assets: dict[str, str] = {}
    for record in records:
        result = record.get("result", record)
        if not isinstance(result, Mapping):
            continue

        pages = result.get("layoutParsingResults")
        if not isinstance(pages, list):
            continue

        for page in pages:
            if not isinstance(page, Mapping):
                continue
            markdown = page.get("markdown")
            if not isinstance(markdown, Mapping):
                continue
            markdown_text = str(markdown.get("text") or "").strip()
            if markdown_text:
                pages_text.append(markdown_text)
            images = markdown.get("images")
            if isinstance(images, Mapping):
                for name, url in images.items():
                    if isinstance(name, str) and isinstance(url, str) and url.strip():
                        assets[name.strip()] = url.strip()

    text = "\n\n".join(item for item in pages_text if item)
    if not text:
        raise PaddleOCRError(
            "PaddleOCR-VL 返回成功，但没有识别到可用的 Markdown 文本。"
        )
    return text, assets


def paddle_ocr_page_assets(
    image_path: str,
    *,
    access_token: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    sleep_fn=time.sleep,
) -> tuple[str, dict[str, str]]:
    """Submit one page and return ``(markdown_text, {image_name: image_url})``.

    Only meaningful for document-parsing (VL) models; PP-OCR models return an
    empty asset mapping.  The URLs are short-lived, so download promptly.
    """

    records = _run_ocr_job(
        image_path,
        access_token=access_token,
        base_url=base_url,
        model=model,
        request_timeout=request_timeout,
        poll_timeout=poll_timeout,
        sleep_fn=sleep_fn,
    )
    if is_document_parsing_model(str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL):
        return extract_markdown_assets(records)
    return extract_ocr_text(records), {}


def _run_ocr_job(
    image_path: str,
    *,
    access_token: str,
    base_url: str,
    model: str,
    request_timeout: float,
    poll_timeout: float,
    sleep_fn=time.sleep,
) -> list[Mapping[str, Any]]:
    """Submit one local image and poll until the JSONL records are ready."""

    token = str(access_token or "").strip()
    if not token:
        raise ValueError("未配置 PaddleOCR Access Token (PADDLEOCR_ACCESS_TOKEN)")

    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))

    jobs_url = _jobs_url(base_url)
    headers = _auth_headers(token)
    model_name = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    upload_data = {
        "model": model_name,
        "optionalPayload": json.dumps(
            _optional_payload(model_name), ensure_ascii=False
        ),
    }

    with path.open("rb") as image_file:
        response = requests.post(
            jobs_url,
            headers=headers,
            data=upload_data,
            files={"file": (path.name, image_file)},
            timeout=request_timeout,
        )
    submit_data = _response_data(response, "任务提交")
    job_id = submit_data.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip():
        raise PaddleOCRError("PaddleOCR 任务提交成功，但没有返回 jobId。")
    job_id = job_id.strip()

    deadline = time.monotonic() + max(float(poll_timeout), 0.1)
    interval = 3.0
    while True:
        status_response = requests.get(
            f"{jobs_url}/{job_id}",
            headers=headers,
            timeout=request_timeout,
        )
        status_data = _response_data(status_response, "任务状态查询")
        state = str(status_data.get("state") or "").lower()

        if state == "done":
            result_response = requests.get(
                _result_url(status_data),
                timeout=request_timeout,
            )
            return _result_jsonl(result_response)

        if state == "failed":
            message = status_data.get("errorMsg") or "未知错误"
            raise PaddleOCRError(f"PaddleOCR 任务失败: {message}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("PaddleOCR 任务轮询超时。")

        sleep_fn(min(interval, remaining))
        interval = min(interval * 1.5, 15.0)


def paddle_ocr_image(
    image_path: str,
    *,
    access_token: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    sleep_fn=time.sleep,
) -> str:
    """Submit one local image to PaddleOCR and return recognized text.

    The result URL is normally a pre-signed object-storage URL, so the access
    token is intentionally sent only to PaddleOCR's submit/status endpoints.
    """

    records = _run_ocr_job(
        image_path,
        access_token=access_token,
        base_url=base_url,
        model=model,
        request_timeout=request_timeout,
        poll_timeout=poll_timeout,
        sleep_fn=sleep_fn,
    )
    model_name = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if is_document_parsing_model(model_name):
        return extract_document_parsing_text(records)
    return extract_ocr_text(records)
