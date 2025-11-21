from __future__ import annotations


def run_synthid_stub(image_bytes: bytes) -> tuple[str, str]:
    """Placeholder SynthID analyzer (real API pending)."""
    checksum = sum(image_bytes[:32]) % 5 if image_bytes else 0
    detected = checksum == 0
    status = "DETECTED" if detected else "NOT DETECTED"
    details = "SynthID integration pending (stub output)."
    if detected:
        details += f" Checksum bucket={checksum}."
    return status, details
