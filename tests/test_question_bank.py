from __future__ import annotations

from app import questions as qbank


def test_total_question_count_is_87():
    assert len(qbank.all_questions()) == 87


def test_exactly_seven_steps_defined():
    assert len(qbank.steps()) == 7
    assert {s["id"] for s in qbank.steps()} == set(range(1, 8))


def test_every_question_maps_to_a_valid_step():
    valid_steps = {s["id"] for s in qbank.steps()}
    for q in qbank.all_questions():
        assert q["step"] in valid_steps, f"{q['id']} has invalid step {q['step']}"


def test_no_duplicate_question_ids():
    ids = [q["id"] for q in qbank.all_questions()]
    assert len(ids) == len(set(ids))


def test_every_question_has_a_type_and_label():
    for q in qbank.all_questions():
        assert q.get("label"), q["id"]
        assert q.get("type"), q["id"]


def test_show_if_references_resolve_to_real_questions():
    ids = {q["id"] for q in qbank.all_questions()}
    for q in qbank.all_questions():
        cond = q.get("show_if")
        if cond:
            assert cond["question_id"] in ids, f"{q['id']} depends on missing {cond['question_id']}"


def test_at_least_one_branching_question_per_several_steps():
    # Confirms the questionnaire genuinely branches, not just validates.
    branching_steps = {q["step"] for q in qbank.all_questions() if q.get("show_if")}
    assert len(branching_steps) >= 5


def test_questions_for_step_returns_only_that_step():
    for step_id in range(1, 8):
        for q in qbank.questions_for_step(step_id):
            assert q["step"] == step_id


def test_question_counts_sum_to_87_across_steps():
    total = sum(len(qbank.questions_for_step(s)) for s in range(1, 8))
    assert total == 87
