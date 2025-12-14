from urllib.parse import unquote_plus, urlparse, parse_qs

from veracity.tools import generate_external_tools


def _extract_google_target(link: str) -> str | None:
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    targets = query.get("url")
    return targets[0] if targets else None


def test_generate_external_tools_with_public_url(app):
    public_url = "https://example.com/image.png"
    with app.test_request_context("/"):
        tools = generate_external_tools(public_url)

    reverse_tool = tools[0]
    google_link = reverse_tool["links"][0]["url"]
    target_url = _extract_google_target(google_link)

    assert target_url == public_url
    assert any(link["label"] == "TinEye" for link in reverse_tool["links"])


def test_generate_external_tools_falls_back_to_cached_route(app):
    analysis_id = "deadbeef1234"
    with app.test_request_context("/"):
        tools = generate_external_tools(None, analysis_id=analysis_id)

    reverse_tool = tools[0]
    google_link = reverse_tool["links"][0]["url"]
    target_url = _extract_google_target(google_link)
    assert target_url is not None
    # google_link url target is percent-encoded already
    decoded_target = unquote_plus(target_url)

    assert f"/analysis/{analysis_id}/raw" in decoded_target
    assert decoded_target.startswith("http")
    assert any(link["label"] == "TinEye" for link in reverse_tool["links"])
