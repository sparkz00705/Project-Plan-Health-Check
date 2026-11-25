"""Generate a professional, findings-based project plan health-check PDF."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


SEVERITY_COLORS = {
    "Critical": colors.HexColor("#8B1E1E"),
    "High": colors.HexColor("#B45309"),
    "Medium": colors.HexColor("#2563A6"),
    "Low": colors.HexColor("#64748B"),
    "Info": colors.HexColor("#475569"),
}


def _register_font() -> tuple[str, str]:
    """Use a system Unicode font when available; do not bundle font files."""
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ]
    for regular, bold in candidates:
        if Path(regular).exists() and Path(bold).exists():
            try:
                if "MVP2Sans" not in pdfmetrics.getRegisteredFontNames():
                    pdfmetrics.registerFont(TTFont("MVP2Sans", regular))
                    pdfmetrics.registerFont(TTFont("MVP2Sans-Bold", bold))
                return "MVP2Sans", "MVP2Sans-Bold"
            except Exception:
                continue
    return "Helvetica", "Helvetica-Bold"


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    text = "" if value is None else str(value)
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _register_font()
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("MVP2Title", parent=base["Title"], fontName=bold, fontSize=21, leading=26,
                                alignment=TA_LEFT, textColor=colors.HexColor("#142B45"), spaceAfter=5),
        "subtitle": ParagraphStyle("MVP2Subtitle", parent=base["Normal"], fontName=regular, fontSize=9,
                                    leading=13, textColor=colors.HexColor("#526273"), spaceAfter=8),
        "h1": ParagraphStyle("MVP2H1", parent=base["Heading1"], fontName=bold, fontSize=14, leading=18,
                             textColor=colors.HexColor("#142B45"), spaceBefore=12, spaceAfter=7),
        "h2": ParagraphStyle("MVP2H2", parent=base["Heading2"], fontName=bold, fontSize=10.5, leading=13,
                             textColor=colors.HexColor("#142B45"), spaceBefore=5, spaceAfter=4),
        "body": ParagraphStyle("MVP2Body", parent=base["BodyText"], fontName=regular, fontSize=8.4, leading=12,
                                textColor=colors.HexColor("#253548"), spaceAfter=3),
        "small": ParagraphStyle("MVP2Small", parent=base["BodyText"], fontName=regular, fontSize=7.4, leading=10,
                                 textColor=colors.HexColor("#526273"), spaceAfter=2),
        "label": ParagraphStyle("MVP2Label", parent=base["BodyText"], fontName=bold, fontSize=7.5, leading=10,
                                 textColor=colors.HexColor("#526273")),
        "finding": ParagraphStyle("MVP2Finding", parent=base["Heading3"], fontName=bold, fontSize=9.5, leading=12,
                                   textColor=colors.HexColor("#142B45"), spaceAfter=2),
        "white": ParagraphStyle("MVP2White", parent=base["BodyText"], fontName=bold, fontSize=7.5, leading=9,
                                 textColor=colors.white, alignment=TA_CENTER),
    }


def _page(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4
    canvas.setStrokeColor(colors.HexColor("#D7DEE7"))
    canvas.setLineWidth(0.5)
    canvas.line(18 * mm, height - 15 * mm, width - 18 * mm, height - 15 * mm)
    canvas.setFont("Helvetica-Bold", 7.5)
    canvas.setFillColor(colors.HexColor("#526273"))
    canvas.drawString(18 * mm, height - 11.5 * mm, "PROJECT PLAN HEALTH CHECK")
    canvas.setFont("Helvetica", 7)
    canvas.drawRightString(width - 18 * mm, 10 * mm, f"Project Plan Health Check | Page {doc.page}")
    canvas.restoreState()


def _summary_table(assessment: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    counts = assessment.get("finding_counts", {})
    headers = ["Critical", "High", "Medium", "Low", "Info"]
    data = [[_p(x, styles["white"]) for x in headers], [str(counts.get(x, 0)) for x in headers]]
    table = Table(data, colWidths=[34 * mm] * 5, rowHeights=[8 * mm, 10 * mm], hAlign="LEFT")
    commands = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#243B53")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F1F5F9")),
        ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#142B45")),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 15),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7DEE7")),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7DEE7")),
    ]
    table.setStyle(TableStyle(commands))
    return table


def _finding_table(finding: dict[str, Any], styles: dict[str, ParagraphStyle]) -> Table:
    severity = finding.get("severity", "Info")
    rule_id = finding.get("rule_id", "")
    title = finding.get("title", "Untitled finding")
    severity_color = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["Info"])
    header = Table(
        [[_p(f"{rule_id} | {title}", styles["finding"]), _p(severity.upper(), styles["white"])]],
        colWidths=[145 * mm, 25 * mm],
        hAlign="LEFT",
    )
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#EEF2F6")),
        ("BACKGROUND", (1, 0), (1, 0), severity_color),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#D7DEE7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    rows = []
    fields = [
        ("Category", finding.get("category")),
        ("What was found", finding.get("description")),
        ("Affected tasks", "; ".join(finding.get("task_refs", []) or [])),
        ("Evidence", finding.get("evidence")),
        ("Potential impact", finding.get("impact")),
        ("Recommended action", finding.get("recommended_action")),
        ("Data confidence", finding.get("data_confidence", "High")),
    ]
    for label, value in fields:
        if value in (None, "", []):
            continue
        rows.append([_p(label, styles["label"]), _p(value, styles["body"])])
    details = Table(rows, colWidths=[31 * mm, 139 * mm], hAlign="LEFT")
    details.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F8FAFC")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D7DEE7")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return KeepTogether([
        header,
        details,
    ])


def build_pdf(schedule: dict[str, Any], assessment: dict[str, Any]) -> bytes:
    """Return a PDF report as bytes. No files are retained on disk."""
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=20 * mm, leftMargin=20 * mm,
        topMargin=22 * mm, bottomMargin=18 * mm,
        title="Project Plan Health Check", author="Project Plan Health Check",
        subject="Findings-based schedule assessment",
    )
    story: list[Any] = []
    source = schedule.get("source", {})
    project = schedule.get("project", {})
    metadata = assessment.get("assessment_metadata", {})
    counts = assessment.get("finding_counts", {})
    report_date = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %B %Y, %I:%M %p IST")

    story.extend([
        Spacer(1, 4 * mm),
        _p("Project Plan Health Check", styles["title"]),
        _p("Findings-based schedule assessment | Read-only review", styles["subtitle"]),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#8FA3B8"), spaceAfter=8),
        _p(assessment.get("assessment_status", "Assessment complete"), styles["h1"]),
        _p(f"Source file: {source.get('file_name', 'Not supplied')} | Format: {source.get('format', 'Unknown')} | Report generated: {report_date}", styles["body"]),
        _p(f"Project: {project.get('title') or 'Not supplied'} | Tasks read: {metadata.get('task_count', schedule.get('task_count', 0))} | Detailed tasks assessed: {metadata.get('detail_task_count', 0)}", styles["body"]),
    ])
    project_dates = []
    if project.get("start_date"):
        project_dates.append(f"Project start: {project['start_date']}")
    if project.get("finish_date"):
        project_dates.append(f"Project finish: {project['finish_date']}")
    if source.get("sheet_name"):
        project_dates.append(f"Worksheet: {source['sheet_name']}")
    if project_dates:
        story.append(_p(" | ".join(project_dates), styles["body"]))
    if metadata.get("status_date"):
        story.append(_p(f"Status date used for overdue analysis: {metadata['status_date']}", styles["body"]))
    else:
        story.append(_p("Status date: Not supplied; overdue-task checks were not performed.", styles["body"]))

    story.extend([
        Spacer(1, 3 * mm),
        _p("Findings by severity", styles["h2"]),
        _summary_table(assessment, styles),
        Spacer(1, 3 * mm),
        _p(f"Total findings: {assessment.get('finding_count', 0)}", styles["body"]),
        _p("This report does not calculate a composite health score. Findings reflect the data available in the uploaded plan and the rules listed in this report.", styles["small"]),
        _p("Detailed findings", styles["h1"]),
    ])

    findings = assessment.get("findings", [])
    if findings:
        for index, finding in enumerate(findings, start=1):
            story.append(_finding_table(finding, styles))
            story.append(Spacer(1, 4 * mm))
    else:
        story.append(_p("No material issues were identified in the data available for this assessment. Review the limitations below before relying on that conclusion.", styles["body"]))

    story.append(_p("Assessment limitations and data gaps", styles["h1"]))
    limitations = assessment.get("limitations", [])
    if limitations:
        for limitation in limitations:
            story.append(_p(f"• {limitation}", styles["body"]))
    else:
        story.append(_p("No additional data limitations were identified by the current checks.", styles["body"]))

    parser_warnings = schedule.get("parser_warnings", [])
    if parser_warnings:
        story.append(_p("Input parsing notes", styles["h1"]))
        for warning in parser_warnings:
            story.append(_p(f"• {warning}", styles["small"]))

    story.extend([
        Spacer(1, 5 * mm),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#D7DEE7"), spaceBefore=3, spaceAfter=5),
        _p("Scope: This is an automated, rules-based review of the supplied schedule data. It is intended to support project-management review, not replace professional judgement or source-system validation. No changes are made to the uploaded plan.", styles["small"]),
    ])

    doc.build(story, onFirstPage=_page, onLaterPages=_page)
    return buffer.getvalue()
