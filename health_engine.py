"""Deterministic, findings-based project schedule health checks.

The engine is format agnostic: both MPP and Excel readers provide the same
canonical payload. It does not calculate an opaque overall score.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import re
from typing import Any

SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Info": 4}


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(text), datetime.min.time())
        except ValueError:
            return None


def _lag_number(lag: Any) -> float | None:
    if not isinstance(lag, dict):
        return None
    value = lag.get("value")
    if isinstance(value, (int, float)):
        return float(value)
    if value in (None, ""):
        return 0.0
    match = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)", str(value))
    return float(match.group(1)) if match else None


def _task_ref(task: dict[str, Any]) -> str:
    task_id = task.get("id")
    name = task.get("name") or "Unnamed task"
    row = task.get("source_row_number")
    if task_id not in (None, ""):
        return f"{task_id} — {name}"
    return f"Row {row} — {name}" if row else name


def _finding(rule_id: str, category: str, severity: str, title: str,
             description: str, tasks: list[dict[str, Any]], impact: str,
             recommendation: str, evidence: str | None = None,
             confidence: str = "High") -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "category": category,
        "severity": severity,
        "title": title,
        "description": description,
        "task_refs": [_task_ref(task) for task in tasks],
        "task_ids": [task.get("id") for task in tasks],
        "evidence": evidence or "; ".join(_task_ref(task) for task in tasks),
        "impact": impact,
        "recommended_action": recommendation,
        "data_confidence": confidence,
    }


def _cycle_nodes(task_ids: list[str], edges: dict[str, list[str]]) -> list[list[str]]:
    """Return unique directed cycles without recursion-depth limitations."""
    state: dict[str, int] = {}
    cycles: list[list[str]] = []
    signatures: set[tuple[str, ...]] = set()

    def record_cycle(path: list[str], repeated_node: str) -> None:
        start_index = path.index(repeated_node)
        cycle = path[start_index:] + [repeated_node]
        core = cycle[:-1]
        if not core:
            return
        rotations = [tuple(core[i:] + core[:i]) for i in range(len(core))]
        signature = min(rotations)
        if signature not in signatures:
            signatures.add(signature)
            cycles.append(cycle)

    for root in sorted(task_ids):
        if state.get(root, 0) != 0:
            continue
        state[root] = 1
        path = [root]
        positions = {root: 0}
        stack: list[tuple[str, Any]] = [(root, iter(sorted(set(edges.get(root, [])))))]
        while stack:
            node, neighbours = stack[-1]
            try:
                nxt = next(neighbours)
            except StopIteration:
                stack.pop()
                state[node] = 2
                positions.pop(node, None)
                path.pop()
                continue
            if state.get(nxt, 0) == 0:
                state[nxt] = 1
                positions[nxt] = len(path)
                path.append(nxt)
                stack.append((nxt, iter(sorted(set(edges.get(nxt, []))))))
            elif state.get(nxt) == 1 and nxt in positions:
                record_cycle(path, nxt)
    return cycles


def assess_schedule(payload: dict[str, Any], status_date: str | date | datetime | None = None) -> dict[str, Any]:
    """Assess a canonical schedule and return findings plus explicit limitations."""
    tasks = payload.get("tasks", [])
    findings: list[dict[str, Any]] = []
    limitations: list[str] = []
    task_rows = [task for task in tasks if isinstance(task, dict)]
    detail_tasks = [task for task in task_rows if not task.get("is_summary")]

    # Data-quality checks
    ids = [str(t.get("id")).strip() for t in task_rows if t.get("id") not in (None, "")]
    id_counts = Counter(ids)
    duplicate_ids = {task_id for task_id, count in id_counts.items() if count > 1}
    for task_id in sorted(duplicate_ids):
        affected = [t for t in task_rows if str(t.get("id")).strip() == task_id]
        findings.append(_finding(
            "DATA-001", "Data quality", "High", "Duplicate task identifier",
            f"Task ID {task_id} appears {len(affected)} times. Dependency references to this ID may be ambiguous.",
            affected, "Task-level traceability and dependency analysis may be unreliable.",
            "Assign a unique ID to every task and update all predecessor references.",
            f"Task ID {task_id}: {len(affected)} occurrences.", "High",
        ))

    for task in task_rows:
        if not task.get("name"):
            findings.append(_finding(
                "DATA-002", "Data quality", "High", "Task name is missing",
                "A schedule row has an ID or schedule data but no task name.", [task],
                "The activity cannot be clearly understood or discussed in governance reviews.",
                "Provide a short, action-oriented task name.",
            ))
        if task.get("id") in (None, ""):
            findings.append(_finding(
                "DATA-003", "Data quality", "Medium", "Task ID is missing",
                "A schedule row has no task identifier.", [task],
                "Dependencies and traceability may be ambiguous.",
                "Assign a unique task ID and use it consistently in predecessor links.",
            ))
        percent = task.get("percent_complete")
        if percent is not None and (percent < 0 or percent > 100):
            findings.append(_finding(
                "DATA-004", "Data quality", "High", "Progress value is outside 0–100%",
                f"The supplied progress value is {percent}.", [task],
                "Progress reporting may be misleading and roll-ups may be incorrect.",
                "Correct the percentage complete using a consistent 0–100 scale.",
            ))

    # Date and duration checks
    for task in detail_tasks:
        start = _dt(task.get("start"))
        finish = _dt(task.get("finish"))
        if start and finish and finish < start:
            findings.append(_finding(
                "DATE-001", "Schedule dates", "Critical", "Finish date is before start date",
                "The task has a finish date earlier than its start date.", [task],
                "The schedule timeline and downstream dependency calculations are invalid.",
                "Correct the task dates and recalculate the schedule.",
                f"Start: {task.get('start')}; Finish: {task.get('finish')}.",
            ))
        raw_start, raw_finish = task.get("start"), task.get("finish")
        missing_start = not start and raw_start in (None, "")
        missing_finish = not finish and raw_finish in (None, "")
        if not task.get("is_milestone") and (missing_start or missing_finish):
            missing = "start" if missing_start else "finish"
            findings.append(_finding(
                "DATE-002", "Schedule dates", "Medium", f"{missing.title()} date is missing",
                f"A non-summary activity has no {missing} date.", [task],
                "The activity cannot be placed reliably on the project timeline.",
                f"Provide the planned {missing} date or explicitly mark the task as not yet scheduled.",
                confidence="Medium",
            ))
        # A supplied but unparsable date is different from a genuinely blank field.
        for field_name in ("start", "finish", "actual_start", "actual_finish", "baseline_start", "baseline_finish", "constraint_date", "deadline"):
            raw_value = task.get(field_name)
            if raw_value not in (None, "") and _dt(raw_value) is None:
                findings.append(_finding(
                    "DATE-004", "Schedule dates", "High", f"{field_name.replace('_', ' ').title()} date is invalid",
                    f"The supplied value '{raw_value}' could not be interpreted as a valid date/time.", [task],
                    "Date-based sequencing, variance and status analysis may be incorrect or incomplete.",
                    f"Correct the {field_name.replace('_', ' ')} value using a valid date or date-time format.",
                    f"Field: {field_name}; value: {raw_value}.",
                ))

        duration = task.get("duration")
        duration_value = duration.get("value") if isinstance(duration, dict) else None
        if duration_value is not None and duration_value < 0:
            findings.append(_finding(
                "DATE-005", "Schedule dates", "High", "Task duration is negative",
                f"The supplied duration is {duration_value} {duration.get('units', '') if isinstance(duration, dict) else ''}.", [task],
                "A negative planned duration is not a valid work estimate and may distort schedule calculations.",
                "Correct the duration and confirm the task's planned start and finish dates.",
            ))
        if not task.get("is_milestone") and duration_value == 0:
            findings.append(_finding(
                "DATE-003", "Schedule dates", "Low", "Zero-duration activity is not marked as a milestone",
                "A non-milestone activity has a zero-day duration.", [task],
                "The schedule may not distinguish work activities from milestone events.",
                "Confirm whether this row should be modelled as a milestone or assigned a realistic duration.",
            ))
        actual_start = _dt(task.get("actual_start"))
        actual_finish = _dt(task.get("actual_finish"))
        if actual_start and actual_finish and actual_finish < actual_start:
            findings.append(_finding(
                "PROG-003", "Progress and status", "High", "Actual finish is before actual start",
                "The recorded actual finish date is earlier than the recorded actual start date.", [task],
                "Actual-progress reporting and earned-progress interpretation may be unreliable.",
                "Verify the actual dates against delivery records and correct the source plan.",
                f"Actual start: {task.get('actual_start')}; actual finish: {task.get('actual_finish')}."
            ))
        deadline = _dt(task.get("deadline"))
        if finish and deadline and finish.date() > deadline.date():
            delay_days = (finish.date() - deadline.date()).days
            severity = "High" if delay_days >= 10 else "Medium"
            findings.append(_finding(
                "DATE-006", "Schedule dates", severity, "Forecast finish is later than the task deadline",
                f"The current finish is {delay_days} calendar day(s) later than the recorded deadline.", [task],
                "The task is forecast to miss its stated target date, which may affect contractual or governance commitments.",
                "Confirm whether the deadline remains valid, assess downstream impact and agree an action or approved change.",
                f"Current finish: {task.get('finish')}; deadline: {task.get('deadline')}; variance: +{delay_days} calendar days.",
            ))
        constraint_type = str(task.get("constraint_type") or "").strip().upper()
        if constraint_type in {"MUST_START_ON", "MUST_FINISH_ON"}:
            findings.append(_finding(
                "LOGIC-006", "Constraints", "Medium", "Task uses a hard date constraint",
                f"The task is restricted by the {constraint_type.replace('_', ' ')} constraint.", [task],
                "Hard constraints can restrict schedule flexibility and may mask dependency-driven dates if used unnecessarily.",
                "Confirm the constraint is required by a genuine external commitment and that the dependency network remains credible.",
                f"Constraint type: {constraint_type}; constraint date: {task.get('constraint_date') or 'not supplied'}.",
                "Medium",
            ))
        if task.get("is_milestone") and duration_value not in (None, 0):
            findings.append(_finding(
                "MILE-002", "Milestones", "Medium", "Milestone has a non-zero duration",
                f"The milestone duration is {duration_value} {duration.get('units', '')}.", [task],
                "Milestone reporting may be inconsistent with the schedule's event-based control points.",
                "Set the milestone duration to zero or change the task classification.",
            ))

    # Dependency integrity and graph analysis
    valid_ids = {str(t.get("id")).strip() for t in task_rows if t.get("id") not in (None, "")}
    edges: dict[str, list[str]] = defaultdict(list)
    unique_ids = valid_ids - duplicate_ids
    task_by_id: dict[str, dict[str, Any]] = {
        str(t.get("id")).strip(): t for t in task_rows
        if t.get("id") not in (None, "") and str(t.get("id")).strip() in unique_ids
    }

    for successor in detail_tasks:
        successor_id = str(successor.get("id")).strip() if successor.get("id") not in (None, "") else None
        for relation in successor.get("predecessors", []) or []:
            predecessor_id = str(relation.get("predecessor_id")).strip() if relation.get("predecessor_id") not in (None, "") else None
            if not predecessor_id:
                continue
            if predecessor_id not in valid_ids:
                findings.append(_finding(
                    "LOGIC-001", "Dependencies", "High", "Predecessor reference does not exist",
                    f"Task {successor_id or _task_ref(successor)} references predecessor ID {predecessor_id}, which is not in the plan.",
                    [successor], "The dependency chain is broken and schedule logic may not calculate correctly.",
                    "Correct the predecessor ID or restore the missing activity.",
                    f"Predecessor reference: {predecessor_id}.",
                ))
            elif predecessor_id in duplicate_ids:
                findings.append(_finding(
                    "LOGIC-007", "Dependencies", "High", "Predecessor reference is ambiguous",
                    f"Task {successor_id or _task_ref(successor)} references predecessor ID {predecessor_id}, which is used by more than one task.",
                    [successor], "The relationship cannot be resolved to one predecessor activity with confidence.",
                    "Assign unique task IDs and update the predecessor reference to the intended activity.",
                    f"Ambiguous predecessor ID: {predecessor_id}.", "Medium",
                ))
            elif successor_id and successor_id == predecessor_id:
                findings.append(_finding(
                    "LOGIC-002", "Dependencies", "Critical", "Task depends on itself",
                    f"Task {successor_id} has a self-referencing dependency.", [successor],
                    "The task logic contains an impossible relationship.",
                    "Remove the self-link and reconnect the task to its genuine predecessor.",
                ))
            elif successor_id and successor_id in unique_ids:
                edges[predecessor_id].append(successor_id)

            relation_type = relation.get("type")
            supported_types = {"FINISH_START", "START_START", "FINISH_FINISH", "START_FINISH"}
            if relation_type is not None and str(relation_type).upper() not in supported_types:
                findings.append(_finding(
                    "LOGIC-008", "Dependencies", "Medium", "Dependency relationship type is unsupported",
                    f"The relationship type '{relation_type}' is not one of FS, SS, FF or SF.", [successor],
                    "The dependency may be interpreted incorrectly by downstream schedule tools or reviewers.",
                    "Confirm the predecessor relationship and map it to Finish-to-Start, Start-to-Start, Finish-to-Finish or Start-to-Finish.",
                    confidence="Medium",
                ))

            if relation.get("parse_warning"):
                findings.append(_finding(
                    "LOGIC-005", "Dependencies", "Medium", "Predecessor notation could not be interpreted",
                    relation["parse_warning"], [successor],
                    "The dependency may not be included correctly in schedule-logic analysis.",
                    "Correct the predecessor notation to a supported task ID and relationship type (FS, SS, FF or SF), with optional lag/lead.",
                    confidence="Medium",
                ))

            lag_number = _lag_number(relation.get("lag"))
            if lag_number is not None and lag_number < 0:
                findings.append(_finding(
                    "LOGIC-003", "Dependencies", "Medium", "Dependency uses lead time",
                    f"The dependency has a negative lag ({relation.get('lag', {}).get('display') if isinstance(relation.get('lag'), dict) else relation.get('lag')}).",
                    [successor], "Lead relationships can allow successor work to start before predecessor completion and may increase coordination risk.",
                    "Confirm that the lead is intentional, justified and understood by the delivery team.",
                    confidence="Medium",
                ))

    tasks_without_ids = any(t.get("id") in (None, "") for t in task_rows)
    if duplicate_ids or tasks_without_ids:
        limitations.append("Circular-dependency detection only covers tasks with unique IDs; links involving missing or duplicate IDs are excluded from the dependency graph.")
    if unique_ids:
        cycles = _cycle_nodes(sorted(unique_ids), edges)
        for cycle in cycles:
            cycle_tasks = [task_by_id[node] for node in cycle[:-1] if node in task_by_id]
            findings.append(_finding(
                "LOGIC-004", "Dependencies", "Critical", "Circular dependency detected",
                "A closed dependency loop exists: " + " → ".join(cycle) + ".",
                cycle_tasks, "The tasks form a circular logic chain and the schedule cannot be sequenced reliably.",
                "Break the loop by correcting at least one predecessor link, then recalculate the schedule.",
                "Dependency cycle: " + " → ".join(cycle),
            ))

    # Milestone gate: an end-of-project milestone without an incoming link is suspect.
    start_dates = [(t, _dt(t.get("start"))) for t in detail_tasks if _dt(t.get("start"))]
    earliest_start = min((dt for _, dt in start_dates), default=None)
    for task in detail_tasks:
        if not task.get("is_milestone"):
            continue
        milestone_finish = _dt(task.get("finish")) or _dt(task.get("start"))
        if milestone_finish is None:
            findings.append(_finding(
                "MILE-003", "Milestones", "Medium", "Milestone has no planned date",
                "The milestone is marked as a milestone but has neither a usable start nor finish date.", [task],
                "The project control point cannot be placed on the schedule timeline.",
                "Assign the milestone's planned date and confirm its predecessor gate.",
            ))
        if not task.get("predecessors") and milestone_finish and earliest_start and milestone_finish > earliest_start:
            findings.append(_finding(
                "MILE-001", "Milestones", "High", "Milestone has no predecessor",
                "A milestone occurring after project work has started is not linked to any predecessor activity.",
                [task], "The milestone may not be supported by the work required to achieve it.",
                "Link the milestone to the activities or approval gate that must be completed before it can be achieved.",
                f"Milestone date: {task.get('finish') or task.get('start')}; no predecessor links found.",
            ))

    # Baseline variance: only assess when baseline dates are actually supplied.
    if payload.get("data_availability", {}).get("baselines"):
        for task in detail_tasks:
            current_finish = _dt(task.get("finish"))
            baseline_finish = _dt(task.get("baseline_finish"))
            if current_finish and baseline_finish and current_finish > baseline_finish:
                slip_days = (current_finish.date() - baseline_finish.date()).days
                severity = "High" if slip_days >= 10 else "Medium"
                findings.append(_finding(
                    "BASE-001", "Baseline and variance", severity, "Forecast finish is later than baseline",
                    f"The current finish is {slip_days} calendar day(s) later than the baseline finish.", [task],
                    "The activity is forecast to miss its approved baseline finish, potentially affecting downstream milestones.",
                    "Confirm the forecast, assess downstream impact and document an approved recovery or rebaseline decision.",
                    f"Baseline finish: {task.get('baseline_finish')}; current finish: {task.get('finish')}; variance: +{slip_days} calendar days.",
                ))
        baseline_coverage = sum(1 for task in detail_tasks if task.get("baseline_start") or task.get("baseline_finish"))
        if 0 < baseline_coverage < len(detail_tasks):
            limitations.append(f"Baseline information is partial ({baseline_coverage} of {len(detail_tasks)} detailed tasks); variance findings cover only tasks with the required baseline and current dates.")
        for task in detail_tasks:
            current_start = _dt(task.get("start"))
            baseline_start = _dt(task.get("baseline_start"))
            if current_start and baseline_start and current_start > baseline_start:
                slip_days = (current_start.date() - baseline_start.date()).days
                if slip_days > 0:
                    severity = "High" if slip_days >= 10 else "Medium"
                    findings.append(_finding(
                        "BASE-002", "Baseline and variance", severity, "Forecast start is later than baseline",
                        f"The current start is {slip_days} calendar day(s) later than the baseline start.", [task],
                        "A delayed activity start may compress downstream work or move dependent milestones.",
                        "Confirm the revised start, assess downstream effects and agree recovery or rebaseline actions.",
                        f"Baseline start: {task.get('baseline_start')}; current start: {task.get('start')}; variance: +{slip_days} calendar days.",
                    ))
    else:
        limitations.append("Baseline variance was not assessed because no baseline dates were supplied in the source plan.")

    # Overdue analysis is deliberately gated by an explicit status date.
    check_date = _dt(status_date) if status_date is not None else _dt(payload.get("project", {}).get("status_date"))
    if check_date:
        for task in detail_tasks:
            finish = _dt(task.get("finish"))
            percent = task.get("percent_complete")
            if finish and finish.date() < check_date.date() and (percent is None or percent < 100):
                days_late = (check_date.date() - finish.date()).days
                findings.append(_finding(
                    "PROG-001", "Progress and status", "High", "Incomplete task is past its planned finish",
                    f"The task's planned finish was {days_late} calendar day(s) before the status date, but progress is {percent if percent is not None else 'not supplied'}%.",
                    [task], "The task is overdue and may affect dependent work or milestones.",
                    "Confirm the current forecast, remaining effort, blockers and recovery actions.",
                    f"Planned finish: {task.get('finish')}; status date: {check_date.date().isoformat()}.",
                ))
    else:
        limitations.append("Overdue-task checks were not run because the plan contains no status date and none was provided for this assessment.")

    # Progress/actual consistency, only where actual dates exist.
    if payload.get("data_availability", {}).get("actual_dates"):
        for task in detail_tasks:
            percent = task.get("percent_complete")
            if percent is not None and percent >= 100 and not task.get("actual_finish"):
                findings.append(_finding(
                    "PROG-002", "Progress and status", "Medium", "Completed task has no actual finish date",
                    "The task is marked 100% complete but has no actual finish date.", [task],
                    "Completion evidence and schedule progress may not reconcile.",
                    "Record the actual finish date or correct the progress percentage.",
                    confidence="Medium",
                ))

    # Resource conflicts: names + dates support potential contention only.
    if payload.get("data_availability", {}).get("resource_assignments"):
        resource_tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for task in detail_tasks:
            for assignment in task.get("assignments", []) or []:
                name = (assignment.get("resource_name") or "").strip()
                if name and _dt(task.get("start")) and _dt(task.get("finish")):
                    resource_tasks[name.casefold()].append(task)
        seen_pairs: set[tuple[str, str, str]] = set()
        for normalized_name, assigned_tasks in resource_tasks.items():
            # One resource can appear more than once in the same task row; do not
            # interpret duplicate assignment labels as a conflict with itself.
            unique_tasks = list({id(task): task for task in assigned_tasks}.values())
            for i, task_a in enumerate(unique_tasks):
                a_start, a_finish = _dt(task_a.get("start")), _dt(task_a.get("finish"))
                for task_b in unique_tasks[i + 1:]:
                    b_start, b_finish = _dt(task_b.get("start")), _dt(task_b.get("finish"))
                    if a_start and a_finish and b_start and b_finish and max(a_start, b_start) <= min(a_finish, b_finish):
                        refs = sorted([str(task_a.get("id") or _task_ref(task_a)), str(task_b.get("id") or _task_ref(task_b))])
                        pair_key = (normalized_name, refs[0], refs[1])
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        findings.append(_finding(
                            "RES-001", "Resources", "Medium", "Potential resource contention",
                            f"The same resource ({assignment_name(task_a, normalized_name)}) is assigned to two tasks with overlapping planned dates.",
                            [task_a, task_b], "The resource may be committed to concurrent work; capacity and working calendars are needed to confirm over-allocation.",
                            "Review the resource's availability, assignment units and priority of the overlapping tasks.",
                            f"Task A: {task_a.get('start')} to {task_a.get('finish')}; Task B: {task_b.get('start')} to {task_b.get('finish')}.",
                            "Medium",
                        ))
        if not payload.get("data_availability", {}).get("resource_load_values"):
            limitations.append("Resource conflicts are treated as potential date overlaps only; assignment units/work and resource calendars were not supplied, so over-allocation cannot be confirmed.")
    else:
        limitations.append("Resource loading was not assessed because resource assignments were not present in the source plan.")

    # WBS consistency is only meaningful when the source includes WBS values.
    wbs_values = [t.get("wbs") for t in detail_tasks]
    if any(wbs_values) and any(not value for value in wbs_values):
        affected = [t for t in detail_tasks if not t.get("wbs")]
        findings.append(_finding(
            "WBS-001", "WBS and structure", "Low", "WBS values are incomplete",
            "Some activities have WBS values while others do not.", affected,
            "Work-package traceability and hierarchy-based reporting may be inconsistent.",
            "Complete the WBS values or confirm that the omitted activities are intentionally outside the WBS.",
        ))

    # Status wording is categorical and findings-based, not a numeric health score.
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 99), f["category"], f["rule_id"], f["title"]))
    counts = {severity: sum(1 for f in findings if f["severity"] == severity)
              for severity in ("Critical", "High", "Medium", "Low", "Info")}
    if counts["Critical"]:
        status = "Critical issues found"
    elif counts["High"]:
        status = "High-priority issues found"
    elif counts["Medium"]:
        status = "Improvement opportunities identified"
    elif counts["Low"]:
        status = "Minor improvements identified"
    else:
        status = "No material issues found in available data"

    return {
        "assessment_status": status,
        "finding_count": len(findings),
        "finding_counts": counts,
        "findings": findings,
        "limitations": list(dict.fromkeys(limitations)),
        "assessment_metadata": {
            "task_count": len(tasks),
            "detail_task_count": len(detail_tasks),
            "status_date": check_date.date().isoformat() if check_date else None,
            "source_format": payload.get("source", {}).get("format"),
            "source_file": payload.get("source", {}).get("file_name"),
        },
    }


def assignment_name(task: dict[str, Any], normalized_name: str) -> str:
    for assignment in task.get("assignments", []) or []:
        if (assignment.get("resource_name") or "").strip().casefold() == normalized_name:
            return (assignment.get("resource_name") or "").strip()
    return normalized_name
