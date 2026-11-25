"""Shared JSON-safe schedule model used by the MPP and Excel readers.

Readers return the same top-level shape so the health-check engine can remain
format-agnostic. Missing source data stays None/False rather than being guessed.
"""
from __future__ import annotations

from typing import Any

TOP_LEVEL_KEYS = {
    "source", "project", "tasks", "data_availability", "parser_warnings", "task_count"
}
TASK_KEYS = {
    "id", "unique_id", "name", "wbs", "outline_number", "outline_level",
    "parent_unique_id", "is_summary", "is_milestone", "start", "finish",
    "duration", "percent_complete", "actual_start", "actual_finish",
    "actual_duration", "baseline_start", "baseline_finish", "baseline_duration",
    "constraint_type", "constraint_date", "deadline", "calendar_name",
    "predecessors", "assignments", "owner", "deliverable", "source_row_number", "source_fields",
}


def validate_schedule_payload(payload: dict[str, Any]) -> list[str]:
    """Return schema problems without treating optional data gaps as defects."""
    problems: list[str] = []
    missing_top = TOP_LEVEL_KEYS - set(payload)
    if missing_top:
        problems.append(f"Missing top-level keys: {', '.join(sorted(missing_top))}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        problems.append("tasks must be a list")
        return problems
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"Task at index {index} is not an object")
            continue
        missing_task = TASK_KEYS - set(task)
        if missing_task:
            problems.append(
                f"Task at index {index} is missing keys: {', '.join(sorted(missing_task))}"
            )
        if not isinstance(task.get("predecessors", []), list):
            problems.append(f"Task at index {index}: predecessors must be a list")
        if not isinstance(task.get("assignments", []), list):
            problems.append(f"Task at index {index}: assignments must be a list")
    if payload.get("task_count") != len(tasks):
        problems.append("task_count does not match the number of tasks")
    return problems
