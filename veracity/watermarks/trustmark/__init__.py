"""Adobe TrustMark watermark decoding on CPU, without PyTorch.

TrustMark is the open (MIT) watermark behind Adobe's "Durable Content
Credentials": a 100-bit identifier hidden in the pixels that survives
metadata stripping and can re-link an image to its C2PA manifest.

``bchecc.py`` and ``datalayer.py`` are vendored unchanged from
github.com/adobe/trustmark (python-hailo/trustmark_hailo, commit 59bde8b);
see LICENSE. This module mirrors that package's decode path
(``pipeline.TrustMarkHailo.decode``), running Adobe's published ONNX decoders
with onnxruntime so a small CPU-only server can afford it (~47 MB per model,
tens of milliseconds per image).
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import tempfile
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .datalayer import DataLayer

logger = logging.getLogger(__name__)

MODEL_REMOTE_HOST = "https://cai-watermark.adobe.net/watermarking/trustmark-models/"
# Checksums published alongside the models in Adobe's package.
MODEL_CHECKSUMS = {
    "decoder_Q.onnx": "90427159004ddbf5128271ba0684cf42",
    "decoder_P.onnx": "2c77408a11bf4f22bb3dcf9e5706dfdd",
}
# Q is TrustMark's default (and what Adobe's C2PA example uses); P is the
# high-quality "perceptual" variant. A mark only decodes with its own variant.
VARIANTS = ("Q", "P")
_DECODER_RESOLUTION = {"Q": 256, "P": 224}
_ASPECT_RATIO_LIMIT = {"Q": 2.0, "P": 0.0}  # P always uses a centre square crop
_SECRET_LEN = 100


class ModelUnavailable(Exception):
    """The decoder model isn't installed and couldn't be downloaded."""


@dataclass(frozen=True)
class TrustMarkHit:
    variant: str
    schema: int  # BCH error-correction schema the mark was encoded with
    payload: str  # the decoded identifier, as a bit string
    confirmations: int = 0  # perturbed copies that decoded to the same payload


SCHEMA_NAMES = {0: "BCH_SUPER", 1: "BCH_5", 2: "BCH_4", 3: "BCH_3"}

# A raw decode is not enough: on ordinary unwatermarked photos, roughly 1 in
# 20 decodes "successfully" by chance (measured on 210 unmarked images, almost
# all with the weakest error correction). Real marks are built to survive mild
# edits, chance decodes aren't, so a hit must reproduce on perturbed copies.
# Measured: every chance decode failed all perturbations; every real mark made
# with Adobe's official encoder passed at least 3 of 4.
_MIN_CONFIRMATIONS = 2


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - integrity check against Adobe's published sums
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_model(model_dir: Path, variant: str, *, download: bool) -> Path:
    """Return the path to a verified decoder model, downloading it if allowed."""
    name = f"decoder_{variant}.onnx"
    path = model_dir / name
    if path.is_file() and _md5(path) == MODEL_CHECKSUMS[name]:
        return path
    if not download:
        raise ModelUnavailable(f"{name} isn't installed in {model_dir}")

    model_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading TrustMark %s (once only)", name)
    fd, tmp = tempfile.mkstemp(dir=model_dir, suffix=".part")
    os.close(fd)
    try:
        # Fixed, trusted host (not user input), so plain urllib is fine here.
        urllib.request.urlretrieve(MODEL_REMOTE_HOST + name, tmp)  # noqa: S310
        if _md5(Path(tmp)) != MODEL_CHECKSUMS[name]:
            raise ModelUnavailable(f"{name} failed its checksum")
        os.replace(tmp, path)
    except OSError as exc:
        raise ModelUnavailable(f"Couldn't download {name}: {exc}") from exc
    finally:
        Path(tmp).unlink(missing_ok=True)
    return path


class TrustMarkDecoder:
    """Thread-safe, lazily loaded decoders for each TrustMark variant."""

    def __init__(self, model_dir: Path, *, download: bool = True, threads: int = 2):
        self.model_dir = Path(model_dir)
        self.download = download
        self.threads = threads
        self._sessions: dict[str, object] = {}
        self._lock = threading.Lock()
        self._ecc = DataLayer(_SECRET_LEN, verbose=False, encoding_mode=1)

    def _session(self, variant: str):
        session = self._sessions.get(variant)
        if session is not None:
            return session
        with self._lock:
            if variant not in self._sessions:
                import onnxruntime as ort

                path = ensure_model(self.model_dir, variant, download=self.download)
                options = ort.SessionOptions()
                options.intra_op_num_threads = self.threads
                options.inter_op_num_threads = 1
                self._sessions[variant] = ort.InferenceSession(
                    str(path), options, providers=["CPUExecutionProvider"]
                )
        return self._sessions[variant]

    def _decode_variant(self, image: Image.Image, variant: str) -> tuple[str, int] | None:
        session = self._session(variant)
        logits = session.run(None, {session.get_inputs()[0].name: _model_input(image, variant)})[0]
        bits = (logits > 0).astype(np.uint8)
        payload, detected, schema = self._ecc.decode_bitstream(bits, "binary")[0]
        return (str(payload), int(schema)) if detected else None

    def decode(self, image: Image.Image, *, confirm: bool = True) -> TrustMarkHit | None:
        """Try each variant; return the first confirmed mark, if any."""
        rgb = image.convert("RGB")
        for variant in VARIANTS:
            raw = self._decode_variant(rgb, variant)
            if raw is None:
                continue
            payload, schema = raw
            if not confirm:
                return TrustMarkHit(variant, schema, payload)
            confirmations = sum(
                self._decode_variant(copy, variant) == raw for copy in _perturbations(rgb)
            )
            if confirmations >= _MIN_CONFIRMATIONS:
                return TrustMarkHit(variant, schema, payload, confirmations)
            logger.debug("Unconfirmed TrustMark %s decode (%d/3); treating as chance", variant, confirmations)
        return None


def _perturbations(image: Image.Image):
    """Mild edits a real TrustMark survives: re-compression, resize, small crop."""
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=85)
    buffer.seek(0)
    with Image.open(buffer) as jpeg:
        yield jpeg.convert("RGB")
    width, height = image.size
    yield image.resize((max(1, int(width * 0.9)), max(1, int(height * 0.9))), Image.BICUBIC)
    dx, dy = int(width * 0.02), int(height * 0.02)
    yield image.crop((dx, dy, width - dx, height - dy))


def _model_input(image: Image.Image, variant: str) -> np.ndarray:
    """Crop and scale like Adobe's pipeline, as (1, 3, H, W) float32 in [-1, 1]."""
    width, height = image.size
    aspect = max(width, height) / max(1, min(width, height))
    if aspect > _ASPECT_RATIO_LIMIT[variant]:
        side = min(width, height)
        left, top = (width - side) // 2, (height - side) // 2
        image = image.crop((left, top, left + side, top + side))
    size = _DECODER_RESOLUTION[variant]
    resized = image.resize((size, size), Image.BILINEAR)
    arr = np.asarray(resized, dtype=np.float32) / 255.0 * 2.0 - 1.0
    return arr.transpose(2, 0, 1)[None].copy()
