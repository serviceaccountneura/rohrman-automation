import io
import json
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import fitz  # pymupdf
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from pipeline import BASE_DIR, IMAGE_EXTS, get_client, extract_path
from doc_schema import default_extraction

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
RECORD_DIR = DATA_DIR / "records"
USERS_FILE = DATA_DIR / "users.json"
for d in (UPLOAD_DIR, RECORD_DIR):
    d.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTS = IMAGE_EXTS | {".pdf"}

app = Flask(__name__)
app.secret_key = "ap-automate-rohrman-dev-key"  # dev only
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB

# Gemini client is created lazily on first extraction (slow import / network).
_client = None


def client():
    global _client
    if _client is None:
        _client = get_client()
    return _client


# In-memory upload-job tracker for the progress bar (dev-grade, single process).
JOBS: dict[str, dict] = {}
STAGE_PCT = {"queued": 10, "ocr": 35, "extract": 75, "done": 100, "error": 100}
STAGE_LABEL = {
    "queued": "Queued…",
    "ocr": "Reading text (OCR)…",
    "extract": "Understanding the document…",
    "done": "Done",
    "error": "Failed",
}


def _set_stage(rec_id: str, stage: str):
    job = JOBS.setdefault(rec_id, {})
    job["stage"] = stage
    job["percent"] = STAGE_PCT.get(stage, 0)
    job["label"] = STAGE_LABEL.get(stage, stage)


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def load_users() -> dict:
    if USERS_FILE.exists():
        return json.loads(USERS_FILE.read_text())
    return {}


def save_users(users: dict):
    USERS_FILE.write_text(json.dumps(users, indent=2))


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*a, **kw)
    return wrapped


# --------------------------------------------------------------------------- #
# Records  (one JSON per invoice)
# --------------------------------------------------------------------------- #
def record_path(rec_id: str) -> Path:
    return RECORD_DIR / f"{rec_id}.json"


def load_record(rec_id: str) -> dict | None:
    p = record_path(rec_id)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def text_path(rec_id: str) -> Path:
    return RECORD_DIR / f"{rec_id}.txt"


def save_record(rec: dict):
    record_path(rec["id"]).write_text(json.dumps(rec, indent=2, default=str))
    # Also write the clean OCR text (stage-1 output) as a plain .txt sidecar.
    raw_text = (rec.get("extraction") or {}).get("_raw_text")
    if raw_text:
        text_path(rec["id"]).write_text(raw_text, encoding="utf-8")


def all_records() -> list[dict]:
    recs = [json.loads(p.read_text()) for p in RECORD_DIR.glob("*.json")]
    recs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return recs


def _find_label(node, *name_parts):
    """DFS for a string value (or a nested name/title) whose key contains any name_part."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            if any(p in str(k).lower() for p in name_parts):
                if isinstance(v, str) and v.strip():
                    return v
                if isinstance(v, dict):
                    for nk in ("name", "title", "id"):
                        if isinstance(v.get(nk), str) and v[nk].strip():
                            return v[nk]
        for k, v in node.items():
            if str(k).startswith("_"):
                continue
            r = _find_label(v, *name_parts)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_label(v, *name_parts)
            if r is not None:
                return r
    return None


def _find_amount(node, *name_parts):
    """DFS for a numeric value whose key contains any name_part."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) \
               and any(p in str(k).lower() for p in name_parts):
                return v
        for v in node.values():
            r = _find_amount(v, *name_parts)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_amount(v, *name_parts)
            if r is not None:
                return r
    return None


def summarize(rec: dict) -> dict:
    """Best-effort columns for the Document Intake table (schema shape is dynamic)."""
    ex = rec.get("extraction", {}) or {}
    created = rec.get("created_at", "")
    amount = _find_amount(ex, "total", "amount_due", "total_debit")
    return {
        "id": rec["id"],
        "doc_type": (ex.get("document_type") or "Document"),
        "vendor": _find_label(ex, "vendor", "supplier") or "—",
        "invoice": _find_label(ex, "invoice_number", "po_number", "purchase_order",
                               "entry_number", "invoice", "reference") or "—",
        "store": _find_label(ex, "dealership", "dealer", "store") or "—",
        "amount": f"${amount:,.2f}" if isinstance(amount, (int, float)) else "—",
        "edited_by": rec.get("edited_by", "AI"),
        "source": rec.get("source", "Upload"),
        "confidence": None,
        "needs_review": bool(ex.get("_needs_review")),
        "date": created,
        "date_human": human_date(created),
    }


def human_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%b %d, %Y %I:%M %p")


# --------------------------------------------------------------------------- #
# Auth routes
# --------------------------------------------------------------------------- #
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # No auth checks for now — accept anything (e.g. admin / admin) and continue.
        email = (request.form.get("email") or "admin").strip().lower()
        name = email.split("@")[0] or "admin"
        session["user"] = {"email": email, "name": name}
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""
        users = load_users()
        if not (name and email and pw):
            flash("All fields are required.", "error")
        elif pw != confirm:
            flash("Passwords do not match.", "error")
        elif email in users:
            flash("An account with that email already exists.", "error")
        else:
            users[email] = {
                "name": name,
                "role": request.form.get("role") or "Reviewer",
                "password": generate_password_hash(pw),
            }
            save_users(users)
            session["user"] = {"email": email, "name": name}
            return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------- #
# App routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return redirect(url_for("dashboard") if "user" in session else url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    rows = [summarize(r) for r in all_records()]
    stats = {
        "new_invoices": len(rows),
        "needs_review": sum(1 for r in rows if r["needs_review"]),
        "no_po": sum(1 for r in rows if r["invoice"] == "—"),
        "completed": sum(1 for r in rows if not r["needs_review"]),
    }
    return render_template("dashboard.html", stats=stats, rows=rows[:6], active="dashboard")


@app.route("/intake")
@login_required
def intake():
    rows = [summarize(r) for r in all_records()]
    return render_template("intake.html", rows=rows, active="intake")


def _process_upload(rec: dict):
    """Background worker: run two-stage extraction, updating JOBS for the progress bar."""
    rec_id = rec["id"]
    try:
        doc = extract_path(
            UPLOAD_DIR / rec["stored"], client(),
            progress=lambda stage: _set_stage(rec_id, stage),
        )
        rec["extraction"] = doc  # already a plain dict
        save_record(rec)
        _set_stage(rec_id, "done")
        JOBS[rec_id]["redirect"] = f"/document/{rec_id}"  # plain URL: no app context in thread
    except Exception as e:
        rec["extraction"] = default_extraction()
        rec["error"] = str(e)
        save_record(rec)
        _set_stage(rec_id, "error")
        JOBS[rec_id]["error"] = str(e)


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(error="No file selected."), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return jsonify(error="Unsupported file type. Upload a PDF or image."), 400

    rec_id = uuid.uuid4().hex[:12]
    stored = f"{rec_id}{ext}"
    file.save(UPLOAD_DIR / stored)

    rec = {
        "id": rec_id,
        "filename": secure_filename(file.filename),
        "stored": stored,
        "source": "Upload",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "edited_by": "AI",
    }
    _set_stage(rec_id, "queued")
    threading.Thread(target=_process_upload, args=(rec,), daemon=True).start()
    return jsonify(rec_id=rec_id)


@app.route("/status/<rec_id>")
@login_required
def status(rec_id):
    job = JOBS.get(rec_id)
    if not job:
        return jsonify(error="unknown job"), 404
    return jsonify(
        stage=job.get("stage"),
        percent=job.get("percent", 0),
        label=job.get("label", ""),
        done=job.get("stage") == "done",
        error=job.get("error"),
        redirect=job.get("redirect"),
    )


def _set_path(obj: dict, dotted: str, value):
    """Set a scalar at a dotted path like 'vendor.name' within a nested dict."""
    parts = dotted.split(".")
    cur = obj
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _clean_extraction(ex: dict) -> dict:
    """Strip our underscore sidecar keys so downloads/displays show only the model's JSON."""
    return {k: v for k, v in (ex or {}).items() if not str(k).startswith("_")}


@app.route("/document/<rec_id>")
@login_required
def document(rec_id):
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    ex = rec.get("extraction", {}) or {}
    return render_template(
        "detail.html", rec=rec, ex=ex, summary=summarize(rec),
        clean=_clean_extraction(ex), validation=ex.get("_validation") or {},
        raw_text=ex.get("_raw_text"),
        page_count=pdf_page_count(rec), active="intake",
    )


@app.route("/document/<rec_id>", methods=["POST"])
@login_required
def save_document(rec_id):
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    ex = rec.get("extraction", {}) or {}

    # Generic: scalar inputs are named by dotted path (e.g. "vendor.name"); write each back.
    for name, val in request.form.items():
        if name.startswith("_"):
            continue
        _set_path(ex, name, val or None)

    rec["extraction"] = ex
    rec["edited_by"] = session["user"]["name"]
    rec["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_record(rec)
    flash("Saved.", "success")
    return redirect(url_for("document", rec_id=rec_id))


@app.route("/document/<rec_id>/download")
@login_required
def download_json(rec_id):
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    buf = io.BytesIO(json.dumps(_clean_extraction(rec.get("extraction", {})), indent=2).encode())
    return send_file(buf, mimetype="application/json", as_attachment=True,
                     download_name=f"{Path(rec['filename']).stem}.json")


@app.route("/document/<rec_id>/text")
@login_required
def download_text(rec_id):
    """Download the clean OCR plain text (stage-1 output) for this document."""
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    raw_text = (rec.get("extraction") or {}).get("_raw_text") or ""
    buf = io.BytesIO(raw_text.encode("utf-8"))
    return send_file(buf, mimetype="text/plain; charset=utf-8", as_attachment=True,
                     download_name=f"{Path(rec['filename']).stem}.txt")


# --------------------------------------------------------------------------- #
# Document rendering (PDF page -> PNG, raw file)
# --------------------------------------------------------------------------- #
def pdf_page_count(rec: dict) -> int:
    p = UPLOAD_DIR / rec["stored"]
    if p.suffix.lower() != ".pdf":
        return 1
    with fitz.open(str(p)) as doc:
        return doc.page_count


@app.route("/preview/<rec_id>/<int:page>.png")
@login_required
def preview(rec_id, page):
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    p = UPLOAD_DIR / rec["stored"]
    if p.suffix.lower() != ".pdf":
        return send_file(p)
    with fitz.open(str(p)) as doc:
        if page < 0 or page >= doc.page_count:
            abort(404)
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csRGB)
        png = pix.tobytes("png")
    return send_file(io.BytesIO(png), mimetype="image/png")


@app.route("/file/<rec_id>")
@login_required
def raw_file(rec_id):
    rec = load_record(rec_id)
    if not rec:
        abort(404)
    return send_file(UPLOAD_DIR / rec["stored"], download_name=rec["filename"])


@app.context_processor
def inject_user():
    return {"current_user": session.get("user")}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
