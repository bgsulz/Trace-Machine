from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

def _transform_reddit(url: str) -> Optional[str]:
    """
    Convert preview.redd.it or external-preview.redd.it URLs into the original
    i.redd.it asset.
    """
    match = re.search(
        r"https?://(?:preview|external-preview)\.redd\.it/([a-zA-Z0-9_\-]+\.(?:jpg|png|gif|webp))",
        url,
    )
    if not match:
        return None
    filename = match.group(1)
    base, ext = filename.rsplit('.', 1)
    new_filename = f"{base[-13:]}.{ext}"
    return f"https://i.redd.it/{new_filename}"


def _transform_twitter(url: str) -> Optional[str]:
    """
    Force Twitter/X media files (pbs.twimg.com) to request name=orig.
    """
    if "pbs.twimg.com/media/" not in url:
        return None

    base_match = re.search(r"(https?://pbs\.twimg\.com/media/[^?]+)", url)
    if not base_match:
        return None

    base_url = base_match.group(1)
    fmt_match = re.search(r"format=([a-z]+)", url)
    ext = fmt_match.group(1) if fmt_match else "jpg"
    return f"{base_url}?format={ext}&name=orig"


_RULES: Iterable[Callable[[str], Optional[str]]] = (
    _transform_reddit,
    _transform_twitter,
)


def get_full_res_url(current_url: Optional[str]) -> Optional[str]:
    """
    Return a higher-resolution version of `current_url` if a matching rule exists.
    """
    if not current_url:
        return None

    for rule in _RULES:
        try:
            candidate = rule(current_url)
        except Exception:
            continue
        if candidate and candidate != current_url:
            return candidate
    return None
