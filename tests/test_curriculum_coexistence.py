from main import LOCAL_TOKEN
from mathbank.database import QuestionCurriculum, init_db
from mathbank.curriculums import KAOYAN_VERSION


def test_kaoyan_classification_mirror_is_single_version(client, db_session):
    init_db()
    headers = {"X-Local-Token": LOCAL_TOKEN}
    payload = {
        "content": "设函数 $f(x)=x^2$，求 $f'(1)$。",
        "question_type": "detailed_answer",
        "exam_track": "数学一",
        "subject": "高等数学",
        "topic": "一元函数微分学",
        "difficulty": "standard",
        "source": "考研数学真题",
        "answer_markdown": "$f'(x)=2x$，故 $f'(1)=2$。",
        "image_paths": "[]",
    }

    response = client.post("/api/questions", data=payload, headers=headers)
    assert response.status_code == 200
    question_id = response.json()["question"]["id"]

    mapping = db_session.query(QuestionCurriculum).filter_by(
        question_id=question_id, version_code=KAOYAN_VERSION
    ).one()
    assert mapping.exam_track == "数学一"
    assert mapping.subject == "高等数学"
    assert mapping.topic == "一元函数微分学"
    assert db_session.query(QuestionCurriculum).filter(
        QuestionCurriculum.question_id == question_id,
        QuestionCurriculum.version_code != KAOYAN_VERSION,
    ).count() == 0

    metadata = client.get("/api/config/metadata").json()
    metadata["curriculum_version"] = "A"
    rejected = client.post("/api/config/metadata", json=metadata, headers=headers)
    assert rejected.status_code == 400
