from mathbank.database import Paper, Question, Source, format_source_label

def test_question_crud_operations(db_session):
    # 1. Create Question（来源为独立实体，先建来源再引用）
    source = Source(name="2024考研数学真题", series="考研数学真题")
    db_session.add(source)
    db_session.flush()
    q = Question(
        content="设集合 $A = \\{1, 2\\}$, $B = \\{2, 3\\}$，则 $A \\cup B = $",
        question_type="single_choice",
        exam_track="数学一",
        subject="第一章 集合与常用逻辑用语",
        topic="集合的并集",
        difficulty="easy",
        source_id=source.id,
        source_number=8,
        source_scope="",
        answer_markdown="$\\{1, 2, 3\\}$",
        review="这是一道基础的集合并集题目",
        association_group_id="group_123"
    )
    # Set image paths
    q.image_paths = ["/static/uploads/test_img.png"]
    
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    
    assert q.id is not None
    assert q.question_type == "single_choice"
    assert q.exam_track == "数学一"
    assert q.image_paths == ["/static/uploads/test_img.png"]
    
    # Test dictionary formats
    d = q.to_dict()
    assert d["id"] == q.id
    assert d["content"] == q.content
    assert "answer_markdown" in d
    assert d["answer_markdown"] == "$\\{1, 2, 3\\}$"
    assert d["has_answer"] is True
    assert d["review"] == "这是一道基础的集合并集题目"
    assert d["association_group_id"] == "group_123"
    
    s = q.to_summary_dict()
    assert s["id"] == q.id
    assert "answer_markdown" not in s  # Summary should not leak answers
    assert s["has_answer"] is True
    
    # 2. Read / Query Question
    retrieved = db_session.query(Question).filter_by(id=q.id).first()
    assert retrieved is not None
    assert retrieved.source.name == "2024考研数学真题"
    assert format_source_label(retrieved.source.name, retrieved.source_scope, retrieved.source_number) == "2024考研数学真题(8)"
    
    # 3. Update Question
    retrieved.difficulty = "standard"
    db_session.commit()
    
    updated = db_session.query(Question).filter_by(id=q.id).first()
    assert updated.difficulty == "standard"
    
    # 4. Delete Question
    db_session.delete(updated)
    db_session.commit()
    
    deleted = db_session.query(Question).filter_by(id=q.id).first()
    assert deleted is None


def test_json_model_fields_fail_closed_on_wrong_json_shapes():
    question = Question(content="shape test")
    for malformed_shape in ('{}', '"/static/uploads/a.png"', "null", "123"):
        question._image_paths = malformed_shape
        assert question.image_paths == []

    paper = Paper(title="shape test", metadata_json="[]")
    payload = paper.to_dict()
    assert payload["show_secret"] is True
    assert payload["show_notice"] is True

    paper.metadata_json = '"not-an-object"'
    payload = paper.to_dict()
    assert payload["show_secret"] is True
    assert payload["show_notice"] is True
