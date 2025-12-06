from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class AnalysisContext:
    image_bytes: bytes
    phash: str
    whash: str
    registry_id: int
    neighbors: List[object]
