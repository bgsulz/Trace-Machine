import os
import secrets

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from . import ingestion

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)

    return render_template("index.html", csrf_token=session["csrf_token"])


@bp.route("/analyze", methods=["POST"])
def analyze():
    form_token = request.form.get("csrf_token")
    session_token = session.get("csrf_token")

    if not form_token or not session_token or not secrets.compare_digest(form_token, session_token):
        flash("Invalid or missing CSRF token. Please try again.")
        return redirect(url_for("main.index"))

    file = request.files.get("file")
    image_url = request.form.get("image_url", "").strip()

    source = None
    try:
        if file and file.filename:
            source = "file"
            filename = _save_uploaded_file(file)
        elif image_url:
            source = "url"
            filename = ingestion.download_image_to_uploads(image_url)
        else:
            flash("Please provide an image file or a URL.")
            return redirect(url_for("main.index"))
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    image_url_path = url_for("main.uploaded_file", filename=filename)

    dummy_results = [
        {
            "name": "Digital Signature (C2PA)",
            "status": "N/A",
            "details": "Analyzer not implemented yet (Phase 1)",
        },
        {
            "name": "Google SynthID",
            "status": "N/A",
            "details": "Analyzer not implemented yet (Phase 1)",
        },
        {
            "name": "Human Consensus",
            "status": "N/A",
            "details": "Analyzer not implemented yet (Phase 1)",
        },
    ]

    return render_template(
        "result.html",
        image_url=image_url_path,
        source=source,
        results=dummy_results,
    )


@bp.route("/uploads/<path:filename>")
def uploaded_file(filename):
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    return send_from_directory(upload_folder, filename)


def _save_uploaded_file(file_storage):
    if not file_storage:
        raise ingestion.IngestionError("No file provided.")

    filename = file_storage.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed = current_app.config.get("ALLOWED_EXTENSIONS", set())

    if ext not in allowed:
        raise ingestion.IngestionError("Unsupported file type.")

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)

    random_name = secrets.token_hex(16)
    stored_name = f"{random_name}.{ext}" if ext else random_name
    path = os.path.join(upload_folder, stored_name)

    file_storage.save(path)

    return stored_name
