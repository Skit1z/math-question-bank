"""结构化来源库（sources）与题目来源引用的 API 契约测试。"""

import pytest
from main import LOCAL_TOKEN
from mathbank.database import format_source_label


HEADERS = {"X-Local-Token": LOCAL_TOKEN}


def test_format_source_label_variants():
    assert format_source_label("880基础篇", "第二章·选择", 10) == "880基础篇·第二章·选择(10)"
    assert format_source_label("1991数一", "", 8) == "1991数一(8)"
    assert format_source_label("1991数一", None, None) == "1991数一"
    assert format_source_label("", "第二章", 3) == "第二章(3)"
    assert format_source_label("", "", None) == ""


def test_sources_crud_lifecycle(client):
    response = client.get("/api/sources")
    assert response.status_code == 200
    assert response.json() == {"sources": []}

    response = client.post(
        "/api/sources",
        data={"name": "880基础篇", "series": "李林880", "note": "高数篇基础题"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    created = response.json()["source"]
    assert created["name"] == "880基础篇"
    assert created["series"] == "李林880"
    source_id = created["id"]

    # 重名冲突
    response = client.post("/api/sources", data={"name": "880基础篇"}, headers=HEADERS)
    assert response.status_code == 409

    # 修改名称与系列
    response = client.put(
        f"/api/sources/{source_id}",
        data={"name": "880基础", "series": "李林880", "note": ""},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["source"]["name"] == "880基础"

    # 修改成其他已有名称冲突
    client.post("/api/sources", data={"name": "1991数一", "series": "考研数学真题"}, headers=HEADERS)
    response = client.put(
        f"/api/sources/{source_id}", data={"name": "1991数一"}, headers=HEADERS
    )
    assert response.status_code == 409

    # 列表带引用计数
    response = client.get("/api/sources")
    names = {item["name"]: item for item in response.json()["sources"]}
    assert set(names) == {"880基础", "1991数一"}
    assert all(item["usage_count"] == 0 for item in names.values())

    # 无引用可删除
    response = client.delete(f"/api/sources/{source_id}", headers=HEADERS)
    assert response.status_code == 200
    response = client.get("/api/sources")
    assert [item["name"] for item in response.json()["sources"]] == ["1991数一"]

    # 删除不存在
    response = client.delete(f"/api/sources/{source_id}", headers=HEADERS)
    assert response.status_code == 404


def test_question_with_structured_source_and_filters(client):
    response = client.post(
        "/api/sources",
        data={"name": "880基础篇", "series": "李林880"},
        headers=HEADERS,
    )
    source_id = response.json()["source"]["id"]
    client.post(
        "/api/sources",
        data={"name": "1991数一", "series": "考研数学真题"},
        headers=HEADERS,
    )

    payload = {
        "content": "设函数 $f(x)$ 在 $(-\\infty,+\\infty)$ 内单调，则（ ）",
        "question_type": "single_choice",
        "exam_track": "数学一",
        "subject": "高等数学",
        "topic": "函数、极限与连续",
        "difficulty": "basic",
        "source_id": str(source_id),
        "source_number": "10",
        "source_scope": "第二章·选择",
        "answer_markdown": "**答案** B",
        "related_question_id": "",
        "image_paths": "[]",
    }
    response = client.post("/api/questions", data=payload, headers=HEADERS)
    assert response.status_code == 200
    question = response.json()["question"]
    assert question["source_id"] == source_id
    assert question["source_number"] == 10
    assert question["source_scope"] == "第二章·选择"
    assert question["source_label"] == "880基础篇·第二章·选择(10)"
    assert "source" not in question
    question_id = question["id"]

    # 另一道无来源题 + 一道 1991数一 的题
    no_source_payload = dict(payload)
    no_source_payload.pop("source_id")
    no_source_payload.pop("source_number")
    no_source_payload.pop("source_scope")
    client.post("/api/questions", data=no_source_payload, headers=HEADERS)

    paper_payload = dict(no_source_payload, source_name="1991数一", source_number="8")
    response = client.post("/api/questions", data=paper_payload, headers=HEADERS)
    assert response.status_code == 200
    # source_name 走幂等查找，不新建重复来源
    paper_question = response.json()["question"]
    assert paper_question["source_label"] == "1991数一(8)"
    response = client.get("/api/sources")
    assert len(response.json()["sources"]) == 2

    # 详情
    response = client.get(f"/api/questions/{question_id}")
    assert response.json()["source_label"] == "880基础篇·第二章·选择(10)"

    # 筛选：按 source_id
    response = client.get(f"/api/questions?source_id={source_id}")
    assert [item["id"] for item in response.json()] == [question_id]
    # 筛选：按系列
    response = client.get("/api/questions?series=考研数学真题")
    assert len(response.json()) == 1
    # 搜索命中来源名
    response = client.get("/api/questions?q=880基础篇")
    assert [item["id"] for item in response.json()] == [question_id]
    # 分页响应同样携带结构化来源
    response = client.get("/api/questions?page=1&page_size=10&source_id=%d" % source_id)
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["source_label"].startswith("880基础篇")

    # 更新来源引用
    response = client.put(
        f"/api/questions/{question_id}",
        data=dict(no_source_payload, source_name="1991数一", source_number="3", source_scope=""),
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["question"]["source_label"] == "1991数一(3)"

    # 非法 source_id
    response = client.post(
        "/api/questions", data=dict(no_source_payload, source_id="99999"), headers=HEADERS
    )
    assert response.status_code == 400


def test_delete_source_referenced_by_question_is_rejected(client):
    response = client.post("/api/sources", data={"name": "880综合篇"}, headers=HEADERS)
    source_id = response.json()["source"]["id"]
    payload = {
        "content": "题目",
        "question_type": "detailed_answer",
        "difficulty": "comprehensive",
        "source_id": str(source_id),
        "source_number": "14",
        "answer_markdown": "",
        "review": "",
        "tikz_code": "",
        "tags": "",
        "related_question_id": "",
        "image_paths": "[]",
    }
    response = client.post("/api/questions", data=payload, headers=HEADERS)
    assert response.status_code == 200
    question_id = response.json()["question"]["id"]

    response = client.delete(f"/api/sources/{source_id}", headers=HEADERS)
    assert response.status_code == 409
    assert "1 道题目引用" in response.json()["detail"]

    # 解除引用后可删除
    response = client.put(
        f"/api/questions/{question_id}",
        data=dict(payload, source_id=""),
        headers=HEADERS,
    )
    assert response.status_code == 200
    response = client.delete(f"/api/sources/{source_id}", headers=HEADERS)
    assert response.status_code == 200
