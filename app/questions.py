"""Loads and indexes the 87-question onboarding definition."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

QUESTIONS_PATH = Path(__file__).resolve().parent / "data" / "questions.json"


class QuestionBankError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def load_definition() -> dict[str, Any]:
    data = json.loads(QUESTIONS_PATH.read_text())
    _validate_definition(data)
    return data


def _validate_definition(data: dict[str, Any]) -> None:
    steps = {s["id"] for s in data["steps"]}
    questions = data["questions"]
    seen_ids: set[str] = set()
    for q in questions:
        if q["id"] in seen_ids:
            raise QuestionBankError(f"duplicate question id: {q['id']}")
        seen_ids.add(q["id"])
        if q["step"] not in steps:
            raise QuestionBankError(f"question {q['id']} assigned to unknown step {q['step']}")
        show_if = q.get("show_if")
        if show_if and show_if["question_id"] not in seen_ids and show_if["question_id"] not in {
            x["id"] for x in questions
        }:
            raise QuestionBankError(f"question {q['id']} depends on unknown question {show_if['question_id']}")


def all_questions() -> list[dict[str, Any]]:
    return load_definition()["questions"]


def steps() -> list[dict[str, Any]]:
    return load_definition()["steps"]


def questionnaire_version() -> str:
    return load_definition()["version"]


def questions_for_step(step: int) -> list[dict[str, Any]]:
    return sorted((q for q in all_questions() if q["step"] == step), key=lambda q: q["order"])


@lru_cache(maxsize=1)
def _by_id() -> dict[str, dict[str, Any]]:
    return {q["id"]: q for q in all_questions()}


def question_by_id(qid: str) -> dict[str, Any] | None:
    return _by_id().get(qid)


def is_visible(question: dict[str, Any], answers: dict[str, Any]) -> bool:
    """Evaluate a question's `show_if` branching condition against prior answers."""
    condition = question.get("show_if")
    if not condition:
        return True
    dep_value = answers.get(condition["question_id"])
    op = condition["op"]
    target = condition.get("value")

    if op == "truthy":
        return bool(dep_value)
    if op == "falsy":
        return not bool(dep_value)
    if op == "eq":
        return dep_value == target
    if op == "ne":
        return dep_value != target
    if op == "includes":
        return isinstance(dep_value, list) and target in dep_value
    if op == "excludes":
        return not (isinstance(dep_value, list) and target in dep_value)
    raise QuestionBankError(f"unknown show_if operator: {op}")


def visible_questions_for_step(step: int, answers: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in questions_for_step(step) if is_visible(q, answers)]


class ValidationError(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} validation error(s)")


def validate_step_answers(step: int, answers: dict[str, Any], full_answers: dict[str, Any]) -> dict[str, Any]:
    """Validate the answers supplied for one step.

    `full_answers` is the accumulated answer set across all steps so far (used
    to resolve branching conditions and cross-question comparisons such as
    `gte_question`). Returns the merged, cleaned per-step answers on success or
    raises ValidationError with a field -> message map.
    """
    merged_context = {**full_answers, **answers}
    visible = visible_questions_for_step(step, merged_context)
    visible_ids = {q["id"] for q in visible}
    errors: dict[str, str] = {}
    cleaned: dict[str, Any] = {}

    for q in visible:
        qid = q["id"]
        present = qid in answers and answers[qid] not in (None, "")
        if not present:
            if q.get("required"):
                errors[qid] = "This question is required."
            continue
        value = answers[qid]
        try:
            cleaned[qid] = _validate_value(q, value, merged_context)
        except ValueError as exc:
            errors[qid] = str(exc)

    # Reject answers submitted for questions that are not visible/known for this step.
    for qid in answers:
        if qid not in visible_ids and qid not in errors:
            question = question_by_id(qid)
            if question is None:
                errors[qid] = "Unknown question id."
            elif question["step"] != step:
                errors[qid] = f"Question belongs to step {question['step']}, not {step}."
            else:
                errors[qid] = "This question is not applicable given earlier answers."

    if errors:
        raise ValidationError(errors)
    return cleaned


def _validate_value(q: dict[str, Any], value: Any, context: dict[str, Any]) -> Any:
    qtype = q["type"]

    if qtype == "text" or qtype == "textarea" or qtype == "url" or qtype == "email":
        if not isinstance(value, str):
            raise ValueError("Expected a string.")
        value = value.strip()
        min_len = q.get("min_length")
        max_len = q.get("max_length")
        if min_len and len(value) < min_len:
            raise ValueError(f"Must be at least {min_len} characters.")
        if max_len and len(value) > max_len:
            raise ValueError(f"Must be at most {max_len} characters.")
        pattern = q.get("pattern")
        if pattern and not re.match(pattern, value):
            raise ValueError(q.get("pattern_message", "Invalid format."))
        if qtype == "url" and not re.match(r"^https?://[^\s]+\.[^\s]+", value):
            raise ValueError("Must be a valid http(s) URL.")
        if qtype == "email" and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value):
            raise ValueError("Must be a valid email address.")
        return value

    if qtype == "number" or qtype == "scale":
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            raise ValueError("Expected a number.")
        if qtype == "scale" and numeric != int(numeric):
            raise ValueError("Must be a whole number.")
        numeric = int(numeric) if numeric == int(numeric) else numeric
        if q.get("min") is not None and numeric < q["min"]:
            raise ValueError(f"Must be at least {q['min']}.")
        if q.get("max") is not None and numeric > q["max"]:
            raise ValueError(f"Must be at most {q['max']}.")
        gte_q = q.get("gte_question")
        if gte_q and gte_q in context:
            try:
                other = float(context[gte_q])
            except (TypeError, ValueError):
                other = None
            if other is not None and numeric < other:
                raise ValueError(f"Must be greater than or equal to '{gte_q}'.")
        return numeric

    if qtype == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise ValueError("Expected a boolean.")

    if qtype == "single_select":
        valid = {o["value"] for o in q["options"]}
        if value not in valid:
            raise ValueError(f"Must be one of: {', '.join(sorted(valid))}.")
        return value

    if qtype == "multi_select":
        if not isinstance(value, list):
            raise ValueError("Expected a list of values.")
        valid = {o["value"] for o in q["options"]}
        invalid = [v for v in value if v not in valid]
        if invalid:
            raise ValueError(f"Invalid option(s): {', '.join(map(str, invalid))}.")
        if len(set(value)) != len(value):
            raise ValueError("Duplicate selections are not allowed.")
        min_sel = q.get("min_select")
        max_sel = q.get("max_select")
        if min_sel and len(value) < min_sel:
            raise ValueError(f"Select at least {min_sel} option(s).")
        if max_sel and len(value) > max_sel:
            raise ValueError(f"Select at most {max_sel} option(s).")
        return value

    raise ValueError(f"Unsupported question type: {qtype}")


def step_progress(answers: dict[str, Any]) -> dict[str, Any]:
    """Compute per-step completion, accounting for branching visibility."""
    result = []
    for step in steps():
        visible = visible_questions_for_step(step["id"], answers)
        required = [q for q in visible if q.get("required")]
        answered_required = [q for q in required if answers.get(q["id"]) not in (None, "", [])]
        result.append(
            {
                "step": step["id"],
                "key": step["key"],
                "title": step["title"],
                "visible_questions": len(visible),
                "required_questions": len(required),
                "answered_required": len(answered_required),
                "complete": len(answered_required) == len(required),
            }
        )
    return {
        "steps": result,
        "overall_percent": round(
            100
            * sum(s["answered_required"] for s in result)
            / max(1, sum(s["required_questions"] for s in result)),
            1,
        ),
    }
