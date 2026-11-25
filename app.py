"""Streamlit UI for the read-only Project Plan Health Check MVP2."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

import streamlit as st

from excel_reader import list_schedule_sheets, read_excel
from health_engine import assess_schedule
from mpp_reader import read_mpp
from pdf_report import build_pdf

MAX_FILE_BYTES = 50 * 1024 * 1024
RESULT_KEY = "mvp2_result"
APP_DIR = Path(__file__).resolve().parent
SAMPLE_EXCEL_PATH = APP_DIR / "Fictional_Project_Plan_Test_Scenarios.xlsx"
SAMPLE_PDF_PATH = APP_DIR / "Sample_Health_Check_Report.pdf"
SAMPLE_MPP_URL = "https://projectkiller.app/assets/ProjectKiller-Sample.mpp"

st.set_page_config(
    page_title="Project Plan Health Check",
    page_icon="📋",
    layout="wide",
)

st.title("Project Plan Health Check")
st.caption("Upload a native Microsoft Project plan or Excel schedule. Review explainable findings and download a PDF report.")

st.subheader("Preview the report before uploading your plan")
st.write(
    "Try the fictional sample files first. You can download a sample Excel workbook, "
    "download a genuine native MPP sample from its publisher, and preview the PDF report format."
)

sample_cols = st.columns(2)
with sample_cols[0]:
    st.markdown("**Sample Excel workbook**")
    st.caption("Includes seven fictional schedule scenarios. Try `Clean_Plan` and `Data_Quality_Errors`.")
    if SAMPLE_EXCEL_PATH.exists():
        st.download_button(
            "Download sample Excel workbook",
            data=SAMPLE_EXCEL_PATH.read_bytes(),
            file_name=SAMPLE_EXCEL_PATH.name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_sample_excel",
        )
    else:
        st.warning("The sample Excel workbook is not present in this deployment.")

with sample_cols[1]:
    st.markdown("**Sample native MPP file**")
    st.caption("Genuine `.mpp` sample hosted externally by ProjectKiller. The file is not bundled in this repository.")
    st.link_button("Download sample MPP", SAMPLE_MPP_URL)

with st.expander("View an example PDF health-check report", expanded=False):
    st.caption(
        "This example was generated from the `Data_Quality_Errors` worksheet in the fictional Excel workbook. "
        "It demonstrates the report layout and sample findings; it is not an assessment of the external MPP sample. "
        "The same PDF report structure is used for Excel and MPP assessments."
    )
    if SAMPLE_PDF_PATH.exists():
        sample_pdf_bytes = SAMPLE_PDF_PATH.read_bytes()
        st.download_button(
            "Download example PDF report",
            data=sample_pdf_bytes,
            file_name=SAMPLE_PDF_PATH.name,
            mime="application/pdf",
            key="download_sample_pdf",
        )
        st.iframe(SAMPLE_PDF_PATH, height=720, alt="Example Project Plan Health Check PDF report")
    else:
        st.warning("The example PDF report is not present in this deployment.")

with st.expander("What this tool checks", expanded=False):
    st.write(
        "Schedule dates and durations, task data quality, dependency integrity and circular logic, "
        "milestone support, baseline variance, progress status, resource date overlaps, and WBS completeness."
    )
    st.write(
        "The tool is read-only. It does not create or edit MPP files, write back to your source plan, "
        "or keep project history in a database. Missing source data is shown as a limitation rather than assumed to be a defect."
    )

uploaded = st.file_uploader(
    "Choose a project plan",
    type=["mpp", "xlsx"],
    help="Supported inputs: native Microsoft Project .mpp and Excel .xlsx. Maximum file size: 50 MB.",
)
current_signature = None
if uploaded is not None:
    upload_bytes = uploaded.getvalue()
    suffix = Path(uploaded.name).suffix.lower()
    if len(upload_bytes) > MAX_FILE_BYTES:
        st.error("The uploaded file exceeds the 50 MB MVP limit.")
        upload_bytes = b""

    selected_sheet = None
    if upload_bytes and suffix == ".xlsx":
        try:
            with TemporaryDirectory(prefix="mvp2_sheets_") as temp_dir:
                temp_path = Path(temp_dir) / Path(uploaded.name).name
                temp_path.write_bytes(upload_bytes)
                sheets = list_schedule_sheets(temp_path)
            if not sheets:
                st.error("No worksheet with recognizable Task ID and Task Name columns was found.")
            else:
                selected_sheet = st.selectbox("Schedule worksheet", sheets)
                st.caption("Only the selected schedule worksheet is assessed. Other worksheets are not merged into the same plan.")
        except Exception as exc:
            st.error(f"Could not inspect the Excel workbook: {exc}")

    use_status_date = st.checkbox("Provide a status date for overdue-task checks", value=False)
    today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    selected_status_date = st.date_input("Status date", value=today_ist, disabled=not use_status_date)

    analyze_clicked = st.button(
        "Analyse project plan",
        type="primary",
        disabled=(not upload_bytes or (suffix == ".xlsx" and not selected_sheet)),
    )

    current_signature = sha256(
        upload_bytes + str(selected_sheet).encode("utf-8") + str(selected_status_date if use_status_date else "").encode("utf-8")
    ).hexdigest() if upload_bytes else None

    if analyze_clicked:
        st.session_state.pop(RESULT_KEY, None)
        try:
            with st.spinner("Reading the schedule and running health checks..."):
                with TemporaryDirectory(prefix="mvp2_health_check_") as temp_dir:
                    temp_path = Path(temp_dir) / Path(uploaded.name).name
                    temp_path.write_bytes(upload_bytes)
                    if suffix == ".mpp":
                        schedule = read_mpp(temp_path)
                    elif suffix == ".xlsx":
                        schedule = read_excel(temp_path, sheet_name=selected_sheet)
                    else:
                        raise ValueError("Only .mpp and .xlsx files are supported.")

                status_value = selected_status_date.isoformat() if use_status_date else None
                assessment = assess_schedule(schedule, status_date=status_value)
                pdf_bytes = build_pdf(schedule, assessment)

            st.session_state[RESULT_KEY] = {
                "signature": current_signature,
                "assessment": assessment,
                "pdf": pdf_bytes,
                "file_name": uploaded.name,
                "schedule": {
                    "project_title": schedule.get("project", {}).get("title"),
                    "task_count": schedule.get("task_count", 0),
                    "source_format": schedule.get("source", {}).get("format"),
                    "parser_warnings": schedule.get("parser_warnings", []),
                },
            }
            st.success("Assessment completed. Download the PDF report below.")
        except Exception as exc:
            st.error(f"Assessment could not be completed: {exc}")
            st.info("For native MPP input, the deployment must have the MPXJ Python package and a working Java runtime.")

result = st.session_state.get(RESULT_KEY)
if result and result.get("signature") == current_signature:
    assessment = result["assessment"]
    source = result["schedule"]
    st.divider()
    st.subheader(assessment["assessment_status"])
    metric_cols = st.columns(5)
    for column, severity in zip(metric_cols, ("Critical", "High", "Medium", "Low", "Info")):
        column.metric(severity, assessment["finding_counts"].get(severity, 0))
    st.caption(f"{source.get('source_format')} input | {source.get('task_count')} tasks | {result['file_name']}")

    findings = assessment.get("findings", [])
    if findings:
        st.subheader("Findings")
        for finding in findings:
            heading = f"{finding['severity']} · {finding['rule_id']} · {finding['title']}"
            with st.expander(heading):
                st.write(finding["description"])
                if finding.get("task_refs"):
                    st.caption("Affected tasks: " + "; ".join(finding["task_refs"]))
                st.markdown(f"**Impact:** {finding['impact']}")
                st.markdown(f"**Recommended action:** {finding['recommended_action']}")
                st.caption("Evidence: " + (finding.get("evidence") or "Not supplied"))
    else:
        st.info("No material issues were identified in the data available. Review the limitations before relying on this result.")

    if assessment.get("limitations"):
        st.subheader("Assessment limitations")
        for limitation in assessment["limitations"]:
            st.write(f"- {limitation}")
    if source.get("parser_warnings"):
        with st.expander("Input parsing notes"):
            for warning in source["parser_warnings"]:
                st.write(f"- {warning}")

    download_name = f"{Path(result['file_name']).stem}_health_check_report.pdf"
    st.download_button(
        "Download PDF report",
        data=result["pdf"],
        file_name=download_name,
        mime="application/pdf",
        type="primary",
    )
elif result and current_signature is not None and result.get("signature") != current_signature:
    st.caption("The uploaded file or assessment options changed. Run the analysis again to refresh the report.")

if st.button("Clear current assessment"):
    st.session_state.pop(RESULT_KEY, None)
    st.rerun()

st.divider()
st.caption("Session-only processing | No project history database | No changes are made to the uploaded schedule")
