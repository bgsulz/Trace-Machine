from veracity.analyzers import AnalyzerSpec, run_all_analyzers


def test_run_all_analyzers_preserves_order():
    events = []

    def make_spec(name: str) -> AnalyzerSpec:
        def _fn(_: bytes):
            events.append(name)
            return "OK", f"details-{name}"

        return AnalyzerSpec(name=name, func=_fn)

    analyzers = [make_spec("A"), make_spec("B"), make_spec("C")]

    results = run_all_analyzers(b"payload", analyzers)

    assert [row["name"] for row in results] == [spec.name for spec in analyzers]
    assert events == ["A", "B", "C"]


def test_run_all_analyzers_handles_exceptions():
    def ok(_: bytes):
        return "OK", "fine"

    def boom(_: bytes):  # pragma: no cover - executed in thread
        raise RuntimeError("boom")

    analyzers = [
        AnalyzerSpec(name="Good", func=ok),
        AnalyzerSpec(name="Bad", func=boom),
    ]

    results = run_all_analyzers(b"payload", analyzers)

    result_map = {row["name"]: row for row in results}
    assert result_map["Good"]["status"] == "OK"
    assert result_map["Bad"]["status"] == "ERROR"
    assert "boom" in result_map["Bad"]["details"]
