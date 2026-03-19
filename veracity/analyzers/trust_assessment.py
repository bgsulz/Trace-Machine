"""C2PA trust assessment — surfaces what the crypto actually proves.

Drop this alongside your existing c2pa_analyzer.py and call
``build_trust_assessment()`` from ``_run_c2pa_tool`` after you've
extracted the active manifest.

Integration point in _run_c2pa_tool (add after validation_status is built):

    validation_codes = extract_validation_codes(
        manifest_store=manifest_store,
        active_manifest=active_manifest,
        ingredients=ingredients,
    )
    trust_assessment = build_trust_assessment(
        active_manifest=active_manifest,
        validation_codes=validation_codes,
    )

Then include ``trust_assessment`` in your ``data`` dict.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Human-readable label maps
# ---------------------------------------------------------------------------

_TIMESTAMP_LABELS: dict[str, str] = {
    "rfc3161": "Verified by an independent Time Stamp Authority (TSA)",
    "rfc3161_unverified": "Time Stamp Authority present, but its certificate is not trusted",
    "clock_only": "Signer\u2019s own clock \u2014 not independently verified",
    "none": "No timestamp present",
}

_TRUST_TIER_LABELS: dict[str, str] = {
    "trust_list": "Certificate recognized by a C2PA trust list",
    "untrusted": "Certificate not recognized by any trust list",
    "unverified": "Trust status could not be determined",
}

_DURABILITY_LABELS: dict[str, str] = {
    "durable": "Should remain verifiable indefinitely",
    "at_risk": "At risk \u2014 no trusted timestamp to outlast certificate expiration",
    "expired_no_tsa": "Certificate expired without a trusted timestamp \u2014 manifest can no longer be verified",
    "expired_with_tsa": "Certificate expired, but a trusted timestamp preserves the manifest",
    "revoked": "Certificate revoked by its issuing authority",
}


# ---------------------------------------------------------------------------
# Validation-code extraction (structured, not stringified)
# ---------------------------------------------------------------------------

def extract_validation_codes(
    *,
    manifest_store: dict[str, Any],
    active_manifest: dict[str, Any],
    ingredients: Any,
) -> set[str]:
    """Pull normalised validation-status codes from every location the
    c2pa-python library might put them."""
    codes: set[str] = set()

    # Top-level and active-manifest validation_status lists
    for source in (
        manifest_store.get("validation_status"),
        active_manifest.get("validation_status"),
    ):
        _harvest_codes(source, codes)

    # Ingredient-level statuses
    if isinstance(ingredients, list):
        for ingredient in ingredients:
            if isinstance(ingredient, dict):
                _harvest_codes(ingredient.get("validation_status"), codes)

    # Newer c2pa-python: validation_results → {activeManifest: {success/failure/…}}
    vr = active_manifest.get("validation_results")
    if isinstance(vr, dict):
        for section in vr.values():
            if isinstance(section, dict):
                for bucket in ("success", "failure", "informational"):
                    _harvest_codes(section.get(bucket), codes)

    return codes


def _harvest_codes(source: Any, dest: set[str]) -> None:
    if not isinstance(source, list):
        return
    for item in source:
        if isinstance(item, str):
            dest.add(item.strip().lower())
        elif isinstance(item, dict):
            code = item.get("code") or item.get("status") or ""
            if isinstance(code, str) and code.strip():
                dest.add(code.strip().lower())


# ---------------------------------------------------------------------------
# Certificate validity (best-effort, requires `cryptography`)
# ---------------------------------------------------------------------------

def _parse_cert_validity(
    active_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Try to read the signing certificate's validity window.

    Returns a dict with ``not_before``, ``not_after`` (ISO strings),
    ``is_expired`` (bool), and ``issuer_cn`` — all nullable.
    """
    result: dict[str, Any] = {
        "not_before": None,
        "not_after": None,
        "is_expired": None,
        "days_until_expiry": None,
        "issuer_cn": None,
    }

    sig_info = active_manifest.get("signature_info") or {}
    cert_chain = sig_info.get("cert_chain")
    if not isinstance(cert_chain, list) or not cert_chain:
        return result

    try:
        import base64

        from cryptography.x509 import load_der_x509_certificate
        from cryptography.x509.oid import NameOID

        raw = cert_chain[0]
        cert_der = base64.b64decode(raw) if isinstance(raw, str) else raw
        cert = load_der_x509_certificate(cert_der)

        now = datetime.now(timezone.utc)
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc

        result["not_before"] = not_before.isoformat()
        result["not_after"] = not_after.isoformat()
        result["is_expired"] = now > not_after
        result["days_until_expiry"] = (not_after - now).days

        # Best-effort CN extraction
        try:
            cns = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
            if cns:
                result["issuer_cn"] = cns[0].value
        except Exception:
            pass

    except ImportError:
        logger.debug("cryptography library not available; skipping cert parsing")
    except Exception:
        logger.debug("Could not parse cert chain", exc_info=True)

    return result


# ---------------------------------------------------------------------------
# Core trust assessment builder
# ---------------------------------------------------------------------------

def build_trust_assessment(
    *,
    active_manifest: dict[str, Any],
    validation_codes: set[str],
) -> dict[str, Any]:
    """Build a structured, human-readable trust assessment.

    The returned dict is safe to drop straight into your Jinja context.
    """
    sig_info = active_manifest.get("signature_info") or {}

    # ── 1. Timestamp analysis ────────────────────────────────────────────
    tsa_trusted = any(
        "timestamp" in c and "trusted" in c for c in validation_codes
    )
    tsa_untrusted = any(
        "timestamp" in c and "untrusted" in c for c in validation_codes
    )
    tsa_mentioned = any("timestamp" in c for c in validation_codes)
    has_signing_time = bool(sig_info.get("time"))

    if tsa_trusted:
        timestamp_type = "rfc3161"
    elif tsa_untrusted or tsa_mentioned:
        timestamp_type = "rfc3161_unverified"
    elif has_signing_time:
        timestamp_type = "clock_only"
    else:
        timestamp_type = "none"

    # ── 2. Certificate trust tier ────────────────────────────────────────
    cred_trusted = any(
        "signingcredential" in c and "trusted" in c for c in validation_codes
    )
    cred_untrusted = any(
        "signingcredential" in c and "untrusted" in c for c in validation_codes
    )
    code_says_expired = any("expired" in c for c in validation_codes)
    code_says_revoked = any("revoked" in c for c in validation_codes)

    if cred_trusted:
        trust_tier = "trust_list"
    elif cred_untrusted:
        trust_tier = "untrusted"
    else:
        trust_tier = "unverified"

    # ── 3. Certificate validity from raw cert chain ──────────────────────
    cert_info = _parse_cert_validity(active_manifest)

    cert_expired: bool | None = None
    if code_says_expired:
        cert_expired = True
    elif cert_info["is_expired"] is not None:
        cert_expired = cert_info["is_expired"]

    cert_revoked = code_says_revoked or None

    # ── 4. Durability ────────────────────────────────────────────────────
    if cert_revoked:
        durability = "revoked"
    elif timestamp_type == "rfc3161" and cert_expired:
        durability = "expired_with_tsa"
    elif cert_expired and timestamp_type != "rfc3161":
        durability = "expired_no_tsa"
    elif timestamp_type == "rfc3161":
        durability = "durable"
    else:
        durability = "at_risk"

    # ── 5. Caveats (plain language, ordered by severity) ─────────────────
    caveats: list[dict[str, str]] = []

    if durability == "expired_no_tsa":
        caveats.append({
            "severity": "error",
            "text": (
                "The signing certificate has expired and this manifest has no "
                "trusted timestamp. The provenance data is still present, but "
                "it can no longer be cryptographically proven authentic."
            ),
        })

    if durability == "revoked":
        caveats.append({
            "severity": "error",
            "text": (
                "The signing certificate has been revoked by its issuing "
                "authority. This usually means the key was compromised or the "
                "signer is no longer authorized."
            ),
        })

    if trust_tier == "untrusted":
        caveats.append({
            "severity": "warning",
            "text": (
                "The signing certificate is not on any known trust list. "
                "Anyone can create a cryptographically valid C2PA signature "
                "using a self-made certificate. A valid signature proves the "
                "data has not been altered since signing \u2014 it does not "
                "prove who signed it."
            ),
        })
    elif trust_tier == "unverified":
        caveats.append({
            "severity": "warning",
            "text": (
                "Trust-list verification was not performed or returned no "
                "result. Without it, the signer\u2019s identity cannot be "
                "confirmed."
            ),
        })

    if trust_tier == "trust_list":
        caveats.append({
            "severity": "info",
            "text": (
                "This certificate is recognized by C2PA\u2019s own trust "
                "list \u2014 not the industry-standard web-PKI trust store "
                "(CCADB) that browsers use. C2PA\u2019s list is maintained "
                "by the Coalition for Content Provenance and Authenticity."
            ),
        })

    if timestamp_type == "clock_only":
        caveats.append({
            "severity": "warning",
            "text": (
                "The \u201csigned at\u201d time was set by the signer\u2019s "
                "own device clock and was not verified by an independent "
                "Time Stamp Authority. It could have been set to any value."
            ),
        })

    if durability == "at_risk":
        caveats.append({
            "severity": "warning",
            "text": (
                "This manifest has no trusted timestamp. When the signing "
                "certificate expires, the manifest will become unverifiable. "
                "This is a known gap in many C2PA implementations."
            ),
        })

    if timestamp_type == "rfc3161_unverified":
        caveats.append({
            "severity": "info",
            "text": (
                "A Time Stamp Authority signature is present, but its "
                "certificate could not be verified against a known TSA trust "
                "list. The timestamp may still be legitimate."
            ),
        })

    if durability == "expired_with_tsa":
        caveats.append({
            "severity": "info",
            "text": (
                "The signing certificate has expired, but a trusted timestamp "
                "proves the signature was created while the certificate was "
                "still valid. The manifest should remain verifiable."
            ),
        })

    # ── 6. Summary line ──────────────────────────────────────────────────
    summary_parts: list[str] = []

    if trust_tier == "trust_list":
        summary_parts.append("Signer recognized by C2PA trust list")
    elif trust_tier == "untrusted":
        summary_parts.append("Signer not recognized")
    else:
        summary_parts.append("Signer identity unverified")

    if durability == "durable":
        summary_parts.append("independently timestamped")
    elif durability == "expired_with_tsa":
        summary_parts.append("cert expired but timestamped")
    elif durability == "expired_no_tsa":
        summary_parts.append("cert expired, no timestamp")
    elif durability == "revoked":
        summary_parts.append("cert revoked")
    elif durability == "at_risk":
        summary_parts.append("no independent timestamp")

    summary_line = " \u00b7 ".join(summary_parts)

    # ── 7. Overall severity for the UI wrapper ───────────────────────────
    if durability in ("expired_no_tsa", "revoked"):
        overall_severity = "error"
    elif trust_tier in ("untrusted", "unverified") or durability == "at_risk":
        overall_severity = "warning"
    elif durability == "durable" and trust_tier == "trust_list":
        overall_severity = "good"
    else:
        overall_severity = "info"

    return {
        # Core fields
        "trust_tier": trust_tier,
        "trust_label": _TRUST_TIER_LABELS.get(trust_tier, "Unknown"),
        "timestamp_type": timestamp_type,
        "timestamp_label": _TIMESTAMP_LABELS.get(timestamp_type, "Unknown"),
        "durability": durability,
        "durability_label": _DURABILITY_LABELS.get(durability, "Unknown"),
        # Certificate details
        "cert_expired": cert_expired,
        "cert_revoked": cert_revoked or False,
        "cert_expiry_date": cert_info.get("not_after"),
        "cert_days_until_expiry": cert_info.get("days_until_expiry"),
        "cert_issuer_cn": cert_info.get("issuer_cn"),
        # UI helpers
        "summary_line": summary_line,
        "overall_severity": overall_severity,
        "caveats": caveats,
        "has_caveats": bool(caveats),
    }
