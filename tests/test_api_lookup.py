from datetime import UTC, datetime, timedelta

from veracity import db
from veracity.models import (
    ImageConsensus,
    ImageRegistry,
    ImageSource,
    ProvenanceFact,
    SynthIDReport,
)


def _seed_image(
    app,
    phash="aabbccdd11223344",
    url="https://example.com/photo.jpg",
    vote_real=0,
    vote_edited=0,
    vote_ai=0,
    facts=(),
    synthid_reports=(),
    created_at=None,
):
    """Insert a registry entry with optional consensus, facts, and synthid reports."""
    with app.app_context():
        reg = ImageRegistry(
            phash=phash,
            whash=phash,
            created_at=created_at or datetime.now(UTC),
        )
        db.session.add(reg)
        db.session.flush()

        src = ImageSource(image_id=reg.id, url=url)
        db.session.add(src)

        if vote_real or vote_edited or vote_ai:
            cons = ImageConsensus(
                image_id=reg.id,
                vote_real=vote_real,
                vote_edited=vote_edited,
                vote_ai=vote_ai,
            )
            db.session.add(cons)

        for analyzer, data in facts:
            db.session.add(ProvenanceFact(image_id=reg.id, analyzer=analyzer, data=data))

        for i, result in enumerate(synthid_reports):
            db.session.add(
                SynthIDReport(
                    image_id=reg.id,
                    voter_id=f"voter-{i}",
                    result=result,
                )
            )

        db.session.commit()
        return reg.id


def _lookup(client, urls):
    resp = client.post("/api/lookup", json={"urls": urls})
    assert resp.status_code == 200
    return resp.get_json()["results"]


def _entry(results, url):
    assert url in results
    entry = results[url]
    assert "match_count" in entry
    assert "matches" in entry
    return entry


def _single_match(results, url):
    entry = _entry(results, url)
    assert entry["match_count"] == 1
    assert len(entry["matches"]) == 1
    return entry["matches"][0]


def test_empty_results(client):
    """Unknown URLs return an explicit empty match list."""
    url = "https://nowhere.example.com/nope.jpg"
    results = _lookup(client, [url])
    entry = _entry(results, url)
    assert entry["match_count"] == 0
    assert entry["matches"] == []


def test_known_url_match(client, app):
    """A known source URL returns one matched registry image."""
    url = "https://example.com/known.jpg"
    _seed_image(app, url=url, vote_real=10, vote_edited=2, vote_ai=1)

    match = _single_match(_lookup(client, [url]), url)
    assert match["vote_real"] == 10
    assert match["vote_edited"] == 2
    assert match["vote_ai"] == 1
    assert match["total_votes"] == 13
    assert match["verdict"] == "real"
    assert match["matched_source_urls"] == [url]


def test_dethumbnail_expansion(client, app):
    """A preview URL can resolve via dethumbnail candidate matching."""
    stored_url = "https://i.redd.it/abc1234567890.jpg"
    _seed_image(app, url=stored_url, phash="1111111111111111", vote_ai=5)

    preview_url = "https://preview.redd.it/abc1234567890.jpg?width=640"
    match = _single_match(_lookup(client, [preview_url]), preview_url)
    assert match["vote_ai"] == 5
    assert stored_url in match["matched_source_urls"]


def test_dethumbnail_collision_preserves_all_inputs(client, app, monkeypatch):
    """When two inputs map to one candidate URL, both keys return data."""
    full_url = "https://i.redd.it/collision123.jpg"
    preview_url = "https://preview.redd.it/collision123.jpg?width=640"
    _seed_image(app, url=full_url, phash="1111222233334444", vote_ai=2)

    monkeypatch.setattr("veracity.lookup_service.get_full_res_url", lambda _: full_url)
    results = _lookup(client, [preview_url, full_url])

    preview_match = _single_match(results, preview_url)
    full_match = _single_match(results, full_url)
    assert preview_match["phash"] == "1111222233334444"
    assert full_match["phash"] == "1111222233334444"


def test_lookup_returns_all_historical_mappings_sorted_newest_first(client, app):
    """If a URL maps to multiple images over time, return all in stable order."""
    url = "https://example.com/replaced.jpg"
    base = datetime(2025, 1, 1, tzinfo=UTC)

    _seed_image(
        app,
        phash="1111111111111111",
        url=url,
        vote_real=1,
        created_at=base,
    )
    _seed_image(
        app,
        phash="2222222222222222",
        url=url,
        vote_ai=3,
        created_at=base + timedelta(days=1),
    )

    entry = _entry(_lookup(client, [url]), url)
    assert entry["match_count"] == 2
    assert len(entry["matches"]) == 2
    assert entry["matches"][0]["created_at"] >= entry["matches"][1]["created_at"]
    assert entry["matches"][0]["phash"] == "2222222222222222"
    assert entry["matches"][1]["phash"] == "1111111111111111"


def test_max_url_cap(client):
    """More than 50 URLs returns 400."""
    urls = [f"https://example.com/{i}.jpg" for i in range(51)]
    resp = client.post("/api/lookup", json={"urls": urls})
    assert resp.status_code == 400
    assert "50" in resp.get_json()["error"]


def test_exactly_max_urls_accepted(client):
    """Exactly 50 URLs is accepted."""
    urls = [f"https://example.com/{i}.jpg" for i in range(50)]
    resp = client.post("/api/lookup", json={"urls": urls})
    assert resp.status_code == 200


def test_invalid_json_body(client):
    """Non-JSON body returns 400."""
    resp = client.post(
        "/api/lookup",
        data="not json",
        content_type="text/plain",
    )
    assert resp.status_code == 400


def test_missing_urls_key(client):
    """JSON without 'urls' key returns 400."""
    resp = client.post("/api/lookup", json={"bad": "payload"})
    assert resp.status_code == 400


def test_c2pa_fact_reflected(client, app):
    """When a c2pa ProvenanceFact exists, c2pa is True."""
    url = "https://example.com/c2pa.jpg"
    _seed_image(app, url=url, phash="c2c2c2c2c2c2c2c2", facts=[("c2pa", "signed")])

    match = _single_match(_lookup(client, [url]), url)
    assert match["c2pa"] is True


def test_no_c2pa_fact(client, app):
    """Without c2pa fact, c2pa is False."""
    url = "https://example.com/noc2pa.jpg"
    _seed_image(app, url=url, phash="d3d3d3d3d3d3d3d3")

    match = _single_match(_lookup(client, [url]), url)
    assert match["c2pa"] is False


def test_synthid_detected_reflected(client, app):
    """More detected than not_detected reports yields synthid=True."""
    url = "https://example.com/synthid.jpg"
    _seed_image(
        app,
        url=url,
        phash="e4e4e4e4e4e4e4e4",
        synthid_reports=["detected", "detected", "not_detected"],
    )

    match = _single_match(_lookup(client, [url]), url)
    assert match["synthid"] is True


def test_synthid_not_detected_reflected(client, app):
    """More not_detected than detected reports yields synthid=False."""
    url = "https://example.com/nosynthid.jpg"
    _seed_image(
        app,
        url=url,
        phash="f5f5f5f5f5f5f5f5",
        synthid_reports=["not_detected", "not_detected", "detected"],
    )

    match = _single_match(_lookup(client, [url]), url)
    assert match["synthid"] is False


def test_synthid_tied_reports_returns_null(client, app):
    """Tied SynthID votes return null."""
    url = "https://example.com/synthid-tie.jpg"
    _seed_image(
        app,
        url=url,
        phash="abababababababab",
        synthid_reports=["detected", "not_detected"],
    )

    match = _single_match(_lookup(client, [url]), url)
    assert match["synthid"] is None


def test_no_consensus_verdict_null(client, app):
    """Image with no votes has verdict null and total_votes=0."""
    url = "https://example.com/novotes.jpg"
    _seed_image(app, url=url, phash="0000000000000001")

    match = _single_match(_lookup(client, [url]), url)
    assert match["verdict"] is None
    assert match["total_votes"] == 0


def test_non_string_urls_filtered(client, app):
    """Non-string entries in the urls array are filtered out."""
    url = "https://example.com/real.jpg"
    _seed_image(app, url=url, phash="abab121234345656", vote_real=1)

    results = _lookup(client, [url, 123, None, ""])
    assert list(results.keys()) == [url]
    match = _single_match(results, url)
    assert match["vote_real"] == 1
