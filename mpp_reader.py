"""Read-only native Microsoft Project MPP adapter using MPXJ.

This adapter converts MPXJ objects into JSON-safe dictionaries. It never writes
back to the uploaded schedule. Native MPP parsing requires Java, JPype1 and the
MPXJ Python distribution (see requirements.txt and packages.txt).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def _invoke(obj: Any, method_name: str, default: Any = None) -> Any:
    """Call a Java getter safely; return default if the getter is unavailable."""
    if obj is None:
        return default
    try:
        member = getattr(obj, method_name)
        return member() if callable(member) else member
    except Exception:
        return default


def _first(obj: Any, *method_names: str, default: Any = None) -> Any:
    for method_name in method_names:
        value = _invoke(obj, method_name, default=None)
        if value is not None:
            return value
    return default


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str(value.toString())
    except Exception:
        return str(value)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return float(str(value))
        except (TypeError, ValueError, OverflowError):
            return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        # Some Java collection proxies expose an iterator but not Python's
        # sequence protocol. Fall back to the standard Java iterator methods.
        result: list[Any] = []
        try:
            iterator = value.iterator()
            while iterator.hasNext():
                result.append(iterator.next())
        except Exception:
            return result
        return result


def _date(value: Any) -> str | None:
    """Render MPXJ/Java date objects as ISO strings in the JVM default timezone."""
    if value is None:
        return None
    # MPXJ commonly exposes java.util.Date instances. Their toString() is not
    # ISO-8601, so format through Java while preserving the runtime's timezone.
    try:
        from java.text import SimpleDateFormat
        from java.util import TimeZone
        formatter = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss")
        formatter.setTimeZone(TimeZone.getDefault())
        return str(formatter.format(value))
    except Exception:
        return _text(value)


def _duration(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    amount = _first(value, "getDuration", "getValue")
    units = _first(value, "getUnits")
    return {
        "display": _text(value),
        "value": _number(amount),
        "units": _text(units),
    }


def _name(value: Any) -> str | None:
    if value is None:
        return None
    return _text(_first(value, "getName", "getDescription", default=value))


def _read_assignments(task: Any) -> list[dict[str, Any]]:
    assignments = _items(_first(task, "getResourceAssignments", default=[]))
    output: list[dict[str, Any]] = []
    for assignment in assignments:
        resource = _invoke(assignment, "getResource")
        output.append({
            "resource_id": _text(_first(resource, "getUniqueID", "getID")),
            "resource_name": _text(_first(resource, "getName")),
            "units": _number(_invoke(assignment, "getUnits")),
            "work": _duration(_invoke(assignment, "getWork")),
            "actual_work": _duration(_invoke(assignment, "getActualWork")),
        })
    return output


def _read_predecessors(task: Any) -> list[dict[str, Any]]:
    relations = _items(_invoke(task, "getPredecessors", default=[]))
    output: list[dict[str, Any]] = []
    for relation in relations:
        predecessor = _first(relation, "getPredecessorTask", "getSourceTask", "getTargetTask")
        output.append({
            "predecessor_id": _text(_first(predecessor, "getID")),
            "predecessor_unique_id": _text(_first(predecessor, "getUniqueID")),
            "predecessor_name": _text(_first(predecessor, "getName")),
            "type": _text(_invoke(relation, "getType")),
            "lag": _duration(_first(relation, "getLag", "getDuration")),
            "relation_unique_id": _text(_invoke(relation, "getUniqueID")),
        })
    return output


def _read_task(task: Any) -> dict[str, Any]:
    calendar = _invoke(task, "getCalendar")
    parent = _invoke(task, "getParentTask")
    return {
        "id": _text(_invoke(task, "getID")),
        "unique_id": _text(_invoke(task, "getUniqueID")),
        "name": _text(_invoke(task, "getName")),
        "wbs": _text(_invoke(task, "getWBS")),
        "outline_number": _text(_invoke(task, "getOutlineNumber")),
        "outline_level": _int(_invoke(task, "getOutlineLevel")),
        "parent_unique_id": _text(_first(parent, "getUniqueID")),
        "is_summary": bool(_first(task, "getSummary", "isSummary", default=False)),
        "is_milestone": bool(_first(task, "getMilestone", "isMilestone", default=False)),
        "start": _date(_invoke(task, "getStart")),
        "finish": _date(_invoke(task, "getFinish")),
        "duration": _duration(_invoke(task, "getDuration")),
        "percent_complete": _number(_first(task, "getPercentageComplete", "getPercentComplete")),
        "actual_start": _date(_invoke(task, "getActualStart")),
        "actual_finish": _date(_invoke(task, "getActualFinish")),
        "actual_duration": _duration(_invoke(task, "getActualDuration")),
        "baseline_start": _date(_invoke(task, "getBaselineStart")),
        "baseline_finish": _date(_invoke(task, "getBaselineFinish")),
        "baseline_duration": _duration(_invoke(task, "getBaselineDuration")),
        "constraint_type": _text(_invoke(task, "getConstraintType")),
        "constraint_date": _date(_invoke(task, "getConstraintDate")),
        "deadline": _date(_invoke(task, "getDeadline")),
        "calendar_name": _text(_first(calendar, "getName")),
        "predecessors": _read_predecessors(task),
        "assignments": _read_assignments(task),
        "owner": None,
        "deliverable": None,
        "source_row_number": None,
        "source_fields": {},
    }


def extract_project(project: Any, source_name: str = "uploaded.mpp") -> dict[str, Any]:
    """Convert an MPXJ ProjectFile (or test double) into the canonical MVP shape."""
    properties = _invoke(project, "getProjectProperties")
    raw_tasks = _first(project, "getAllTasks", "getTasks", default=[])
    tasks = [_read_task(task) for task in _items(raw_tasks) if task is not None]

    project_metadata = {
        "title": _text(_first(properties, "getProjectTitle", "getName")),
        "author": _text(_invoke(properties, "getAuthor")),
        "company": _text(_invoke(properties, "getCompany")),
        "start_date": _date(_invoke(properties, "getStartDate")),
        "finish_date": _date(_invoke(properties, "getFinishDate")),
        "status_date": _date(_invoke(properties, "getStatusDate")),
        "application_version": _text(_invoke(properties, "getApplicationVersion")),
    }

    has_baseline = any(t["baseline_start"] or t["baseline_finish"] for t in tasks)
    has_actuals = any(t["actual_start"] or t["actual_finish"] for t in tasks)
    has_dependencies = any(t["predecessors"] for t in tasks)
    has_assignments = any(t["assignments"] for t in tasks)
    has_resource_load_values = any(
        a.get("units") is not None or a.get("work") is not None
        for task in tasks for a in task["assignments"]
    )
    has_calendars = any(t["calendar_name"] for t in tasks)
    has_constraints = any(t["constraint_type"] or t["constraint_date"] or t["deadline"] for t in tasks)

    warnings: list[str] = []
    if not tasks:
        warnings.append("No tasks were returned by MPXJ. Confirm the file is a supported, non-empty MPP plan.")
    if not has_baseline:
        warnings.append("No baseline start/finish dates were found; baseline variance checks must be marked unavailable.")
    if not has_actuals:
        warnings.append("No actual start/finish dates were found; actual-progress date checks may be limited.")
    if not has_assignments:
        warnings.append("No resource assignments were found; resource loading checks must be marked unavailable.")

    return {
        "source": {"file_name": Path(source_name).name, "format": "MPP", "reader": "MPXJ"},
        "project": project_metadata,
        "tasks": tasks,
        "data_availability": {
            "baselines": has_baseline,
            "actual_dates": has_actuals,
            "dependencies": has_dependencies,
            "resource_assignments": has_assignments,
            "resource_load_values": has_resource_load_values,
            "calendars": has_calendars,
            "constraints_or_deadlines": has_constraints,
        },
        "parser_warnings": warnings,
        "task_count": len(tasks),
    }


def _start_jvm_and_read(path: Path) -> Any:
    try:
        import jpype
        import mpxj  # noqa: F401 - importing registers MPXJ jars for JPype.
    except ImportError as exc:
        raise RuntimeError(
            "MPP support dependencies are missing. Install requirements.txt and a Java runtime."
        ) from exc

    if not jpype.isJVMStarted():
        try:
            jpype.startJVM(convertStrings=True)
        except Exception as exc:
            raise RuntimeError(
                "Could not start the Java runtime for MPXJ. Check that JAVA_HOME/JRE is installed."
            ) from exc

    try:
        from org.mpxj.mpp import MPPReader
        reader = MPPReader()
        # The report does not need table layouts, filters or Gantt formatting.
        reader.setReadPresentationData(False)
        return reader.read(str(path.resolve()))
    except Exception as exc:
        message = str(exc)
        if "password protected" in message.lower():
            raise ValueError(
                "This MPP appears to be password-protected. Password-protected MPP files are not supported by this MVP reader."
            ) from exc
        raise ValueError(f"MPXJ could not read this MPP file: {message[:500]}") from exc


def read_mpp(path: str | Path) -> dict[str, Any]:
    """Read one native MPP file and return the shared project-schedule structure."""
    source_path = Path(path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"MPP file not found: {source_path}")
    if source_path.suffix.lower() != ".mpp":
        raise ValueError("The MPP reader accepts .mpp files only.")
    project = _start_jvm_and_read(source_path)
    result = extract_project(project, source_name=source_path.name)
    if result["task_count"] == 0:
        raise ValueError("MPXJ opened the file but returned no tasks. The plan may be empty or incompatible.")
    return result


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Read-only MPP extraction smoke test using MPXJ")
    parser.add_argument("mpp_file", help="Path to a native .mpp file")
    parser.add_argument("--json", dest="json_path", help="Optional JSON output path")
    args = parser.parse_args()

    data = read_mpp(args.mpp_file)
    rendered = json.dumps(data, indent=2, ensure_ascii=False)
    if args.json_path:
        Path(args.json_path).write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
