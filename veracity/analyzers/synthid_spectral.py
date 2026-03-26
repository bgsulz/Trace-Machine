"""SynthID spectral analyzer — experimental frequency-domain watermark detection.

Uses pre-extracted carrier frequencies and phase templates 
to detect Google's SynthID watermark without calling any external API.

This is a LOCAL, OFFLINE analyzer.  It loads a ~12 MB codebook once at
import time and runs pure NumPy / SciPy / OpenCV signal processing.
"""

from __future__ import annotations

import logging
import os
import pickle
from dataclasses import dataclass
from typing import Dict, List, Optional

import cv2
import numpy as np
import pywt
from scipy.fft import fft2, ifft2, fftshift
from scipy import ndimage

from .context import AnalysisContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Codebook loading (once at import)
# ---------------------------------------------------------------------------
_CODEBOOK_PATH = os.path.join(os.path.dirname(__file__), "data", "synthid_codebook.pkl")
_CODEBOOK: Optional[dict] = None


def _load_codebook() -> Optional[dict]:
    global _CODEBOOK
    if _CODEBOOK is not None:
        return _CODEBOOK
    if not os.path.exists(_CODEBOOK_PATH):
        logger.warning("SynthID codebook not found at %s", _CODEBOOK_PATH)
        return None
    with open(_CODEBOOK_PATH, "rb") as f:
        _CODEBOOK = pickle.load(f)
    logger.info(
        "Loaded SynthID codebook (image_size=%s, carriers=%d)",
        _CODEBOOK.get("image_size"),
        len(_CODEBOOK.get("carriers", [])),
    )
    return _CODEBOOK


# Eagerly load on import so the first analysis doesn't pay the cost.
_load_codebook()


# ---------------------------------------------------------------------------
# Known carrier frequencies 
# ---------------------------------------------------------------------------
KNOWN_CARRIERS = [
    (14, 14), (-14, -14),
    (126, 14), (-126, -14),
    (98, -14), (-98, 14),
    (128, 128), (-128, -128),
    (210, -14), (-210, 14),
    (238, 14), (-238, -14),
]


# ---------------------------------------------------------------------------
# Detection result
# ---------------------------------------------------------------------------
@dataclass
class SpectralResult:
    is_watermarked: bool
    confidence: float
    correlation: float
    phase_match: float
    structure_ratio: float
    carrier_strength: float
    multi_scale_consistency: float
    details: Dict


# ---------------------------------------------------------------------------
# Denoising helpers
# ---------------------------------------------------------------------------

def _wavelet_denoise(channel: np.ndarray, wavelet: str = "db4", level: int = 3) -> np.ndarray:
    coeffs = pywt.wavedec2(channel, wavelet, level=level)
    detail = coeffs[-1][0]
    sigma = np.median(np.abs(detail)) / 0.6745
    threshold = sigma * np.sqrt(2 * np.log(channel.size))
    new_coeffs = [coeffs[0]]
    for details in coeffs[1:]:
        new_coeffs.append(tuple(pywt.threshold(d, threshold, mode="soft") for d in details))
    denoised = pywt.waverec2(new_coeffs, wavelet)
    return denoised[: channel.shape[0], : channel.shape[1]]


def _bilateral_denoise(image: np.ndarray) -> np.ndarray:
    if len(image.shape) == 2:
        return cv2.bilateralFilter(image.astype(np.float32), 9, 75, 75)
    result = np.zeros_like(image)
    for c in range(image.shape[2]):
        result[:, :, c] = cv2.bilateralFilter(image[:, :, c].astype(np.float32), 9, 75, 75)
    return result


def _nlm_denoise(image: np.ndarray) -> np.ndarray:
    img_uint8 = (image * 255).clip(0, 255).astype(np.uint8)
    if len(image.shape) == 2:
        denoised = cv2.fastNlMeansDenoising(img_uint8, None, 10, 7, 21)
    else:
        denoised = cv2.fastNlMeansDenoisingColored(img_uint8, None, 10, 10, 7, 21)
    return denoised.astype(np.float32) / 255.0


def _wiener_filter(channel: np.ndarray) -> np.ndarray:
    noise_variance = np.var(channel - ndimage.gaussian_filter(channel, sigma=2))
    f = fft2(channel)
    power = np.abs(f) ** 2
    signal_power = np.maximum(power - noise_variance, 0)
    wiener_ratio = signal_power / (signal_power + noise_variance + 1e-10)
    return np.real(ifft2(f * wiener_ratio))


def _extract_noise_single(image: np.ndarray, method: str, **kwargs) -> np.ndarray:
    img_f = image.astype(np.float32)
    if img_f.max() > 1:
        img_f = img_f / 255.0

    if method == "wavelet":
        wavelet = kwargs.get("wavelet", "db4")
        if len(img_f.shape) == 2:
            denoised = _wavelet_denoise(img_f, wavelet)
        else:
            denoised = np.zeros_like(img_f)
            for c in range(img_f.shape[2]):
                denoised[:, :, c] = _wavelet_denoise(img_f[:, :, c], wavelet)
    elif method == "bilateral":
        denoised = _bilateral_denoise(img_f)
    elif method == "nlm":
        denoised = _nlm_denoise(img_f)
    elif method == "wiener":
        if len(img_f.shape) == 2:
            denoised = _wiener_filter(img_f)
        else:
            denoised = np.zeros_like(img_f)
            for c in range(img_f.shape[2]):
                denoised[:, :, c] = _wiener_filter(img_f[:, :, c])
    else:
        raise ValueError(f"Unknown denoising method: {method}")

    return img_f - denoised


def _extract_noise_fused(image: np.ndarray) -> np.ndarray:
    noises: List[np.ndarray] = []
    weights: List[float] = []

    for wavelet in ("db4", "sym8", "coif3"):
        noises.append(_extract_noise_single(image, "wavelet", wavelet=wavelet))
        weights.append(1.0)

    noises.append(_extract_noise_single(image, "bilateral"))
    weights.append(0.8)

    noises.append(_extract_noise_single(image, "nlm"))
    weights.append(0.7)

    noises.append(_extract_noise_single(image, "wiener"))
    weights.append(0.6)

    w = np.array(weights) / sum(weights)
    return np.tensordot(w, np.array(noises), axes=([0], [0]))


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def _detect(image_array: np.ndarray, codebook: dict) -> SpectralResult:
    target_size = codebook["image_size"]  # 512
    img_resized = cv2.resize(image_array, (target_size, target_size))

    # -- Noise extraction and correlation with reference --
    noise = _extract_noise_fused(img_resized)
    ref_noise = codebook["reference_noise"]
    correlation = float(np.corrcoef(noise.ravel(), ref_noise.ravel())[0, 1])

    # -- Phase coherence at carrier frequencies --
    gray = np.mean(img_resized.astype(np.float32), axis=2) if len(img_resized.shape) == 3 else img_resized.astype(np.float32)
    f = fftshift(fft2(gray))
    magnitude = np.abs(f)
    phase = np.angle(f)

    center = target_size // 2
    carrier_scores: List[float] = []
    carrier_strengths: List[float] = []

    carriers_to_check = codebook.get("carriers", [])[:30]
    known_dicts = [{"frequency": freq, "phase": 0} for freq in KNOWN_CARRIERS]
    carriers_to_check = list(carriers_to_check) + known_dicts

    ref_phase = codebook.get("reference_phase")

    for carrier in carriers_to_check:
        freq = carrier["frequency"]
        y = freq[0] + center
        x = freq[1] + center
        if not (0 <= y < target_size and 0 <= x < target_size):
            continue
        actual_phase = phase[y, x]
        if ref_phase is not None:
            expected_phase = ref_phase[y, x]
        else:
            expected_phase = carrier.get("phase", 0)
        phase_diff = np.abs(np.angle(np.exp(1j * (actual_phase - expected_phase))))
        carrier_scores.append(1 - phase_diff / np.pi)
        carrier_strengths.append(float(magnitude[y, x]))

    avg_phase_match = float(np.mean(carrier_scores)) if carrier_scores else 0.0
    avg_carrier_strength = float(np.mean(carrier_strengths)) if carrier_strengths else 0.0

    # -- Noise structure ratio --
    noise_gray = np.mean(noise, axis=2) if len(noise.shape) == 3 else noise
    structure_ratio = float(np.std(noise_gray) / (np.mean(np.abs(noise_gray)) + 1e-10))

    # -- Multi-scale consistency --
    scales = [256, 512, 1024]
    scale_scores: List[float] = []
    for scale in scales:
        img_scaled = cv2.resize(image_array, (scale, scale))
        noise_scaled = _extract_noise_single(img_scaled, "wavelet")
        ref_scaled = cv2.resize(ref_noise, (scale, scale))
        corr = float(np.corrcoef(noise_scaled.ravel(), ref_scaled.ravel())[0, 1])
        scale_scores.append(corr)
    multi_scale_consistency = float(np.std(scale_scores))

    # -- Decision --
    # The codebook threshold (0.179) was calibrated on pure reference images.
    # Real content images produce much weaker correlation (~0.01–0.02 for
    # genuine Gemini output) because image content drowns out the watermark.
    # We use empirically-tuned thresholds for real-world images instead.
    _CORR_FLOOR = 0.003       # below this, indistinguishable from noise
    _CORR_STRONG = 0.025      # strong real-world watermark signal
    _PHASE_BASELINE = 0.5     # random-chance phase match
    _PHASE_CEIL = 0.2         # max meaningful excess over baseline

    # Correlation score: 0 at floor, 1 at strong
    corr_score = max(0.0, min(1.0,
        (correlation - _CORR_FLOOR) / (_CORR_STRONG - _CORR_FLOOR)
    ))

    # Phase score: excess over random baseline, normalised
    phase_excess = max(0.0, avg_phase_match - _PHASE_BASELINE)
    phase_score = min(1.0, phase_excess / _PHASE_CEIL)

    # Confidence: correlation-dominant (70%) with phase as supporting signal (30%)
    confidence = min(1.0, 0.70 * corr_score + 0.30 * phase_score)

    # Binary decision gate
    is_watermarked = confidence >= 0.50

    return SpectralResult(
        is_watermarked=bool(is_watermarked),
        confidence=float(confidence),
        correlation=correlation,
        phase_match=avg_phase_match,
        structure_ratio=structure_ratio,
        carrier_strength=avg_carrier_strength,
        multi_scale_consistency=multi_scale_consistency,
        details={
            "corr_score": round(corr_score, 4),
            "phase_score": round(phase_score, 4),
            "phase_excess": round(phase_excess, 4),
            "scale_correlations": scale_scores,
        },
    )


# ---------------------------------------------------------------------------
# Analyzer entry point
# ---------------------------------------------------------------------------

def run_synthid_spectral(context: AnalysisContext) -> dict[str, object]:
    """Analyzer plugin: spectral SynthID watermark detection."""
    codebook = _load_codebook()
    if codebook is None:
        return {
            "status": "ERROR",
            "summary": "SynthID codebook not available.",
            "data": {},
        }

    # Decode image bytes into a BGR numpy array, then convert to RGB.
    buf = np.frombuffer(context.image_bytes, dtype=np.uint8)
    image = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if image is None:
        return {
            "status": "ERROR",
            "summary": "Could not decode image for spectral analysis.",
            "data": {},
        }
    image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

    result = _detect(image, codebook)

    if result.is_watermarked:
        status = "DETECTED"
        summary = (
            f"SynthID watermark detected (confidence {result.confidence:.0%})."
        )
    elif result.confidence > 0.20:
        status = "UNCERTAIN"
        summary = (
            f"Possible SynthID signal (confidence {result.confidence:.0%})."
        )
    else:
        status = "NOT FOUND"
        summary = "No SynthID watermark detected."

    return {
        "status": status,
        "summary": summary,
        "data": {
            "is_watermarked": result.is_watermarked,
            "confidence": result.confidence,
            "confidence_pct": f"{result.confidence:.1%}",
            "correlation": round(result.correlation, 6),
            "phase_match": round(result.phase_match, 4),
            "structure_ratio": round(result.structure_ratio, 4),
            "carrier_strength": round(result.carrier_strength, 2),
            "multi_scale_consistency": round(result.multi_scale_consistency, 6),
            "details": result.details,
        },
    }
