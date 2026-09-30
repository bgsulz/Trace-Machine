from __future__ import annotations
import json
import re
import logging
import threading
from io import BytesIO
from typing import Any

from sqlalchemy.exc import IntegrityError

from .. import db
from ..models import ProvenanceFact
from .context import AnalysisContext
from .hash_utils import (
    iter_neighbor_views,
)
from ..ingestion import sniff_mime_type
from ..safe_fetch import FetchError, UnsafeURLError, safe_get
from .trust_assessment import build_trust_assessment, extract_validation_codes

try:  # pragma: no cover - import guard
    from c2pa import Reader
    from c2pa.c2pa import load_settings
except ImportError:  # pragma: no cover - handled at runtime
    Reader = None
    load_settings = None

logger = logging.getLogger(__name__)



_AVIF_BRANDS = {b"avif", b"avis", b"av01"}
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}

# Known producers, checked in order against the manifest's generator fields.
# More specific names come first so e.g. a Pixel camera isn't read as Gemini.
_KNOWN_PRODUCERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("claude", "anthropic"), "Claude (Anthropic)"),
    (("chatgpt",), "ChatGPT (OpenAI)"),
    (("gpt-image", "dall-e", "dall·e", "dalle"), "OpenAI image model"),
    (("sora",), "Sora (OpenAI)"),
    (("openai",), "OpenAI"),
    (("google pixel", "pixel camera"), "Google Pixel camera"),
    (("nano banana", "gemini", "imagen"), "Google Gemini"),
    (("firefly",), "Adobe Firefly"),
    (("photoshop",), "Adobe Photoshop"),
    (("lightroom",), "Adobe Lightroom"),
    (("microsoft designer", "bing image creator", "microsoft copilot"), "Microsoft"),
    (("meta ai", "muse image"), "Meta AI"),
    (("leica",), "Leica camera"),
    (("nikon",), "Nikon camera"),
    (("canon",), "Canon camera"),
    (("sony",), "Sony camera"),
    (("samsung galaxy", "galaxy s", "galaxy z"), "Samsung Galaxy"),
    (("truepic",), "Truepic"),
)
_AI_SOURCE_TYPES = {
    "trainedalgorithmicmedia",
    "compositewithtrainedalgorithmicmedia",
}
_CAMERA_SOURCE_TYPES = {
    "digitalcapture",
    "computationalcapture",
}
_CAPTURE_KEYS = (
    ("Make", "make"),
    ("Model", "model"),
    ("LensModel", "lens_model"),
    ("Software", "software"),
    ("DateTimeOriginal", "date_time_original"),
    ("DateTimeDigitized", "date_time_digitized"),
    ("CreateDate", "create_date"),
    ("ModifyDate", "modify_date"),
)
_ACTION_NAME_OVERRIDES = {
    "c2pa.created": "Created",
    "c2pa.opened": "Opened",
    "c2pa.placed": "Placed",
    "c2pa.copied": "Copied",
    "c2pa.converted": "Converted",
    "c2pa.cropped": "Cropped",
    "c2pa.resized": "Resized",
    "c2pa.edited": "Edited",
    "c2pa.filtered": "Filtered",
    "c2pa.transcoded": "Transcoded",
    "c2pa.exported": "Exported",
    "c2pa.redacted": "Redacted",
}


_REMOTE_MANIFEST_MAX_BYTES = 5 * 1024 * 1024
_REMOTE_URL_IN_ERROR = re.compile(r"remote manifests? from url (\S+)", re.IGNORECASE)
_PROVENANCE_XMP = re.compile(
    rb"dcterms:provenance(?:=\"([^\"]+)\"|>\s*([^<\s]+)\s*<)"
)
_thread_state = threading.local()


def _disable_remote_fetch_for_this_thread() -> None:
    """Stop the c2pa library from fetching remote manifests itself.

    Left on, it requests any URL an image's XMP names, from our server
    (server-side request forgery). Settings are per-thread in c2pa-rs and
    analyzers run in a thread pool, so apply them in every thread.
    """
    if getattr(_thread_state, "settings_loaded", False) or load_settings is None:
        return
    load_settings({"verify": {"remote_manifest_fetch": False}})
    _thread_state.settings_loaded = True


def _remote_manifest_url(error_message: str, image_bytes: bytes) -> str | None:
    """The remote manifest URL an image declares, if that's why reading failed."""
    if match := _REMOTE_URL_IN_ERROR.search(error_message):
        return match.group(1)
    if "remote" not in error_message.lower():
        return None
    if match := _PROVENANCE_XMP.search(image_bytes):
        return (match.group(1) or match.group(2)).decode("utf-8", "replace")
    return None


def _read_remote_manifest(url: str, mime_type: str, image_bytes: bytes) -> dict[str, Any] | str:
    """Fetch and validate a remote manifest; return its store or an error."""
    try:
        result = safe_get(url, max_bytes=_REMOTE_MANIFEST_MAX_BYTES)
    except (UnsafeURLError, FetchError) as exc:
        return str(exc)
    try:
        with Reader(mime_type, BytesIO(image_bytes), manifest_data=result.content) as reader:  # type: ignore[arg-type]
            return json.loads(reader.json())
    except Exception as exc:
        logger.info("Remote C2PA manifest at %s didn't validate: %s", url, exc)
        return "The referenced manifest couldn't be read."


def _detect_mime_type(image_bytes: bytes) -> str:
    mime_type = sniff_mime_type(image_bytes)
    if mime_type != "application/octet-stream":
        return mime_type

    # Pillow couldn't open it; fall back to the ISO-BMFF brands (major brand
    # first, then the compatible-brand list) for AVIF/HEIF.
    if len(image_bytes) >= 16 and image_bytes[4:8] == b"ftyp":
        box_size = int.from_bytes(image_bytes[0:4], "big")
        brands = [image_bytes[8:12]] + [
            image_bytes[i : i + 4]
            for i in range(16, min(box_size, len(image_bytes), 64), 4)
        ]
        for brand in brands:
            if brand in _AVIF_BRANDS:
                return "image/avif"
            if brand in _HEIF_BRANDS:
                return "image/heic"
    return "application/octet-stream"


def _run_c2pa_tool(image_bytes: bytes) -> dict[str, object]:
    """Run the low-level C2PA tool on raw bytes.

    Returns a dict with keys: status, summary, data.
    """
    if Reader is None:
        return {
            "status": "NOT AVAILABLE",
            "summary": "c2pa-python dependency is not installed.",
            "data": {},
        }

    mime_type = _detect_mime_type(image_bytes)
    remote_url: str | None = None
    _disable_remote_fetch_for_this_thread()
    try:
        with Reader(mime_type, BytesIO(image_bytes)) as reader:  # type: ignore[arg-type]
            manifest_store = json.loads(reader.json())
    except Exception as exc:  # pragma: no cover - defensive logging
        message = str(exc)
        if (
            "ManifestNotFound" in type(exc).__name__
            or "ManifestNotFound" in message
            or "no JUMBF data found" in message
        ):
            logger.info("No C2PA manifest found: %s", exc)
            return {
                "status": "NOT FOUND",
                "summary": "No C2PA signature found.",
                "data": {"has_manifest": False},
            }

        remote_url = _remote_manifest_url(message, image_bytes)
        if remote_url is None:
            logger.exception("C2PA analyzer failed")
            return {
                "status": "ERROR",
                "summary": f"Failed to read C2PA manifest: {exc}",
                "data": {},
            }
        fetched = _read_remote_manifest(remote_url, mime_type, image_bytes)
        if isinstance(fetched, str):
            return {
                "status": "REFERENCED",
                "summary": "Content Credentials are referenced but couldn't be retrieved.",
                "data": {
                    "has_manifest": False,
                    "remote_manifest_url": remote_url,
                    "remote_error": fetched,
                },
            }
        manifest_store = fetched

    manifests = manifest_store.get("manifests") or {}
    active_id = manifest_store.get("active_manifest")
    active_manifest = manifests.get(active_id) if active_id else None

    if not active_manifest:
        return {
            "status": "NOT FOUND",
            "summary": "No C2PA signature found.",
            "data": {"has_manifest": False},
        }

    signer = (
        active_manifest.get("signature_info", {}).get("issuer")
        or active_manifest.get("claim_generator")
        or "Unknown signer"
    )
    claim_generator = active_manifest.get("claim_generator") or ""
    ingredients = active_manifest.get("ingredients") or []
    provenance_depth = len(ingredients) if isinstance(ingredients, list) else None

    assertions = _extract_assertions(active_manifest)
    actions, origin_signals = _extract_actions_and_origin_signals(assertions)
    capture_details = _extract_capture_details(assertions)
    ingredient_summaries = _extract_ingredient_summaries(ingredients)
    validation_status = _collect_validation_status(
        manifest_store=manifest_store,
        active_manifest=active_manifest,
        ingredients=ingredients,
    )
    # Structured trust analysis
    validation_codes = extract_validation_codes(
        manifest_store=manifest_store,
        active_manifest=active_manifest,
        ingredients=ingredients,
    )
    trust_assessment = build_trust_assessment(
        active_manifest=active_manifest,
        validation_codes=validation_codes,
    )

    signature_status = _infer_signature_status(
        active_manifest=active_manifest,
        ingredients=ingredients,
        validation_status=validation_status,
    )
    origin_label = _build_origin_label(origin_signals)
    claim_generator_info = _extract_claim_generator_info(active_manifest)
    signature_info = _extract_signature_info(active_manifest)
    producer = identify_producer(
        software_agents=[a["software_agent"] for a in actions if a.get("software_agent")],
        claim_generators=[claim_generator, *claim_generator_info],
        issuer=signature_info.get("issuer", ""),
    )

    summary = f"Signed by {signer}"
    if producer and not _names_overlap(producer, signer):
        summary += f" · {producer}"
    if origin_label:
        summary += f" ({origin_label})"

    data: dict[str, object] = {
        "remote_manifest_url": remote_url,
        "signer": signer,
        "producer": producer,
        "tool": claim_generator,
        "has_manifest": True,
        "provenance_depth": provenance_depth,
        "origin_signals": origin_signals,
        "origin_label": origin_label,
        "manifest_label": active_id,
        "manifest_count": len(manifests) if isinstance(manifests, dict) else 0,
        "manifest_title": str(active_manifest.get("title") or ""),
        "manifest_format": str(active_manifest.get("format") or ""),
        "manifest_instance_id": str(active_manifest.get("instance_id") or ""),
        "assertion_labels": [entry["label"] for entry in assertions if entry["label"]],
        "actions": actions,
        "capture_details": capture_details,
        "ingredients": ingredient_summaries,
        "validation_status": validation_status,
        "claim_generator_info": claim_generator_info,
        "signature_info": signature_info,
        "trust_assessment": trust_assessment,
        "raw_manifest_store": manifest_store,
    }
    if signature_status is not None:
        data["signature_status"] = signature_status

    return {
        "status": "FOUND",
        "summary": summary,
        "data": data,
    }


def run_c2pa(context: AnalysisContext) -> dict[str, object]:
    """Run the C2PA analyzer with caching based on the analysis context."""

    # 1. Run the tool on the raw bytes ("new" analysis).
    result = _run_c2pa_tool(context.image_bytes)

    # If the tool is unavailable or errored, just return that directly.
    if result["status"] in {"NOT AVAILABLE", "ERROR"}:
        return result

    status = str(result.get("status", "UNKNOWN"))

    # 2. If we found fresh metadata, save it to the DB.
    if status == "FOUND":
        fact = ProvenanceFact(
            image_id=context.registry_id,
            analyzer="c2pa",
            data=str(result.get("summary", "")),
        )
        try:
            db.session.add(fact)
            db.session.commit()
        except IntegrityError as exc:  # duplicate fact is expected sometimes
            db.session.rollback()
            logger.info(
                "C2PA fact already persisted for image %s: %s",
                context.registry_id,
                exc.orig or exc,
            )
        except Exception:  # pragma: no cover - defensive
            db.session.rollback()
            logger.exception("Failed to persist C2PA provenance fact")

    # 3. Build a list of nearby matches from provenance facts on neighbors.
    matches: list[dict[str, object]] = []

    for neighbor_view in iter_neighbor_views(context, include_self=False):
        neighbor = neighbor_view["neighbor"]
        phash = neighbor_view["phash"]
        if not phash:
            continue

        neighbor_whash_val = neighbor_view["whash"]
        for fact in getattr(neighbor, "facts", []) or []:
            if fact.analyzer != "c2pa":
                continue

            matches.append(
                {
                    "phash": phash,
                    "whash": neighbor_whash_val,
                    "hash_display": neighbor_view["hash_display"],
                    "distance": neighbor_view["display_distance"],
                    "distance_phash": neighbor_view["phash_distance"],
                    "distance_whash": neighbor_view["whash_distance"],
                    "fact_data": str(fact.data),
                    "sources": neighbor_view["sources"],
                }
            )

    # If the tool itself found nothing but we have cached matches, adjust status.
    if status == "NOT FOUND" and matches:
        result["status"] = "SIMILAR"
        result["summary"] = (
            f"Found {len(matches)} visually similar image{'s' if len(matches) != 1 else ''} with C2PA metadata."
        )

    # Attach matches for the UI layer; always include the key for consistency.
    data = result.get("data") or {}
    data["matches"] = matches
    data["has_distant_matches"] = bool(matches)
    result["data"] = data

    return result


def _extract_assertions(active_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_assertions = active_manifest.get("assertions")
    if not isinstance(raw_assertions, list):
        return []

    assertions: list[dict[str, Any]] = []
    for assertion in raw_assertions:
        if not isinstance(assertion, dict):
            continue
        label = str(assertion.get("label") or assertion.get("url") or "")
        assertions.append(
            {
                "label": label,
                "data": assertion.get("data"),
            }
        )
    return assertions


def _extract_actions_and_origin_signals(
    assertions: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str]]:
    actions: list[dict[str, str]] = []
    source_signals: set[str] = set()

    for assertion in assertions:
        label = str(assertion.get("label") or "").lower()
        if "actions" not in label:
            continue

        payload = assertion.get("data")
        if not isinstance(payload, dict):
            continue
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list):
            continue

        for item in raw_actions:
            if not isinstance(item, dict):
                continue
            action_name = str(item.get("action") or item.get("label") or "").strip()
            action_display = _friendly_action_name(action_name)
            digital_source_type = _extract_digital_source_type(item)
            source_signal = _source_signal_from_digital_source_type(digital_source_type)
            if source_signal:
                source_signals.add(source_signal)
            software_agent = _extract_software_agent(item)

            action_entry: dict[str, str] = {}
            if action_name:
                action_entry["action"] = action_name
            if action_display:
                action_entry["action_display"] = action_display
            if digital_source_type:
                action_entry["digital_source_type"] = digital_source_type
            if software_agent:
                action_entry["software_agent"] = software_agent
            if source_signal:
                action_entry["origin_signal"] = source_signal

            if action_entry:
                actions.append(action_entry)

    return actions, sorted(source_signals)


def _names_overlap(a: str, b: str) -> bool:
    """True when two labels share a meaningful word (e.g. "Google")."""
    words = lambda text: {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 3}
    return bool(words(a) & words(b))


# The certificate issuer names who signed, not what made the image (a camera
# maker can sign its editing app), so it may only suggest these vendor labels.
_ISSUER_LABELS = {"Claude (Anthropic)", "OpenAI", "Google Gemini", "Adobe Firefly", "Truepic"}


def _match_producer(text: str) -> str:
    normalized = re.sub(r"[_\-]+", " ", text.lower())
    for tokens, label in _KNOWN_PRODUCERS:
        for token in tokens:
            if re.search(rf"(?<![a-z]){re.escape(token)}(?![a-z])", normalized):
                return label
    return ""


def identify_producer(
    *,
    software_agents: list[str],
    claim_generators: list[str],
    issuer: str,
) -> str:
    """Name the product behind a manifest, if it's one we recognize.

    Sources are checked from most to least specific, one value at a time and
    in order: the software recorded on actions (the first action is usually
    the one that created the image), then the claim generator (what wrote the
    manifest). The certificate issuer is a last resort, and only for vendors.
    Names are matched as whole words, so "Pixelmator" isn't a Pixel camera.
    """
    for value in [*software_agents, *claim_generators]:
        if value and (label := _match_producer(str(value))):
            return label
    label = _match_producer(issuer or "")
    return label if label in _ISSUER_LABELS else ""


def _extract_digital_source_type(action: dict[str, Any]) -> str:
    direct = action.get("digitalSourceType") or action.get("digital_source_type")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    parameters = action.get("parameters")
    if isinstance(parameters, dict):
        nested = parameters.get("digitalSourceType") or parameters.get(
            "digital_source_type"
        )
        if isinstance(nested, str) and nested.strip():
            return nested.strip()

    return ""


def _friendly_action_name(action_name: str) -> str:
    if not action_name:
        return ""
    if action_name in _ACTION_NAME_OVERRIDES:
        return _ACTION_NAME_OVERRIDES[action_name]

    normalized = action_name
    if normalized.startswith("c2pa."):
        normalized = normalized[5:]
    normalized = normalized.replace("_", " ").replace("-", " ").strip()
    if not normalized:
        return action_name
    return normalized.title()


def _source_signal_from_digital_source_type(value: str) -> str | None:
    if not value:
        return None
    normalized = value.split("/")[-1].replace("-", "").replace("_", "").lower()
    if normalized in _AI_SOURCE_TYPES:
        return "ai"
    if normalized in _CAMERA_SOURCE_TYPES:
        return "camera"
    return None


def _extract_software_agent(action: dict[str, Any]) -> str:
    raw_agent = action.get("softwareAgent") or action.get("software_agent")
    if isinstance(raw_agent, str):
        return raw_agent.strip()
    if not isinstance(raw_agent, dict):
        return ""

    name = str(raw_agent.get("name") or raw_agent.get("identifier") or "").strip()
    version = str(raw_agent.get("version") or "").strip()
    if name and version:
        return f"{name} {version}"
    return name


def _build_origin_label(origin_signals: list[str]) -> str:
    has_ai = "ai" in origin_signals
    has_camera = "camera" in origin_signals
    if has_ai and has_camera:
        return "Mixed signal: AI generation and camera capture"
    if has_ai:
        return "Generative AI signal"
    if has_camera:
        return "Camera capture signal"
    return ""


def _extract_capture_details(assertions: list[dict[str, Any]]) -> dict[str, str]:
    capture: dict[str, str] = {}
    for assertion in assertions:
        label = str(assertion.get("label") or "").lower()
        if "stds.exif" not in label and "c2pa.exif" not in label:
            continue

        payload = assertion.get("data")
        if not isinstance(payload, dict):
            continue

        for raw_key, normalized_key in _CAPTURE_KEYS:
            if normalized_key in capture:
                continue
            value = _lookup_ci(payload, raw_key)
            if value:
                capture[normalized_key] = value

    return capture


def _lookup_ci(payload: dict[str, Any], key: str) -> str:
    for existing_key, value in payload.items():
        if str(existing_key).lower() != key.lower():
            continue
        if value is None:
            return ""
        return str(value).strip()
    return ""


def _extract_ingredient_summaries(ingredients: Any) -> list[dict[str, Any]]:
    if not isinstance(ingredients, list):
        return []

    summaries: list[dict[str, Any]] = []
    for ingredient in ingredients:
        if not isinstance(ingredient, dict):
            continue

        summary: dict[str, Any] = {}
        for field in (
            "title",
            "relationship",
            "format",
            "claim_generator",
            "document_id",
            "instance_id",
            "active_manifest",
        ):
            value = ingredient.get(field)
            if value not in (None, ""):
                summary[field] = str(value)

        ingredient_validation = ingredient.get("validation_status")
        if isinstance(ingredient_validation, list):
            clean = [str(item) for item in ingredient_validation if str(item).strip()]
            if clean:
                summary["validation_status"] = clean

        if summary:
            summaries.append(summary)

    return summaries


def _collect_validation_status(
    *,
    manifest_store: dict[str, Any],
    active_manifest: dict[str, Any],
    ingredients: Any,
) -> list[str]:
    statuses: list[str] = []

    for source in (
        manifest_store.get("validation_status"),
        active_manifest.get("validation_status"),
    ):
        if isinstance(source, list):
            statuses.extend(str(item) for item in source if str(item).strip())

    if isinstance(ingredients, list):
        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                continue
            raw = ingredient.get("validation_status")
            if isinstance(raw, list):
                statuses.extend(str(item) for item in raw if str(item).strip())

    # Preserve order while removing duplicates.
    unique: list[str] = []
    seen: set[str] = set()
    for item in statuses:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _infer_signature_status(
    *,
    active_manifest: dict[str, Any],
    ingredients: Any,
    validation_status: list[str],
) -> str | None:
    validation_nodes: list[dict[str, Any]] = []
    validation_results = active_manifest.get("validation_results")
    if isinstance(validation_results, dict):
        active_validation = validation_results.get("activeManifest")
        if isinstance(active_validation, dict):
            validation_nodes.append(active_validation)

    if isinstance(ingredients, list) and ingredients:
        first = ingredients[0]
        if isinstance(first, dict):
            first_validation = (first.get("validation_results") or {}).get(
                "activeManifest"
            )
            if isinstance(first_validation, dict):
                validation_nodes.append(first_validation)

    found_success = False
    for node in validation_nodes:
        failures = node.get("failure")
        if isinstance(failures, list) and failures:
            return "invalid"
        successes = node.get("success")
        if isinstance(successes, list) and successes:
            found_success = True

    failure_markers = ("invalid", "mismatch", "failure", "untrusted", "revoked")
    success_markers = ("validated", "trusted")

    for status in validation_status:
        normalized = status.lower()
        if any(marker in normalized for marker in failure_markers):
            return "invalid"
        if any(marker in normalized for marker in success_markers):
            found_success = True

    if found_success:
        return "valid"
    return None


def _extract_claim_generator_info(active_manifest: dict[str, Any]) -> list[str]:
    raw = active_manifest.get("claim_generator_info")
    if not isinstance(raw, list):
        return []

    info: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("identifier") or "").strip()
        version = str(item.get("version") or "").strip()
        if name and version:
            info.append(f"{name} {version}")
        elif name:
            info.append(name)
    return info


def _extract_signature_info(active_manifest: dict[str, Any]) -> dict[str, str]:
    raw = active_manifest.get("signature_info")
    if not isinstance(raw, dict):
        return {}

    signature_info: dict[str, str] = {}
    for key in ("issuer", "alg", "time", "cert_serial_number"):
        value = raw.get(key)
        if value in (None, ""):
            continue
        signature_info[key] = str(value)
    return signature_info
