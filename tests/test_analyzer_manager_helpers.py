from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.manager import (
    AnalyzerSpec,
    _error_payload,
    _format_result,
    run_single_analyzer,
)


def _make_context():
    return AnalysisContext(
        image_bytes=b"data",
        phash="ffffffffffffffff",
        whash="0000000000000000",
        registry_id=1,
        neighbors=[],
        width=10,
        height=10,
    )


def test_error_payload_formats_exception_message():
    payload = _error_payload(ValueError("bad input"))
    assert payload["status"] == "ERROR"
    assert payload["summary"] == "bad input"
    assert payload["data"] == {}


def test_format_result_populates_defaults():
    spec = AnalyzerSpec(name="Test", slug="test", func=lambda ctx: {})
    raw = {"status": "FOUND", "data": {"k": "v"}}

    formatted = _format_result(spec, raw)

    assert formatted["name"] == "Test"
    assert formatted["slug"] == "test"
    assert formatted["status"] == "FOUND"
    assert formatted["summary"] == ""
    assert formatted["details"] == ""
    assert formatted["data"] == {"k": "v"}
    assert formatted["template"] == spec.template
    assert formatted["tooltip"] == spec.tooltip


def test_run_single_analyzer_uses_spec_and_formats_result(monkeypatch):
    context = _make_context()
    events = []

    def _func(ctx):
        events.append(ctx.phash)
        return {"status": "OK", "summary": "fine", "data": {"x": 1}}

    spec = AnalyzerSpec(name="Dummy", slug="dummy", func=_func)
    monkeypatch.setattr(
        "veracity.analyzers.manager._ANALYZER_BY_SLUG", {"dummy": spec}, raising=False
    )

    result = run_single_analyzer(context, "dummy")

    assert events == ["ffffffffffffffff"]
    assert result["name"] == "Dummy"
    assert result["slug"] == "dummy"
    assert result["status"] == "OK"
    assert result["summary"] == "fine"
    assert result["data"] == {"x": 1}


def test_run_single_analyzer_returns_error_payload_on_exception(monkeypatch):
    context = _make_context()

    def _boom(ctx):  # noqa: ARG001
        raise RuntimeError("boom")

    spec = AnalyzerSpec(name="Explode", slug="explode", func=_boom)
    monkeypatch.setattr(
        "veracity.analyzers.manager._ANALYZER_BY_SLUG", {"explode": spec}, raising=False
    )

    result = run_single_analyzer(context, "explode")

    assert result["status"] == "ERROR"
    assert "boom" in result["summary"]
    assert result["data"] == {}
