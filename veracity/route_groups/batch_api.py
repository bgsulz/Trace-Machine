from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

from .. import csrf, limiter
from ..batch_service import MAX_BATCH_URLS, process_batch_urls
from ..lookup_service import lookup_urls

MAX_LOOKUP_URLS = 50


def register_batch_api_routes(bp: Blueprint) -> None:
    @bp.route("/batch")
    def batch():
        return render_template("batch.html")

    @bp.route("/batch", methods=["POST"])
    @limiter.limit("3/minute")
    def batch_submit():
        raw_text = (request.form.get("urls") or "").strip()
        if not raw_text:
            flash("Please paste at least one image URL.")
            return redirect(url_for("main.batch"))

        urls = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if not urls:
            flash("Please paste at least one image URL.")
            return redirect(url_for("main.batch"))

        if len(urls) > MAX_BATCH_URLS:
            flash(f"Maximum {MAX_BATCH_URLS} URLs per batch.")
            return redirect(url_for("main.batch"))

        # Validate each URL format
        valid_urls: list[str] = []
        valid_positions: list[tuple[int, str]] = []
        results_by_index: dict[int, dict] = {}
        for idx, url in enumerate(urls):
            if not url.startswith(("http://", "https://")):
                results_by_index[idx] = {
                    "url": url,
                    "analysis_id": None,
                    "error": "Invalid URL format",
                    "image_data_url": None,
                    "public_url_display": url[:60],
                }
            else:
                valid_urls.append(url)
                valid_positions.append((idx, url))

        if valid_urls:
            batch_results = process_batch_urls(valid_urls)
            by_url = {row["url"]: row for row in batch_results}
            for idx, url in valid_positions:
                row = by_url.get(url)
                if row is not None:
                    results_by_index[idx] = dict(row)

        results = [results_by_index[idx] for idx in range(len(urls)) if idx in results_by_index]

        return render_template("batch_results.html", results=results)

    @bp.route("/api/lookup", methods=["POST"])
    @csrf.exempt
    @limiter.limit("30/minute")
    @limiter.limit("500/hour", key_func=lambda: "global")
    def api_lookup():
        data = request.get_json(silent=True)
        if not data or not isinstance(data.get("urls"), list):
            return jsonify({"error": "Request body must be JSON with a 'urls' array."}), 400

        urls = data["urls"]
        if len(urls) > MAX_LOOKUP_URLS:
            return jsonify({"error": f"Maximum {MAX_LOOKUP_URLS} URLs per request."}), 400

        # Filter to strings only
        urls = [u for u in urls if isinstance(u, str) and u]

        results = lookup_urls(urls)
        return jsonify({"results": results})

