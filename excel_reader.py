"""Read Excel project schedules into the same canonical model as native MPP."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from typing import Any

from openpyxl import load_workbook

from canonical_model import validate_schedule_payload

ALIASES = {
    "id": {"task_id", "task id", "id", "uid", "unique id", "task unique id"},
    "name": {"task_name", "task name", "task", "name", "activity", "activity name"},
    "wbs": {"wbs", "wbs code", "outline number"},
    "outline_level": {"outline level", "level"},
    "start": {"start", "start date", "planned start"},
    "finish": {"finish", "finish date", "end", "end date", "planned finish"},
    "duration": {"duration_days", "duration days", "duration", "duration (days)"},
    "predecessors": {"predecessors", "predecessor", "dependencies", "depends on"},
    "milestone": {"milestone", "is milestone"},
    "summary": {"summary", "is summary", "summary task"},
    "resource": {"resource", "resources", "assigned resources", "assignment"},
    "percent_complete": {"percent_complete", "percent complete", "% complete", "progress"},
    "actual_start": {"actual_start", "actual start"},
    "actual_finish": {"actual_finish", "actual finish"},
    "baseline_start": {"baseline_start", "baseline start"},
    "baseline_finish": {"baseline_finish", "baseline finish"},
    "baseline_duration": {"baseline_duration", "baseline duration"},
    "constraint_type": {"constraint_type", "constraint type", "constraint"},
    "constraint_date": {"constraint_date", "constraint date"},
    "deadline": {"deadline", "target finish"},
    "owner": {"owner", "task owner", "responsible"},
    "deliverable": {"deliverable", "output"},
}

IGNORED_SHEETS = {"readme", "expected_results", "expected results", "instructions"}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes") if value.second == 0 else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        # Excel serial dates are normally already converted by openpyxl.
        return str(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat(timespec="minutes") if parsed.second == 0 else parsed.isoformat()
    except ValueError:
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError:
            return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "yes", "y", "1", "milestone", "summary"}


def _id_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _duration(value: Any) -> dict[str, Any] | None:
    number = _float(value)
    if number is None:
        return None
    return {"display": f"{number:g} days", "value": number, "units": "DAYS"}


def _split_resource_names(value: Any) -> list[str]:
    if value is None or str(value).strip() == "":
        return []
    names = re.split(r"[;,\n]+", str(value))
    return [name.strip() for name in names if name.strip()]


_PRED_RE = re.compile(
    r"^\s*(?P<id>\d+)\s*(?P<type>FS|SS|FF|SF)?\s*"
    r"(?P<lag>[+-]\s*\d+(?:\.\d+)?\s*(?:d|day|days|h|hr|hour|hours|w|wk|week|weeks)?)?\s*$",
    re.IGNORECASE,
)


def parse_predecessors(value: Any) -> list[dict[str, Any]]:
    """Parse common Excel dependency strings (e.g. 1, 1SS+1d, 2FF-2d)."""
    if value is None or str(value).strip() == "":
        return []
    result: list[dict[str, Any]] = []
    parts = re.split(r"[,;\n]+", str(value))
    for part in parts:
        token = part.strip()
        if not token:
            continue
        match = _PRED_RE.match(token)
        if not match:
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", token):
                result.append({
                    "predecessor_id": token,
                    "predecessor_unique_id": token,
                    "predecessor_name": None,
                    "type": "FINISH_START",
                    "lag": {"display": "0 days", "value": None, "units": "SOURCE_TEXT"},
                    "relation_unique_id": None,
                })
            else:
                result.append({
                    "predecessor_id": token,
                    "predecessor_unique_id": token,
                    "predecessor_name": None,
                    "type": None,
                    "lag": None,
                    "relation_unique_id": None,
                    "parse_warning": f"Unrecognised predecessor notation: {token}",
                })
            continue
        relation_type = (match.group("type") or "FS").upper()
        type_map = {"FS": "FINISH_START", "SS": "START_START", "FF": "FINISH_FINISH", "SF": "START_FINISH"}
        lag = (match.group("lag") or "").replace(" ", "")
        result.append({
            "predecessor_id": match.group("id"),
            "predecessor_unique_id": match.group("id"),
            "predecessor_name": None,
            "type": type_map[relation_type],
            "lag": {"display": lag or "0 days", "value": lag or None, "units": "SOURCE_TEXT"},
            "relation_unique_id": None,
        })
    return result


def _find_header(ws, scan_rows: int = 20) -> tuple[int | None, dict[str, int]]:
    for row_number in range(1, min(ws.max_row, scan_rows) + 1):
        raw = [_norm(cell.value) for cell in ws[row_number]]
        mapping: dict[str, int] = {}
        for canonical, aliases in ALIASES.items():
            for column_index, text in enumerate(raw, start=1):
                if text in aliases:
                    mapping[canonical] = column_index
                    break
        if "id" in mapping and "name" in mapping:
            return row_number, mapping
    return None, {}


def _cell(row: tuple[Any, ...], column: int | None) -> Any:
    if not column or column - 1 >= len(row):
        return None
    return row[column - 1]


def _read_sheet(ws, formula_ws, file_name: str, header_row: int, mapping: dict[str, int]) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    warnings: list[str] = []
    formula_cells_without_cache: list[str] = []

    for row_number in range(header_row + 1, ws.max_row + 1):
        row = tuple(cell.value for cell in ws[row_number])
        formula_row = tuple(cell.value for cell in formula_ws[row_number]) if formula_ws else row
        task_id = _cell(row, mapping.get("id"))
        task_name = _cell(row, mapping.get("name"))
        start = _iso(_cell(row, mapping.get("start")))
        finish = _iso(_cell(row, mapping.get("finish")))
        predecessor_value = _cell(row, mapping.get("predecessors"))
        resource_value = _cell(row, mapping.get("resource"))
        milestone_value = _cell(row, mapping.get("milestone"))
        duration_value = _cell(row, mapping.get("duration"))
        actual_start = _iso(_cell(row, mapping.get("actual_start")))
        actual_finish = _iso(_cell(row, mapping.get("actual_finish")))
        baseline_start = _iso(_cell(row, mapping.get("baseline_start")))
        baseline_finish = _iso(_cell(row, mapping.get("baseline_finish")))
        constraint_type = _cell(row, mapping.get("constraint_type"))
        constraint_date = _iso(_cell(row, mapping.get("constraint_date")))
        deadline = _iso(_cell(row, mapping.get("deadline")))

        # Ignore wholly blank rows, but preserve partially populated rows so
        # data-quality rules can report their missing IDs/names/dates.
        meaningful_values = [task_id, task_name, start, finish, predecessor_value, resource_value,
                             milestone_value, duration_value, actual_start, actual_finish,
                             baseline_start, baseline_finish, constraint_type, constraint_date, deadline]
        if not any(value not in (None, "") for value in meaningful_values):
            continue

        predecessors = parse_predecessors(predecessor_value)
        for pred in predecessors:
            if pred.get("parse_warning"):
                warnings.append(f"Row {row_number}: {pred['parse_warning']}")

        resources = _split_resource_names(resource_value)
        assignments = [{
            "resource_id": None,
            "resource_name": resource,
            "units": None,
            "work": None,
            "actual_work": None,
        } for resource in resources]

        percent_col = mapping.get("percent_complete")
        percent_value = _float(_cell(row, percent_col))
        if percent_value is not None and percent_col:
            number_format = ws.cell(row=row_number, column=percent_col).number_format or ""
            if "%" in number_format:
                percent_value *= 100.0

        # Keep unmapped cells for traceability. These values are report evidence,
        # not instructions and are never executed.
        source_fields: dict[str, Any] = {}
        for column_index, header_cell in enumerate(ws[header_row], start=1):
            header = str(header_cell.value).strip() if header_cell.value is not None else ""
            if header and column_index not in mapping.values():
                value = row[column_index - 1] if column_index <= len(row) else None
                if isinstance(value, (datetime, date)):
                    value = value.isoformat()
                if value not in (None, ""):
                    source_fields[header] = str(value)

        tasks.append({
            "id": _id_text(task_id),
            "unique_id": _id_text(task_id),
            "name": str(task_name).strip() if task_name not in (None, "") else None,
            "wbs": str(_cell(row, mapping.get("wbs"))).strip() if _cell(row, mapping.get("wbs")) not in (None, "") else None,
            "outline_number": None,
            "outline_level": int(_float(_cell(row, mapping.get("outline_level")))) if _float(_cell(row, mapping.get("outline_level"))) is not None else None,
            "parent_unique_id": None,
            "is_summary": _truthy(_cell(row, mapping.get("summary"))),
            "is_milestone": _truthy(milestone_value),
            "start": start,
            "finish": finish,
            "duration": _duration(duration_value),
            "percent_complete": percent_value,
            "actual_start": actual_start,
            "actual_finish": actual_finish,
            "actual_duration": None,
            "baseline_start": baseline_start,
            "baseline_finish": baseline_finish,
            "baseline_duration": _duration(_cell(row, mapping.get("baseline_duration"))),
            "constraint_type": str(constraint_type).strip() if constraint_type not in (None, "") else None,
            "constraint_date": constraint_date,
            "deadline": deadline,
            "calendar_name": None,
            "predecessors": predecessors,
            "assignments": assignments,
            "owner": str(_cell(row, mapping.get("owner"))).strip() if _cell(row, mapping.get("owner")) not in (None, "") else None,
            "deliverable": str(_cell(row, mapping.get("deliverable"))).strip() if _cell(row, mapping.get("deliverable")) not in (None, "") else None,
            "source_row_number": row_number,
            "source_fields": source_fields,
        })

        # Formula cells without cached results can appear blank with data_only=True.
        for field_name, column_index in mapping.items():
            formula = formula_row[column_index - 1] if column_index <= len(formula_row) else None
            cached = row[column_index - 1] if column_index <= len(row) else None
            if isinstance(formula, str) and formula.startswith("=") and cached is None:
                formula_cells_without_cache.append(f"{ws.title}!{ws.cell(row=row_number, column=column_index).coordinate}")

    if formula_cells_without_cache:
        sample = ", ".join(formula_cells_without_cache[:8])
        suffix = " …" if len(formula_cells_without_cache) > 8 else ""
        warnings.append(f"Formula cells have no cached results and were read as blank: {sample}{suffix}")

    if not tasks:
        raise ValueError(f"Worksheet '{ws.title}' contains a task header but no task rows.")

    availability = {
        "baselines": any(t["baseline_start"] or t["baseline_finish"] for t in tasks),
        "actual_dates": any(t["actual_start"] or t["actual_finish"] for t in tasks),
        "dependencies": any(t["predecessors"] for t in tasks),
        "resource_assignments": any(t["assignments"] for t in tasks),
        "resource_load_values": any(
            a.get("units") is not None or a.get("work") is not None
            for task in tasks for a in task["assignments"]
        ),
        "calendars": any(t["calendar_name"] for t in tasks),
        "constraints_or_deadlines": any(t["constraint_type"] or t["constraint_date"] or t["deadline"] for t in tasks),
    }
    if not availability["baselines"]:
        warnings.append("No baseline dates were supplied; baseline variance checks must be marked unavailable.")
    if not availability["actual_dates"]:
        warnings.append("No actual dates were supplied; actual-progress date checks may be limited.")
    if not availability["resource_assignments"]:
        warnings.append("No resource assignments were supplied; resource loading checks must be marked unavailable.")
    if not any(mapping.get(k) for k in ("start", "finish")):
        warnings.append("No Start or Finish date columns were mapped; schedule date checks are limited.")

    result = {
        "source": {"file_name": Path(file_name).name, "format": "Excel", "sheet_name": ws.title, "reader": "openpyxl"},
        "project": {
            "title": Path(file_name).stem,
            "author": None,
            "company": None,
            "start_date": min((t["start"] for t in tasks if t["start"]), default=None),
            "finish_date": max((t["finish"] for t in tasks if t["finish"]), default=None),
            "status_date": None,
            "application_version": None,
        },
        "tasks": tasks,
        "data_availability": availability,
        "parser_warnings": warnings,
        "task_count": len(tasks),
    }
    shape_problems = validate_schedule_payload(result)
    if shape_problems:
        raise ValueError("Excel reader produced an invalid schedule model: " + "; ".join(shape_problems))
    return result


def read_excel(path: str | Path, sheet_name: str | None = None) -> dict[str, Any]:
    source_path = Path(path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Excel file not found: {source_path}")
    if source_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("The Excel reader accepts .xlsx and .xlsm files only.")

    workbook_values = load_workbook(source_path, data_only=True, read_only=True)
    workbook_formulas = load_workbook(source_path, data_only=False, read_only=True)
    try:
        if sheet_name:
            if sheet_name not in workbook_values.sheetnames:
                raise ValueError(f"Worksheet '{sheet_name}' not found. Available sheets: {', '.join(workbook_values.sheetnames)}")
            candidates = [sheet_name]
        else:
            candidates = [name for name in workbook_values.sheetnames if _norm(name) not in {_norm(x) for x in IGNORED_SHEETS}]

        matched: list[tuple[str, int, dict[str, int]]] = []
        for name in candidates:
            ws = workbook_values[name]
            header_row, mapping = _find_header(ws)
            if header_row is not None:
                matched.append((name, header_row, mapping))

        if not matched:
            raise ValueError("No worksheet with recognizable Task_ID/Task_Name (or ID/Name) headers was found.")
        if sheet_name is None and len(matched) > 1:
            selected = matched[0]
            # Multiple schedule tabs are common; default to the first schedule-like
            # sheet and surface that choice so the UI can add an explicit selector.
            extra_warning = (
                f"Multiple schedule worksheets detected ({', '.join(x[0] for x in matched)}). "
                f"Only '{selected[0]}' was assessed; select another sheet to assess it separately."
            )
        else:
            selected = matched[0]
            extra_warning = None

        name, header_row, mapping = selected
        result = _read_sheet(workbook_values[name], workbook_formulas[name], source_path.name, header_row, mapping)
        if extra_warning:
            result["parser_warnings"].append(extra_warning)
        return result
    finally:
        workbook_values.close()
        workbook_formulas.close()


def list_schedule_sheets(path: str | Path) -> list[str]:
    """Return worksheets with recognizable task ID and task name headers."""
    source_path = Path(path)
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"Excel file not found: {source_path}")
    if source_path.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise ValueError("The Excel reader accepts .xlsx and .xlsm files only.")
    workbook = load_workbook(source_path, data_only=True, read_only=True)
    try:
        matched: list[str] = []
        ignored = {_norm(x) for x in IGNORED_SHEETS}
        for name in workbook.sheetnames:
            if _norm(name) in ignored:
                continue
            if _find_header(workbook[name])[0] is not None:
                matched.append(name)
        return matched
    finally:
        workbook.close()
