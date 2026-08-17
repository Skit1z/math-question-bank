import json
from unittest.mock import patch

from main import ocr_via_provider
from mathbank.ai_providers import resolve_ocr_provider
from mathbank.paddle_ocr import (
    extract_document_parsing_text,
    extract_ocr_text,
    paddle_ocr_image,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_extract_ocr_text_reads_pruned_rec_texts_without_coordinates():
    records = [
        {
            "result": {
                "ocrResults": [
                    {"prunedResult": {"rec_texts": ["设函数", "f(x)=x^2"]}},
                    {"prunedResult": {"rec_texts": ["求导数"]}},
                ]
            }
        }
    ]

    assert extract_ocr_text(records) == "设函数\nf(x)=x^2\n求导数"


def test_extract_document_parsing_text_reads_paddleocr_vl_markdown():
    records = [
        {
            "result": {
                "layoutParsingResults": [
                    {"markdown": {"text": "设函数 $f(x)=x^2$。"}},
                    {"markdown": {"text": "求 $f'(x)$。"}},
                ]
            }
        }
    ]

    assert extract_document_parsing_text(records) == (
        "设函数 $f(x)=x^2$。\n\n求 $f'(x)$。"
    )


def test_paddle_ocr_image_submits_polls_and_downloads_jsonl(tmp_path):
    image_path = tmp_path / "question.png"
    image_path.write_bytes(b"fake-image")
    submit_response = FakeResponse(
        payload={"code": 0, "data": {"jobId": "job-123"}}
    )
    status_response = FakeResponse(
        payload={
            "code": 0,
            "data": {
                "state": "done",
                "resultUrl": {"jsonUrl": "https://result.example/job-123.jsonl"},
            },
        }
    )
    result_response = FakeResponse(
        text=json.dumps(
            {"result": {"ocrResults": [{"prunedResult": {"rec_texts": ["题干"]}}]}}
        )
        + "\n"
    )

    with patch(
        "mathbank.paddle_ocr.requests.post", return_value=submit_response
    ) as mock_post, patch(
        "mathbank.paddle_ocr.requests.get",
        side_effect=[status_response, result_response],
    ) as mock_get:
        result = paddle_ocr_image(
            str(image_path),
            access_token="paddle-token",
            base_url="https://paddle.example",
            model="PP-OCRv6",
            sleep_fn=lambda _: None,
        )

    assert result == "题干"
    assert mock_post.call_args.args[0] == (
        "https://paddle.example/api/v2/ocr/jobs"
    )
    assert mock_post.call_args.kwargs["headers"] == {
        "Authorization": "Bearer paddle-token"
    }
    assert mock_post.call_args.kwargs["data"]["model"] == "PP-OCRv6"
    assert mock_post.call_args.kwargs["files"]["file"][0] == "question.png"
    assert mock_get.call_args_list[0].kwargs["headers"] == {
        "Authorization": "Bearer paddle-token"
    }
    assert "headers" not in mock_get.call_args_list[1].kwargs


def test_paddle_ocr_image_parses_vl_document_result(tmp_path):
    image_path = tmp_path / "question.png"
    image_path.write_bytes(b"fake-image")
    submit_response = FakeResponse(
        payload={"code": 0, "data": {"jobId": "vl-job-123"}}
    )
    status_response = FakeResponse(
        payload={
            "code": 0,
            "data": {
                "state": "done",
                "resultUrl": {"jsonUrl": "https://result.example/vl.jsonl"},
            },
        }
    )
    result_response = FakeResponse(
        text=json.dumps(
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "\n".join(
                                    ["题目：设 $f(x)=x^2$。", "求 $f'(x)$。"]
                                )
                            },
                            "prunedResult": {"ignored": "fallback"},
                        }
                    ]
                }
            }
        )
        + "\n"
    )

    with patch(
        "mathbank.paddle_ocr.requests.post", return_value=submit_response
    ) as mock_post, patch(
        "mathbank.paddle_ocr.requests.get",
        side_effect=[status_response, result_response],
    ):
        result = paddle_ocr_image(
            str(image_path),
            access_token="paddle-token",
            model="PaddleOCR-VL-1.6",
            sleep_fn=lambda _: None,
        )

    assert result == "题目：设 $f(x)=x^2$。\n求 $f'(x)$。"
    assert json.loads(mock_post.call_args.kwargs["data"]["optionalPayload"]) == {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }


def test_ocr_via_provider_routes_paddleocr_without_chat_completion(tmp_path):
    image_path = tmp_path / "question.png"
    image_path.write_bytes(b"fake-image")
    provider = resolve_ocr_provider(
        "paddleocr",
        {
            "PADDLEOCR_ACCESS_TOKEN": "paddle-token",
            "PADDLEOCR_BASE_URL": "https://paddle.example",
            "PADDLEOCR_MODEL": "PP-OCRv5",
        },
    )

    with patch("main.paddle_ocr_image", return_value="识别文本") as mock_ocr:
        assert ocr_via_provider(str(image_path), provider) == "识别文本"

    mock_ocr.assert_called_once_with(
        str(image_path),
        access_token="paddle-token",
        base_url="https://paddle.example",
        model="PP-OCRv5",
    )
