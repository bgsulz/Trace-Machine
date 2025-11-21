import base64

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from . import ingestion

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/analyze", methods=["POST"])
def analyze():
    file = request.files.get("file")
    image_url = request.form.get("image_url", "").strip()

    source = None
    try:
        if file and file.filename:
            source = "file"
            image_bytes = file.read()
            ingestion.validate_image_bytes(image_bytes)
            mime_type = file.mimetype or "application/octet-stream"
        elif image_url:
            source = "url"
            image_bytes, mime_type = ingestion.fetch_image_bytes(image_url)
        else:
            flash("Please provide an image file or a URL.")
            return redirect(url_for("main.index"))
    except ingestion.IngestionError as exc:
        flash(str(exc))
        return redirect(url_for("main.index"))

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    image_data_url = f"data:{mime_type};base64,{image_b64}"

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
        image_url=image_data_url,
        source=source,
        results=dummy_results,
    )
