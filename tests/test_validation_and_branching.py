from __future__ import annotations

import pytest

from app import questions as qbank
from tests.conftest import FULL_STEP_ANSWERS


def test_missing_required_field_raises():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(1, {"brand_legal_name": "Acme"}, {})
    assert "brand_display_name" in exc_info.value.errors


def test_text_length_bounds_enforced():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(1, {"brand_one_liner": "hi"}, {})
    assert "brand_one_liner" in exc_info.value.errors


def test_url_format_enforced():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(1, {"brand_website": "not-a-url"}, {})
    assert "brand_website" in exc_info.value.errors


def test_valid_url_passes():
    cleaned = qbank.validate_step_answers(
        1,
        {
            "brand_website": "https://example.com",
            "brand_legal_name": "Acme Pvt Ltd",
            "brand_display_name": "Acme",
            "brand_industry": "saas",
            "brand_one_liner": "We make marketing automation simple.",
            "brand_mission": "Helping teams grow through consistent marketing execution every day.",
            "brand_values": ["trust", "speed"],
            "brand_has_style_guide": False,
        },
        {},
    )
    assert cleaned["brand_website"] == "https://example.com"


def test_number_out_of_range_rejected():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(3, {"audience_age_min": 5}, {})
    assert "audience_age_min" in exc_info.value.errors


def test_single_select_invalid_option_rejected():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(2, {"offer_type": "not_a_real_option"}, {})
    assert "offer_type" in exc_info.value.errors


def test_multi_select_below_min_select_rejected():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(3, {"audience_genders": []}, {})
    assert "audience_genders" in exc_info.value.errors


def test_multi_select_above_max_select_rejected():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(
            1, {"brand_values": ["trust", "speed", "luxury", "community", "playfulness", "innovation"]}, {}
        )
    assert "brand_values" in exc_info.value.errors


def test_boolean_field_accepts_bool_and_string():
    payload = dict(FULL_STEP_ANSWERS[2])
    payload["offer_has_seasonality"] = True
    payload["offer_peak_seasons"] = ["q4", "festive"]
    cleaned = qbank.validate_step_answers(2, payload, {})
    assert cleaned["offer_has_seasonality"] is True


# -- branching --------------------------------------------------------------


def test_conditional_question_hidden_when_condition_not_met():
    answers = {"brand_industry": "saas"}
    visible = qbank.visible_questions_for_step(1, answers)
    ids = {q["id"] for q in visible}
    assert "brand_industry_other" not in ids


def test_conditional_question_shown_when_condition_met():
    answers = {"brand_industry": "other"}
    visible = qbank.visible_questions_for_step(1, answers)
    ids = {q["id"] for q in visible}
    assert "brand_industry_other" in ids


def test_hidden_conditional_question_not_required():
    # brand_has_style_guide=False -> brand_style_guide_url must not be required
    cleaned = qbank.validate_step_answers(
        1,
        {
            "brand_legal_name": "Acme Pvt Ltd",
            "brand_display_name": "Acme",
            "brand_website": "https://acme.example.com",
            "brand_industry": "saas",
            "brand_one_liner": "We make marketing automation simple for growing teams.",
            "brand_mission": "Helping teams grow through consistent marketing execution every day.",
            "brand_values": ["trust", "speed"],
            "brand_has_style_guide": False,
        },
        {},
    )
    assert "brand_style_guide_url" not in cleaned


def test_visible_conditional_question_required_when_shown():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(
            1,
            {
                "brand_legal_name": "Acme Pvt Ltd",
                "brand_display_name": "Acme",
                "brand_website": "https://acme.example.com",
                "brand_industry": "saas",
                "brand_one_liner": "We make marketing automation simple for growing teams.",
                "brand_mission": "Helping teams grow through consistent marketing execution every day.",
                "brand_values": ["trust", "speed"],
                "brand_has_style_guide": True,
                # brand_style_guide_url intentionally omitted
            },
            {},
        )
    assert "brand_style_guide_url" in exc_info.value.errors


def test_answer_for_non_applicable_question_is_rejected():
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(
            1,
            {
                "brand_legal_name": "Acme Pvt Ltd",
                "brand_display_name": "Acme",
                "brand_website": "https://acme.example.com",
                "brand_industry": "saas",
                "brand_one_liner": "We make marketing automation simple for growing teams.",
                "brand_mission": "Helping teams grow through consistent marketing execution every day.",
                "brand_values": ["trust", "speed"],
                "brand_has_style_guide": False,
                "brand_style_guide_url": "https://example.com/guide.pdf",  # not applicable since False
            },
            {},
        )
    assert "brand_style_guide_url" in exc_info.value.errors


def test_multi_select_includes_operator_branching():
    visible_with = qbank.visible_questions_for_step(3, {"audience_geographies": ["other"]})
    visible_without = qbank.visible_questions_for_step(3, {"audience_geographies": ["india_metro"]})
    assert "audience_geo_other" in {q["id"] for q in visible_with}
    assert "audience_geo_other" not in {q["id"] for q in visible_without}


def test_gte_question_cross_field_validation():
    payload = dict(FULL_STEP_ANSWERS[3])
    payload["audience_age_min"] = 30
    payload["audience_age_max"] = 20  # less than min -> invalid
    with pytest.raises(qbank.ValidationError) as exc_info:
        qbank.validate_step_answers(3, payload, {})
    assert "audience_age_max" in exc_info.value.errors


def test_step_progress_accounts_for_branching_visibility():
    # Without a secondary segment, that step's required-count should be lower.
    answers = {"audience_has_secondary_segment": False}
    progress = qbank.step_progress(answers)
    step3 = next(s for s in progress["steps"] if s["step"] == 3)
    ids_required = {
        q["id"] for q in qbank.visible_questions_for_step(3, answers) if q.get("required")
    }
    assert "audience_secondary_segment" not in ids_required
