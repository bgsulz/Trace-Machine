from veracity.analyzers.context import AnalysisContext


def test_analysis_context_defaults_width_and_height_to_zero():
    context = AnalysisContext(
        image_bytes=b"payload",
        phash="ffffffffffffffff",
        whash="0000000000000000",
        registry_id=7,
        neighbors=[],
    )

    assert context.width == 0
    assert context.height == 0
