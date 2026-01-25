"""SynthID analyzer stub.

SynthID is Google's invisible watermarking technology for AI-generated images.
There's no TOS-compliant way to automatically detect it, so this analyzer
provides manual instructions for checking via Google reverse image search.
"""

from __future__ import annotations

from .context import AnalysisContext


def run_synthid(context: AnalysisContext) -> dict[str, object]:
    """Return a MANUAL status with instructions for checking SynthID."""
    return {
        "status": "MANUAL",
        "summary": "Check for Google's invisible AI watermark.",
        "data": {
            "header_action": {
                "type": "link",
                "label": "Check Google",
            },
        },
    }
