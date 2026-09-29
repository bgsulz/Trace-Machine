"""Presentation helpers shared by templates.

Analyzers report a raw ``status`` string (``FOUND``, ``MANUAL``, ...). The UI
collapses those into a handful of display states so every surface renders the
same vocabulary:

- ``found``   – the check surfaced a signal worth reading.
- ``none``    – the check ran and found nothing (which is not proof of anything).
- ``action``  – the check needs the user to do something (verification portals).
- ``off``     – the check is unavailable on this deployment.
- ``error``   – the check failed.
- ``loading`` – the check is still running.
"""

from __future__ import annotations

from typing import Any

_STATUS_STATES: dict[str, tuple[str, str]] = {
    "FOUND": ("found", "Found"),
    "DETECTED": ("found", "Detected"),
    "REPORTED": ("found", "Reported"),
    "SIMILAR": ("found", "On similar image"),
    "NOT FOUND": ("none", "None found"),
    "CHECKED": ("none", "Checked, none"),
    "MANUAL": ("action", "Needs you"),
    "NOT AVAILABLE": ("off", "Unavailable"),
    "ERROR": ("error", "Error"),
    "LOADING": ("loading", "Running"),
    "RUNNING": ("loading", "Running"),
}

_SLUG_LABELS: dict[tuple[str, str], str] = {
    ("human", "FOUND"): "Has votes",
    ("human", "NOT FOUND"): "No votes",
    ("exif", "FOUND"): "AI metadata",
    ("c2pa", "FOUND"): "Manifest found",
    ("invisible", "FOUND"): "Watermark",
    ("invisible", "NOT FOUND"): "No local hit",
}

_SHORT_NAMES: dict[str, str] = {
    "c2pa": "C2PA",
    "exif": "Metadata",
    "invisible": "Watermarks",
    "synthid": "Portals",
    "human": "Community",
    "distant": "Similar images",
    "contained": "Contained regions",
}


# Shown when a check finds nothing, so absence isn't mistaken for a clean result.
_NONE_NOTES: dict[str, str] = {
    "c2pa": (
        "Screenshots, re-encoding, and most social platforms strip C2PA data, "
        "so a missing manifest says nothing about where an image came from."
    ),
    "exif": (
        "Metadata is easy to remove or rewrite. Finding no AI metadata doesn't "
        "mean no AI was involved."
    ),
    "invisible": (
        "Local decoders only recognize a few open watermark schemes. A miss "
        "doesn't mean the image is clean."
    ),
}


_SCALARS = (str, int, float, bool)


def graph_outline(parsed: Any) -> list[dict[str, Any]] | None:
    """Summarize embedded node-graph JSON (ComfyUI) into readable nodes.

    Handles both ComfyUI shapes: the API "prompt" (``{id: {class_type,
    inputs}}``) and the UI "workflow" (``{"nodes": [{type, widgets_values}]}``).
    Only scalar settings are kept; links between nodes are dropped. Returns
    ``None`` for anything else so callers can fall back to raw JSON.
    """
    if isinstance(parsed, dict) and parsed and all(
        isinstance(node, dict) and "class_type" in node for node in parsed.values()
    ):
        def order(item: tuple[str, Any]) -> tuple[int, str]:
            key = str(item[0])
            return (int(key), key) if key.isdigit() else (1 << 30, key)

        outline = []
        for node_id, node in sorted(parsed.items(), key=order):
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            title = (node.get("_meta") or {}).get("title") if isinstance(node.get("_meta"), dict) else None
            outline.append(
                {
                    "id": node_id,
                    "type": str(node.get("class_type")),
                    "title": title,
                    "fields": [
                        (key, value)
                        for key, value in inputs.items()
                        if isinstance(value, _SCALARS) and value != ""
                    ],
                }
            )
        return outline

    nodes = parsed.get("nodes") if isinstance(parsed, dict) else None
    if isinstance(nodes, list) and nodes and all(isinstance(n, dict) and "type" in n for n in nodes):
        outline = []
        for node in sorted(nodes, key=lambda n: (n.get("order", 0) if isinstance(n.get("order"), int) else 0)):
            values = node.get("widgets_values")
            if isinstance(values, dict):
                fields = [(k, v) for k, v in values.items() if isinstance(v, _SCALARS) and v != ""]
            elif isinstance(values, list):
                scalars = [v for v in values if isinstance(v, _SCALARS) and v != ""]
                fields = [("values", " · ".join(str(v) for v in scalars))] if scalars else []
            else:
                fields = []
            outline.append(
                {
                    "id": node.get("id"),
                    "type": str(node.get("type")),
                    "title": node.get("title"),
                    "fields": fields,
                }
            )
        return outline

    return None


def humanize_key(key: Any) -> str:
    text = str(key).replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else text


def none_note(slug: str | None) -> str:
    return _NONE_NOTES.get(slug or "", "")


def status_ui(status: Any, slug: str | None = None) -> dict[str, str]:
    """Return ``{"state", "label"}`` for an analyzer status."""
    key = str(status or "LOADING").strip().upper()
    state, label = _STATUS_STATES.get(key, ("none", key.title()))
    if slug:
        label = _SLUG_LABELS.get((slug, key), label)
    return {"state": state, "label": label}


def short_name(slug: str | None, fallback: str = "") -> str:
    return _SHORT_NAMES.get(slug or "", fallback or (slug or ""))
