import json

from urllib.parse import urlparse

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

from flask_wtf.csrf import CSRFError

from .analyzers.manager import get_active_analyzers
from .services.config_service import DONATION_GOAL_CENTS, get_global_config
from .web.routes.analysis import register_analysis_routes
from .web.routes.batch_api import register_batch_api_routes
from .web.routes.community import register_community_routes
from .web.ui import graph_outline, humanize_key, none_note, same_box, short_name, status_ui

bp = Blueprint("main", __name__)
bp.add_app_template_global(status_ui, "status_ui")
bp.add_app_template_global(short_name, "short_name")
bp.add_app_template_global(none_note, "none_note")
bp.add_app_template_global(graph_outline, "graph_outline")
bp.add_app_template_global(same_box, "same_box")
bp.add_app_template_filter(humanize_key, "humanize_key")

EXPIRED_MESSAGE = "Analysis expired. Please submit the image again."
RATE_LIMIT_MESSAGE = "Rate limit reached. Please wait a minute before trying again."


STALE_FORM_MESSAGE = "This page was open for a while, so that didn't go through. Please try again."
NO_SESSION_MESSAGE = (
    "Your browser didn't send this site's session cookie, so that didn't go "
    "through. Try again from the full Trace Machine page."
)


@bp.app_errorhandler(CSRFError)
def handle_csrf_error(error):
    """Recover from rejected form tokens instead of showing a bare 400.

    A stale or mismatched token (e.g. a tab left open overnight) is fixed by
    reloading the page, which issues a fresh one. A missing session cookie
    (e.g. third-party cookies blocked in an embedded view) isn't, so it gets
    an explanation without a pointless reload.
    """
    session_missing = "session token is missing" in (error.description or "")
    message = NO_SESSION_MESSAGE if session_missing else STALE_FORM_MESSAGE

    if request.headers.get("HX-Request"):
        response = make_response("", 200)
        if session_missing:
            # Leave the page as-is; the flash cookie wouldn't survive a reload.
            response.headers["HX-Reswap"] = "none"
            response.headers["HX-Trigger"] = json.dumps({"showToast": message})
        else:
            flash(message)
            response.headers["HX-Refresh"] = "true"
        return response

    flash(message)
    referrer = urlparse(request.referrer or "")
    same_origin = (referrer.scheme, referrer.netloc) == (request.scheme, request.host)
    return redirect(request.referrer if same_origin else url_for("main.index"))


@bp.errorhandler(429)
def handle_rate_limit(_error):
    """Handle rate limit exceeded errors."""
    if request.headers.get("HX-Request"):
        response = make_response("", 429)
        response.headers["HX-Trigger"] = json.dumps({"showToast": RATE_LIMIT_MESSAGE})
        return response

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
        analyzers=get_active_analyzers(),
    )


@bp.route("/info")
def analyzer_info():
    return render_template("info.html", analyzers=get_active_analyzers())


register_analysis_routes(bp, _expired_analysis_response)
register_community_routes(bp, _expired_analysis_response)
register_batch_api_routes(bp)


@bp.route("/dev/mini-test")
def dev_mini_test():
    """Dev-only page to test the analyze-mini iframe view."""
    if not current_app.debug:
        abort(404)
    return render_template("dev_mini_test.html")
