"""Streamlit UI for testing sublet RO resolution and job matching.

Drop an invoice PDF/image, optionally override VIN/dealership, and the app
will:
  1. Run the same OCR the pipeline uses
  2. Search Tekion for ROs by VIN
  3. Show resolved RO number
  4. Fetch jobs on that RO
  5. Match each invoice line to a job via the LLM
  6. Display results with frontend job labels (A, B, C, D) only — no
     raw jobNumber shown

    uv run streamlit run scripts/sublet_match_ui.py

Read-only — no PO, pre-invoice, or draft is created.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(ROOT / ".env")

from api.services.job_matching import match_line_items_to_jobs
from api.services.ocr_helpers import get_dealership_name, get_raw_line_items, get_vin
from api.services.ocr_service import extract_document
from api.services.tekion_client import TekionApiClient

MAIN_PIPELINE = ROOT / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))
from pipeline import load_pages  # noqa: E402

# Frontend job labels: Tekion UI shows A, B, C, D... which map positionally
# to backend jobNumber 1, 2, 3, 4...
_JOB_LABELS = "ABCDEFGHIJKLMNOP"

# Dealer names fetched from Tekion's login response (client.dealers).
# Hardcoded so the dropdown populates without requiring a login first.
DEALERSHIPS = [
    "Arlington Acura in Palatine",
    "Arlington Nissan in Arlington Heights",
    "Bob Rohrman Honda",
    "Bob Rohrman Hyundai Genesis",
    "Bob Rohrman Indy Hyundai",
    "Bob Rohrman Kenosha Nissan",
    "Bob Rohrman Kia",
    "Bob Rohrman Schaumburg Kia",
    "Fort Wayne Kia",
    "Fort Wayne Nissan",
    "Fort Wayne Toyota Lexus",
    "Gurnee Hyundai",
    "Gurnee Volkswagen",
    "Indy Honda",
    "Lexus of Arlington",
    "Oakbrook Toyota in Westmont",
    "Rohrman Toyota",
    "Schaumburg Ford",
    "Schaumburg Honda",
]


def _job_label(job_number: str) -> str:
    """Convert API jobNumber ('1', '2', ...) to frontend label ('A', 'B', ...)."""
    try:
        idx = int(job_number) - 1
        if 0 <= idx < len(_JOB_LABELS):
            return _JOB_LABELS[idx]
    except (ValueError, TypeError):
        pass
    return job_number


@st.cache_resource
def get_tekion_client() -> TekionApiClient:
    """Login once and reuse the client across reruns."""
    client = TekionApiClient()
    client.login()
    return client


# ── Session state ─────────────────────────────────────────────────────────────
st.session_state.setdefault("zoom", 400)
st.session_state.setdefault("results", None)


def main() -> None:
    st.set_page_config(
        page_title="Sublet RO & job matcher",
        page_icon=":material/receipt_long:",
        layout="wide",
    )

    st.title("Sublet RO resolution & job matching")
    st.caption(
        "Drop an invoice, see which RO it resolves to and which job each line "
        "maps to. Uses the same OCR and LLM matcher as the production pipeline. "
        "Read-only — nothing is written to Tekion."
    )

    # ── Inputs ───────────────────────────────────────────────────────────────
    col_file, col_dealer = st.columns([3, 2])

    with col_file:
        uploaded = st.file_uploader(
            "Invoice PDF or image",
            type=["pdf", "png", "jpg", "jpeg", "tif", "tiff"],
        )

    with col_dealer:
        dealership_choice = st.selectbox(
            "Dealership",
            options=["(use OCR)"] + DEALERSHIPS,
            help="Select the dealership for Tekion dealer context. In "
            "production this comes from the frontend dropdown.",
        )
        dealership_override = (
            "" if dealership_choice == "(use OCR)" else dealership_choice
        )

    col_vin, col_status = st.columns([3, 1])

    with col_vin:
        vin_override = st.text_input(
            "VIN override",
            placeholder="Leave blank to use OCR-extracted VIN",
        )

    with col_status:
        any_status = st.toggle(
            "Any RO status",
            help="Include closed/invoiced/voided ROs (testing only). "
            "Production always filters to open ROs.",
        )

    if not uploaded:
        st.info("Upload an invoice to begin.")
        return

    # Save uploaded file to a temp path so extract_document can read it
    tmp_dir = ROOT / ".tmp_streamlit_uploads"
    tmp_dir.mkdir(exist_ok=True)
    tmp_path = tmp_dir / uploaded.name
    tmp_path.write_bytes(uploaded.getvalue())

    with st.container(horizontal=True):
        st.button(
            "Resolve RO & match jobs",
            type="primary",
            icon=":material/search:",
            on_click=_run_pipeline,
            args=(str(tmp_path), vin_override, dealership_override, any_status),
        )
        if st.session_state.results is not None:
            if st.button("Clear", icon=":material/clear:"):
                st.session_state.results = None
                st.rerun()

    # Render results if available
    if st.session_state.results is not None:
        _render_results(str(tmp_path))


def _run_pipeline(
    doc_path: str,
    vin_override: str,
    dealership_override: str,
    any_status: bool,
) -> None:
    """Execute the full OCR -> VIN search -> job match flow, store in session state."""
    results: dict = {}

    # ── 1. OCR ───────────────────────────────────────────────────────────────
    try:
        ocr = extract_document(doc_path)
    except Exception as exc:
        st.session_state.results = {"error": f"OCR error: {exc}"}
        return

    line_items = get_raw_line_items(ocr)
    descriptions = [item["description"] or "(no description)" for item in line_items]
    vin = (vin_override or get_vin(ocr) or "").strip().upper()
    dealership = (dealership_override or get_dealership_name(ocr) or "").strip()

    results["ocr"] = {
        "document_type": ocr.get("document_type"),
        "vin": vin,
        "dealership": dealership,
        "descriptions": descriptions,
    }

    if not descriptions:
        st.session_state.results = {**results, "error": "OCR found no line items. Nothing to match."}
        return

    if not vin:
        st.session_state.results = {**results, "error": "No VIN extracted from OCR. Enter a VIN override above."}
        return

    # ── 2. Tekion login + dealer context ─────────────────────────────────────
    try:
        client = get_tekion_client()
    except Exception as exc:
        st.session_state.results = {**results, "error": f"Login error: {exc}"}
        return

    dealer_id = ""
    if dealership:
        dealer_id = client.find_dealer_by_name(dealership)
        if not dealer_id:
            st.session_state.results = {
                **results,
                "error": f"Could not match dealership '{dealership}' to a Tekion dealer.",
            }
            return
        client.switch_dealer(dealer_id)

    results["dealer_id"] = dealer_id

    # ── 3. Search ROs by VIN ─────────────────────────────────────────────────
    hits = client.search_ro_by_vin(vin)
    results["ro_hits"] = hits

    if not hits:
        st.session_state.results = {**results, "error": f"No repair orders found for VIN {vin}."}
        return

    if any_status:
        ro = hits[0]
    else:
        ro = client.find_latest_open_ro_by_vin(vin)
        if not ro:
            st.session_state.results = {
                **results,
                "error": "No open RO found. Toggle 'Any RO status' to test against closed ROs.",
            }
            return

    results["selected_ro"] = ro

    # ── 4. Jobs on the RO ────────────────────────────────────────────────────
    jobs = client.get_ro_job_details(ro["id"])
    results["jobs"] = jobs

    # ── 5. LLM matching ──────────────────────────────────────────────────────
    try:
        job_numbers = match_line_items_to_jobs(descriptions, jobs)
    except Exception as exc:
        st.session_state.results = {**results, "error": f"Matching error: {exc}"}
        return

    results["job_numbers"] = job_numbers
    st.session_state.results = results


def _render_results(doc_path: str) -> None:
    """Display the pipeline results from session state."""
    r = st.session_state.results

    if "error" in r:
        st.error(r["error"])
        return

    ocr = r["ocr"]
    vin = ocr["vin"]
    dealership = ocr["dealership"]
    descriptions = ocr["descriptions"]
    ro = r.get("selected_ro", {})
    jobs = r.get("jobs", [])
    job_numbers = r.get("job_numbers", [])
    ro_hits = r.get("ro_hits", [])

    # ── Summary bar ──────────────────────────────────────────────────────────
    with st.container(border=True):
        c1, c2, c3, c4 = st.columns(4)
        c2.metric("VIN", vin or "(none)")
        c3.metric("Resolved RO", ro.get("roNo", "(none)"))
        c4.metric("RO status", ro.get("status", "(none)"))
        with c1:
            st.metric("Document type", ocr.get("document_type") or "(none)")
            st.caption(f"Dealership: {dealership or '(default)'}")

    # ── RO search results (collapsible) ──────────────────────────────────────
    with st.expander(f"RO search results ({len(ro_hits)} found)", icon=":material/list:"):
        for h in ro_hits:
            is_selected = h.get("roNo") == ro.get("roNo")
            marker = ":material/check_circle:" if is_selected else ":material/radio_button_unchecked:"
            st.write(
                f"{marker} RO **{h.get('roNo')}**  "
                f":orange-badge[{h.get('status')}]  "
                f"created={h.get('createdTime')}"
            )

    # ── Side-by-side: invoice preview | match results ────────────────────────
    col_preview, col_results = st.columns([2, 3], vertical_alignment="top")

    # Left: invoice preview with zoom controls
    with col_preview:
        st.markdown("##### Invoice preview")

        # Zoom controls
        with st.container(horizontal=True):
            if st.button(":material/zoom_out:", help="Zoom out", key="zoom_out"):
                st.session_state.zoom = max(150, st.session_state.zoom - 50)
            st.caption(f"width: {st.session_state.zoom}px")
            if st.button(":material/zoom_in:", help="Zoom in", key="zoom_in"):
                st.session_state.zoom = min(1200, st.session_state.zoom + 50)

        try:
            pages = load_pages(Path(doc_path))
            with st.container(height=800, border=True):
                for img in pages:
                    st.image(img, width=st.session_state.zoom)
        except Exception as exc:
            st.warning(f"Could not render preview: {exc}")

    # Right: only matched jobs + line item matches
    with col_results:
        st.markdown("##### Match results")

        by_number = {j["jobNumber"]: j for j in jobs}
        # Collect only the jobs that were actually matched
        matched_job_numbers = set(job_numbers)

        for i, desc in enumerate(descriptions):
            picked = job_numbers[i] if i < len(job_numbers) else ""
            job = by_number.get(picked, {})
            label = _job_label(picked)

            with st.container(border=True):
                header_col, badge_col = st.columns([4, 1])
                with header_col:
                    st.write(f"**Line {i}:** {desc}")
                with badge_col:
                    st.badge(f"Job {label}", color="blue")

                st.caption(f"Concern: {job.get('capture') or '(none)'}")
                st.caption(f"Tech story: {job.get('techStory') or '(none)'}")

        # Show matched job details at the bottom (only matched jobs)
        if matched_job_numbers:
            with st.expander(
                f"Matched job details ({len(matched_job_numbers)} job(s))",
                icon=":material/build:",
            ):
                for j in jobs:
                    if j["jobNumber"] not in matched_job_numbers:
                        continue
                    label = _job_label(j["jobNumber"])
                    with st.container(border=True):
                        st.write(f"**Job {label}**")
                        st.caption(f"Concern: {j.get('capture') or '(none)'}")
                        st.caption(f"Tech story: {j.get('techStory') or '(none)'}")


if __name__ == "__main__":
    main()
