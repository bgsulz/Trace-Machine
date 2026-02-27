from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from .analyzers.manager import ANALYZERS, get_analyzer_spec
from .services.config_service import DONATION_GOAL_CENTS, get_global_config
from .web.routes.analysis import register_analysis_routes
from .web.routes.batch_api import register_batch_api_routes
from .web.routes.community import register_community_routes

bp = Blueprint("main", __name__)

EXPIRED_MESSAGE = "Analysis expired. Please submit the image again."
RATE_LIMIT_MESSAGE = "Rate limit reached (5 per hour). Please wait before trying again."


@bp.errorhandler(429)
def handle_rate_limit(_error):
    """Handle rate limit exceeded errors."""
    if request.headers.get("HX-Request"):
        analysis_id = request.view_args.get("analysis_id") if request.view_args else None
        spec = get_analyzer_spec("tineye")
        row = {
            "name": spec.name,
            "slug": spec.slug,
            "status": "ERROR",
            "summary": RATE_LIMIT_MESSAGE,
            "data": {},
            "template": spec.template,
            "tooltip": spec.tooltip,
            "info_id": f"info-{spec.slug}",
            "context": {"analysis_id": analysis_id},
        }
        return render_template("partials/analyzer_row.html", row=row), 429

    flash(RATE_LIMIT_MESSAGE)
    return redirect(url_for("main.index"))


def _expired_analysis_response():
    """Return a consistent response for expired analysis across all routes."""
    if request.headers.get("HX-Request"):
        flash(EXPIRED_MESSAGE)
        response = make_response("", 200)
        response.headers["HX-Redirect"] = url_for("main.index")
        return response

    flash(EXPIRED_MESSAGE)
    response = redirect(url_for("main.index"))
    response.status_code = 410
    return response


@bp.route("/")
def index():
    config = get_global_config()
    total_cents = config.total_donated_cents
    progress = min(total_cents / DONATION_GOAL_CENTS, 1) if DONATION_GOAL_CENTS else 0
    return render_template(
        "index.html",
        donation_total_cents=total_cents,
        donation_goal_cents=DONATION_GOAL_CENTS,
        donation_progress_percent=round(progress * 100, 2),
        donation_goal_met=total_cents >= DONATION_GOAL_CENTS,
    )


@bp.route("/info")
def analyzer_info():
    return render_template("info.html", analyzers=ANALYZERS)


register_analysis_routes(bp, _expired_analysis_response)
register_community_routes(bp, _expired_analysis_response)
register_batch_api_routes(bp)


@bp.route("/dev/mini-test")
def dev_mini_test():
    """Dev-only page to test the analyze-mini iframe view."""
    if not current_app.debug:
        abort(404)
    return render_template("dev_mini_test.html")
