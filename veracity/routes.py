import base64

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import ingestion, db
from .models import ImageConsensus
from .analyzers.manager import run_all_analyzers

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

    analyzer_results = run_all_analyzers(image_bytes)

    return render_template(
        "result.html",
        image_url=image_data_url,
        source=source,
        results=analyzer_results,
    )


@bp.route("/vote", methods=["POST"])
def vote():
    phash = (request.form.get("phash") or "").strip()
    vote_kind = (request.form.get("vote") or "").strip().lower()

    if not phash or vote_kind not in {"real", "ai"}:
        flash("Invalid vote request.")
        return redirect(url_for("main.index"))

    record = ImageConsensus.query.filter_by(phash=phash).first()
    if record is None:
        record = ImageConsensus(phash=phash)
        db.session.add(record)

    if vote_kind == "real":
        record.vote_real = (record.vote_real or 0) + 1
    else:
        record.vote_ai = (record.vote_ai or 0) + 1

    db.session.commit()

    flash("Thanks for your vote.")
    return redirect(url_for("main.index"))
