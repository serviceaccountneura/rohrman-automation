#!/usr/bin/env python3
"""Streamlit app — dealership dropdown + PO type folder navigation.

Navigation flow:
  Select dealership (dropdown, top right) -> PO type folders -> click PO type -> upload + OCR
"""
import sys
import os
import json
import streamlit as st
from pathlib import Path

# Add main_pipeline to path so we can import the OCR pipeline
MAIN_PIPELINE = Path(__file__).parent / "main_pipeline"
if str(MAIN_PIPELINE) not in sys.path:
    sys.path.insert(0, str(MAIN_PIPELINE))

# Fix credentials path — pipeline.py loads .env with ../neura_vertex_ai.json
# which is wrong when running from project root. Force absolute path before import.
PROJECT_ROOT = Path(__file__).parent
os.environ["VERTEX_CREDENTIALS"] = str(PROJECT_ROOT / "neura_vertex_ai.json")

# ── Data ─────────────────────────────────────────────────────────────────── #

DEALERSHIPS = {
    "Arlington Acura in Palatine": "AN",
    "Arlington Nissan in Arlington Heights": "BH",
    "Bob Rohrman Honda": "RH",
    "Bob Rohrman Hyundai Genesis": "KN",
    "Bob Rohrman Kenosha Nissan": "BK",
    "Bob Rohrman Kia": "SK",
    "Bob Rohrman Schaumburg Kia": "FK",
    "Fort Wayne Kia": "FI",
    "Fort Wayne Nissan": "FL",
    "Fort Wayne Toyota Lexus": "GH",
    "Gurnee Hyundai": "GW",
    "Gurnee Volkswagen": "IH",
    "Indy Honda": "IH",
    "Indy Hyundai": "LA",
    "Lexus of Arlington": "OT",
    "Oakbrook Toyota in Westmont": "RT",
    "Rohrman Toyota": "SF",
    "Schaumburg Ford": "HA",
    "Schaumburg Honda Automobiles": "SH",
}

PO_TYPES = [
    "Sublet PO",
    "OEM Stock Order",
    "OEM Special Order",
    "Vendor Stock Order",
    "Vendor Special Order",
    "Miscellaneous PO",
    "Vendor Credit PO",
]

UPLOAD_DIR = Path(__file__).parent / "uploads"


def count_files(directory: Path) -> int:
    """Count files in a directory (non-recursive)."""
    if not directory.exists():
        return 0
    return sum(1 for f in directory.iterdir() if f.is_file())


def folder_path_for(dealer_code: str, po_type: str) -> Path:
    return UPLOAD_DIR / dealer_code / po_type.replace(" ", "_")


# ── Session state ────────────────────────────────────────────────────────── #

st.session_state.setdefault("nav_dealer", None)
st.session_state.setdefault("nav_po", None)
st.session_state.setdefault("ocr_results", {})

# Default to first dealership if none selected
if st.session_state.nav_dealer is None:
    st.session_state.nav_dealer = list(DEALERSHIPS.values())[0]


# ── Page config ──────────────────────────────────────────────────────────── #

st.set_page_config(page_title="Rohrman AP Automation", page_icon=":material/folder:", layout="wide")


# ── Breadcrumb / back navigation ─────────────────────────────────────────── #

def on_dealer_change():
    """Callback when dealership dropdown changes."""
    selected = st.session_state.dealer_dropdown
    new_code = DEALERSHIPS.get(selected)
    if new_code and new_code != st.session_state.nav_dealer:
        st.session_state.nav_dealer = new_code
        st.session_state.nav_po = None


def render_top_bar():
    """Render dealership dropdown at top right and PO type breadcrumb."""
    col_left, col_right = st.columns([3, 1])
    with col_left:
        po_type = st.session_state.nav_po
        if po_type:
            if st.button(f":material/arrow_back: Back to {po_type} folders", key="back_to_po_types"):
                st.session_state.nav_po = None
                st.rerun()
    with col_right:
        dealer_names = list(DEALERSHIPS.keys())
        current_code = st.session_state.nav_dealer
        current_name = next((n for n, c in DEALERSHIPS.items() if c == current_code), dealer_names[0])
        st.selectbox(
            "Dealership",
            options=dealer_names,
            index=dealer_names.index(current_name) if current_name in dealer_names else 0,
            key="dealer_dropdown",
            label_visibility="collapsed",
            on_change=on_dealer_change,
        )


# ── Views ────────────────────────────────────────────────────────────────── #

def view_dealer(dealer_code: str):
    """Dealership view — grid of PO type folders."""
    dealer_name = next((n for n, c in DEALERSHIPS.items() if c == dealer_code), dealer_code)
    st.subheader(f"{dealer_name} — PO types")
    st.caption("Click a PO type folder to upload documents")

    cols = st.columns(4)
    for idx, po_type in enumerate(PO_TYPES):
        col = cols[idx % 4]
        with col:
            fp = folder_path_for(dealer_code, po_type)
            file_count = count_files(fp)
            with st.container(border=True):
                if st.button(
                    f":material/folder: {po_type}",
                    key=f"po_{dealer_code}_{po_type}",
                    width="stretch",
                    help=f"{file_count} file(s)",
                ):
                    st.session_state.nav_po = po_type
                    st.rerun()
                st.caption(f"{file_count} file(s)")


def ocr_cache_path(file_path: Path) -> Path:
    """Path for the saved OCR result JSON next to the uploaded file."""
    return file_path.with_suffix(".ocr.json")


def save_ocr_result(file_path: Path, result: dict):
    """Save OCR result to disk so it persists across restarts."""
    cache = ocr_cache_path(file_path)
    cache.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[OCR] Saved result to {cache.name}", flush=True)


def load_ocr_result(file_path: Path) -> dict | None:
    """Load saved OCR result from disk if available."""
    cache = ocr_cache_path(file_path)
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            return None
    return None


def run_ocr(file_path: Path) -> dict | None:
    """Run the OCR pipeline on a file and return extracted JSON."""
    print(f"[OCR] Starting extraction: {file_path.name}", flush=True)
    try:
        import pipeline as ocr_pipeline
        print("[OCR] Getting Gemini client...", flush=True)
        client = ocr_pipeline.get_client()
        print(f"[OCR] Running extraction on {file_path}...", flush=True)
        result = ocr_pipeline.extract_path(file_path, client)
        print(f"[OCR] Done. Keys: {list(result.keys()) if result else 'None'}", flush=True)
        if result:
            save_ocr_result(file_path, result)
        return result
    except Exception as e:
        print(f"[OCR] FAILED: {e}", flush=True)
        st.error(f"OCR failed: {e}")
        return None


def render_field(label: str, value, key_prefix: str):
    """Recursively render an OCR-extracted field as an editable widget.

    - Scalars (str/int/float/bool/None) -> text_input or checkbox
    - dict -> nested group of fields
    - list of dicts -> list of bordered item cards
    - list of scalars -> text input (comma-separated)
    """
    # Pretty label
    pretty = label.replace("_", " ").capitalize()

    # None / null
    if value is None:
        return st.text_input(pretty, value="", key=f"{key_prefix}_nil", placeholder="(empty)")

    # Bool
    if isinstance(value, bool):
        return st.checkbox(pretty, value=value, key=f"{key_prefix}_bool")

    # Numbers
    if isinstance(value, (int, float)):
        if isinstance(value, float) or "." in str(value):
            return st.number_input(pretty, value=float(value), key=f"{key_prefix}_num")
        return st.number_input(pretty, value=int(value), step=1, key=f"{key_prefix}_int")

    # Strings
    if isinstance(value, str):
        if len(value) > 100:
            return st.text_area(pretty, value=value, key=f"{key_prefix}_str")
        return st.text_input(pretty, value=value, key=f"{key_prefix}_str")

    # Dict — render nested fields
    if isinstance(value, dict):
        st.markdown(f"**{pretty}**")
        result = {}
        for k, v in value.items():
            result[k] = render_field(k, v, f"{key_prefix}_{k}")
        return result

    # List
    if isinstance(value, list):
        st.markdown(f"**{pretty}** ({len(value)} item(s))")
        if not value:
            st.caption("(empty list)")
            return []

        # List of dicts — each item gets a bordered card
        if all(isinstance(item, dict) for item in value):
            edited_list = []
            for i, item in enumerate(value):
                with st.container(border=True):
                    st.caption(f"Item {i + 1}")
                    edited_item = {}
                    for k, v in item.items():
                        edited_item[k] = render_field(k, v, f"{key_prefix}_{i}_{k}")
                    edited_list.append(edited_item)
            return edited_list

        # List of scalars — comma-separated text input
        return st.text_input(
            pretty,
            value=", ".join(str(v) for v in value),
            key=f"{key_prefix}_list",
        )

    # Fallback
    return st.text_input(pretty, value=str(value), key=f"{key_prefix}_fallback")


def view_po_folder(dealer_code: str, po_type: str):
    """PO type view — upload zone, file list, and OCR extraction results."""
    dealer_name = next((n for n, c in DEALERSHIPS.items() if c == dealer_code), dealer_code)
    fp = folder_path_for(dealer_code, po_type)
    fp.mkdir(parents=True, exist_ok=True)

    st.subheader(f"{dealer_name} — {po_type}")
    st.caption("Drop invoice / PO documents here for AI processing")

    uploaded = st.file_uploader(
        "Upload documents",
        type=["pdf", "png", "jpg", "jpeg", "tif", "tiff"],
        accept_multiple_files=True,
        key=f"upload_{dealer_code}_{po_type}",
        label_visibility="collapsed",
    )

    if uploaded:
        for f in uploaded:
            save_path = fp / f.name
            save_path.write_bytes(f.getbuffer())
        st.success(f"Saved {len(uploaded)} file(s)")

    st.divider()

    existing = sorted(f for f in fp.glob("*") if f.is_file() and not f.name.endswith(".ocr.json"))
    if existing:
        st.markdown(f"**Files in this folder ({len(existing)})**")

        for file_path in existing:
            with st.container(border=True):
                col_name, col_ocr, col_del = st.columns([6, 2, 1])
                with col_name:
                    st.markdown(f":material/description: {file_path.name}")
                with col_ocr:
                    if st.button("Run OCR", key=f"ocr_{file_path.name}", width="stretch"):
                        with st.spinner("Running OCR extraction... this may take 30-60 seconds"):
                            result = run_ocr(file_path)
                        if result is not None:
                            st.session_state[f"ocr_result_{file_path.name}"] = result
                            st.rerun()
                with col_del:
                    if st.button("Delete", key=f"del_{file_path.name}", help=f"Delete {file_path.name}"):
                        file_path.unlink()
                        cache = ocr_cache_path(file_path)
                        if cache.exists():
                            cache.unlink()
                        st.rerun()

                # Show OCR results — load from session state or disk cache
                result = st.session_state.get(f"ocr_result_{file_path.name}")
                if result is None:
                    result = load_ocr_result(file_path)
                    if result is not None:
                        st.session_state[f"ocr_result_{file_path.name}"] = result
                if result is not None:
                    st.divider()

                    # Validation status
                    val = result.get("_validation")
                    if val:
                        if val.get("needs_review"):
                            st.warning(val.get("notes", "Needs review"))
                        else:
                            st.success("All validation checks passed")

                    # PO type from folder (auto-filled) — map to endpoint enum
                    PO_TYPE_MAP = {
                        "Sublet PO": "SUBLET",
                        "OEM Stock Order": "OEM_STOCK_ORDER",
                        "OEM Special Order": "OEM_SPECIAL_ORDER",
                        "Vendor Stock Order": "VENDOR_STOCK_ORDER",
                        "Vendor Special Order": "VENDOR_SPECIAL_ORDER",
                        "Miscellaneous PO": "MISCELLANEOUS",
                        "Vendor Credit PO": "VENDOR_CREDIT_PO",
                    }
                    po_type_code = PO_TYPE_MAP.get(po_type, po_type.upper().replace(" ", "_"))

                    # Dynamic editable form — renders fields based on what OCR extracted
                    with st.form(key=f"form_{file_path.name}"):
                        st.markdown("#### Extracted data (editable)")

                        # Show PO type from folder
                        st.text_input("PO type (from folder)", value=po_type, disabled=True, key=f"pt_{file_path.name}")

                        # Split top-level fields into two columns to reduce scrolling
                        public_fields = [(k, v) for k, v in result.items() if not k.startswith("_")]
                        mid = (len(public_fields) + 1) // 2
                        col_left, col_right = st.columns(2)

                        edited = {}
                        with col_left:
                            for key, value in public_fields[:mid]:
                                edited[key] = render_field(key, value, f"{file_path.name}_{key}")

                        with col_right:
                            for key, value in public_fields[mid:]:
                                edited[key] = render_field(key, value, f"{file_path.name}_{key}")

                        # Submit button — sends to TS backend (non-blocking)
                        submitted = st.form_submit_button("Submit to Tekion", type="primary")

                        if submitted:
                            payload = dict(edited)
                            payload["po_type"] = po_type_code
                            payload["invoice_file_path"] = str(file_path)
                            # Send dealership name so Playwright can switch to the correct dealer
                            dealer_name = next((n for n, c in DEALERSHIPS.items() if c == dealer_code), None)
                            if dealer_name:
                                payload["dealer_name"] = dealer_name
                            print(f"[SUBMIT] Sending to /api/po/create -- keys: {list(payload.keys())}", flush=True)
                            st.session_state[f"submit_payload_{file_path.name}"] = payload
                            st.session_state[f"submit_status_{file_path.name}"] = "sending"
                            st.rerun()

                    # Show submit status
                    status = st.session_state.get(f"submit_status_{file_path.name}")
                    payload = st.session_state.get(f"submit_payload_{file_path.name}")
                    if status == "sending" and payload:
                        with st.spinner("Sending to Tekion backend... Check TS server terminal for automation logs."):
                            import requests
                            try:
                                resp = requests.post("http://localhost:3000/api/po/create", json=payload, timeout=120)
                                if resp.status_code == 200:
                                    st.session_state[f"submit_status_{file_path.name}"] = "done"
                                    st.session_state[f"submit_response_{file_path.name}"] = resp.json()
                                else:
                                    st.session_state[f"submit_status_{file_path.name}"] = "error"
                                    st.session_state[f"submit_response_{file_path.name}"] = f"Error {resp.status_code}: {resp.text}"
                            except requests.exceptions.ConnectionError:
                                st.session_state[f"submit_status_{file_path.name}"] = "error"
                                st.session_state[f"submit_response_{file_path.name}"] = "Could not connect to TS server. Is `npm run dev` running on port 3000?"
                            except Exception as e:
                                st.session_state[f"submit_status_{file_path.name}"] = "error"
                                st.session_state[f"submit_response_{file_path.name}"] = f"Submit failed: {e}"
                        st.rerun()
                    elif status == "done" and payload:
                        resp_data = st.session_state.get(f"submit_response_{file_path.name}")
                        st.success(f"Submitted to Tekion. Response: {resp_data}")
                        if st.button("Clear", key=f"clear_{file_path.name}"):
                            st.session_state[f"submit_status_{file_path.name}"] = None
                            st.session_state[f"submit_payload_{file_path.name}"] = None
                            st.session_state[f"submit_response_{file_path.name}"] = None
                            st.rerun()
                    elif status == "error" and payload:
                        err_msg = st.session_state.get(f"submit_response_{file_path.name}")
                        st.error(err_msg)
                        if st.button("Clear", key=f"clear_{file_path.name}"):
                            st.session_state[f"submit_status_{file_path.name}"] = None
                            st.session_state[f"submit_payload_{file_path.name}"] = None
                            st.session_state[f"submit_response_{file_path.name}"] = None
                            st.rerun()

                    # Collapsible raw views
                    with st.expander("Raw JSON", expanded=False):
                        display = {k: v for k, v in result.items() if not k.startswith("_")}
                        st.json(display)

                    with st.expander("Raw OCR text", expanded=False):
                        raw = result.get("_raw_text", "")
                        if raw:
                            st.text(raw)
                        else:
                            st.caption("No raw text available")
    else:
        st.info("No documents uploaded yet.")


# ── Sidebar overview ─────────────────────────────────────────────────────── #

with st.sidebar:
    st.header("Overview")
    total = 0
    for name, code in DEALERSHIPS.items():
        dealer_dir = UPLOAD_DIR / code
        if not dealer_dir.exists():
            continue
        count = sum(1 for _ in dealer_dir.rglob("*") if _.is_file())
        if count > 0:
            total += count
            st.text(f"{code} — {name}: {count}")
    st.divider()
    st.metric("Total documents", total)


# ── Main render ──────────────────────────────────────────────────────────── #

render_top_bar()
st.divider()

if st.session_state.nav_po is None:
    view_dealer(st.session_state.nav_dealer)
else:
    view_po_folder(st.session_state.nav_dealer, st.session_state.nav_po)
