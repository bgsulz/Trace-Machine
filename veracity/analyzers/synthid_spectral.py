"""SynthID spectral analyzer — experimental frequency-domain watermark detection.

Uses pre-extracted carrier frequencies and phase templates 
to detect Google's SynthID watermark without calling any external API.

This is a LOCAL, OFFLINE analyzer.  It loads a ~12 MB codebook once at
import time and runs pure NumPy / SciPy / OpenCV signal processing.
"""

from __future__ import annotations

import logging
import math
import os
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
_CODEBOOKS: Optional[List[dict]] = None


def _codebook_candidates() -> List[Tuple[str, Path]]:
    analyzer_dir = Path(__file__).resolve().parent
    repo_root = analyzer_dir.parent.parent
    return [
        ("nb1", analyzer_dir / "data" / "synthid_codebook.pkl"),
        ("nb2-blackonly", analyzer_dir / "data" / "synthid_codebook_nb2_blackonly.pkl"),
        ("nb2-blackonly", repo_root / ".experiments" / "synthid_codebook_nb2_blackonly.pkl"),
        ("nb2-mixed", analyzer_dir / "data" / "synthid_codebook_nb2.pkl"),
        ("nb2-mixed", repo_root / ".experiments" / "synthid_codebook_nb2.pkl"),
    ]


def _infer_codebook_mode(codebook: dict) -> str:
    carriers = codebook.get("carriers", [])[:12]
    if not carriers:
        return "nb1"

    strong_pairs = []
    for carrier in carriers:
        fy, fx = carrier.get("frequency", (0, 0))
        strong_pairs.append((abs(int(fy)), abs(int(fx))))

    if any(
        pair in {
            (64, 64),
            (64, 128),
            (128, 64),
            (64, 192),
            (192, 64),
            (128, 192),
            (192, 128),
            (192, 192),
            (0, 256),
            (256, 0),
        }
        for pair in strong_pairs
    ):
        return "nb2"
    return "nb1"


def _load_codebooks() -> List[dict]:
    global _CODEBOOKS
    if _CODEBOOKS is not None:
        return _CODEBOOKS

    loaded: List[dict] = []
    seen_paths: set[str] = set()
    for profile_name, path in _codebook_candidates():
        path_str = str(path)
        if path_str in seen_paths or not path.exists():
            continue
        seen_paths.add(path_str)
        with path.open("rb") as f:
            codebook = pickle.load(f)
        codebook["_path"] = path_str
        codebook["_profile_name"] = profile_name
        codebook["_mode"] = _infer_codebook_mode(codebook)
        loaded.append(codebook)
        logger.info(
            "Loaded SynthID codebook %s from %s (image_size=%s, carriers=%d, mode=%s)",
            profile_name,
            path,
            codebook.get("image_size"),
            len(codebook.get("carriers", [])),
            codebook["_mode"],
        )

    if not loaded:
        logger.warning("No SynthID codebooks found in configured locations")

    _CODEBOOKS = loaded
    return _CODEBOOKS


# Eagerly load on import so the first analysis doesn't pay the cost.
_load_codebooks()


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
    profile_name: str
    detection_mode: str
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


def _safe_corrcoef(a: np.ndarray, b: np.ndarray) -> float:
    a_flat = a.ravel()
    b_flat = b.ravel()
    a_std = float(np.std(a_flat))
    b_std = float(np.std(b_flat))
    if a_std < 1e-10 or b_std < 1e-10:
        return 0.0
    return float(np.corrcoef(a_flat, b_flat)[0, 1])


def _wrapped_phase_score(actual_phase: float, expected_phase: float) -> float:
    phase_diff = np.abs(np.angle(np.exp(1j * (actual_phase - expected_phase))))
    return float(1 - phase_diff / np.pi)


def _local_background_mean(
    magnitude: np.ndarray,
    y: int,
    x: int,
    radius: int = 4,
    exclusion_radius: int = 1,
) -> float:
    y0 = max(0, y - radius)
    y1 = min(magnitude.shape[0], y + radius + 1)
    x0 = max(0, x - radius)
    x1 = min(magnitude.shape[1], x + radius + 1)
    patch = magnitude[y0:y1, x0:x1].copy()

    cy = y - y0
    cx = x - x0
    yy, xx = np.ogrid[: patch.shape[0], : patch.shape[1]]
    mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= exclusion_radius ** 2
    patch[mask] = np.nan

    mean_val = float(np.nanmean(patch))
    if math.isnan(mean_val):
        return 0.0
    return mean_val


def _compute_codebook_tile_templates(codebook: dict, tile_size: int) -> np.ndarray:
    cache_key = f"_tile_templates_{tile_size}"
    cached = codebook.get(cache_key)
    if cached is not None:
        return cached

    ref_noise = codebook["reference_noise"].astype(np.float32)
    templates = []
    for channel_idx in range(ref_noise.shape[2]):
        channel = ref_noise[:, :, channel_idx]
        h, w = channel.shape
        trimmed = channel[: h - (h % tile_size), : w - (w % tile_size)]
        folded = trimmed.reshape(
            trimmed.shape[0] // tile_size,
            tile_size,
            trimmed.shape[1] // tile_size,
            tile_size,
        ).mean(axis=(0, 2))
        folded = folded - float(np.mean(folded))
        templates.append(folded.astype(np.float32))

    result = np.array(templates, dtype=np.float32)
    codebook[cache_key] = result
    return result


def _fold_channel(channel: np.ndarray, tile_size: int, shift_y: int, shift_x: int) -> np.ndarray:
    h, w = channel.shape
    rolled = np.roll(channel, shift=(-shift_y, -shift_x), axis=(0, 1))
    trimmed = rolled[: h - (h % tile_size), : w - (w % tile_size)]
    folded = trimmed.reshape(
        trimmed.shape[0] // tile_size,
        tile_size,
        trimmed.shape[1] // tile_size,
        tile_size,
    ).mean(axis=(0, 2))
    return folded.astype(np.float32)


def _nb2_shift_phase_score(
    image_array: np.ndarray,
    codebook: dict,
    shift_period: int,
) -> tuple[float, tuple[int, int], float]:
    target_size = codebook["image_size"]
    img_resized = cv2.resize(image_array, (target_size, target_size)).astype(np.float32)

    channel_ffts = []
    for channel_idx in range(img_resized.shape[2]):
        fft_map = fftshift(fft2(img_resized[:, :, channel_idx]))
        channel_ffts.append((np.abs(fft_map), np.angle(fft_map)))

    carriers = [
        carrier
        for carrier in codebook.get("carriers", [])[:24]
        if carrier.get("coherence", 0.0) >= 0.9
    ]
    if not carriers:
        carriers = codebook.get("carriers", [])[:16]

    ref_phase = codebook.get("reference_phase")
    center = target_size // 2
    best_score = 0.0
    best_shift = (0, 0)
    best_strength = 0.0

    for shift_y in range(shift_period):
        for shift_x in range(shift_period):
            score_total = 0.0
            weight_total = 0.0
            strength_total = 0.0
            strength_count = 0

            for carrier in carriers:
                fy, fx = carrier["frequency"]
                y = fy + center
                x = fx + center
                if not (0 <= y < target_size and 0 <= x < target_size):
                    continue

                expected_base = (
                    float(ref_phase[y, x])
                    if ref_phase is not None
                    else float(carrier.get("phase", 0.0))
                )
                shift_term = 2 * np.pi * ((fy * shift_y) + (fx * shift_x)) / target_size
                expected_phase = expected_base - shift_term
                carrier_weight = float(carrier.get("coherence", 1.0))

                for magnitude, phase in channel_ffts:
                    score_total += carrier_weight * _wrapped_phase_score(phase[y, x], expected_phase)
                    weight_total += carrier_weight
                    bg = _local_background_mean(magnitude, y, x)
                    if bg > 1e-6:
                        strength_total += float(magnitude[y, x] / bg)
                        strength_count += 1

            if weight_total <= 0:
                continue

            score = score_total / weight_total
            if score > best_score:
                best_score = float(score)
                best_shift = (shift_y, shift_x)
                best_strength = float(strength_total / max(1, strength_count))

    return best_score, best_shift, best_strength


def _nb2_tile_score(
    image_array: np.ndarray,
    codebook: dict,
    tile_size: int,
    shift_period: int,
) -> tuple[float, tuple[int, int], list[float]]:
    target_size = codebook["image_size"]
    img_resized = cv2.resize(image_array, (target_size, target_size)).astype(np.float32) / 255.0
    templates = _compute_codebook_tile_templates(codebook, tile_size)

    highpassed = np.zeros_like(img_resized)
    for channel_idx in range(img_resized.shape[2]):
        low = ndimage.gaussian_filter(img_resized[:, :, channel_idx], sigma=4)
        highpassed[:, :, channel_idx] = img_resized[:, :, channel_idx] - low

    best_score = -1.0
    best_shift = (0, 0)
    best_channels: list[float] = []

    for shift_y in range(shift_period):
        for shift_x in range(shift_period):
            channel_scores: list[float] = []
            for channel_idx in range(highpassed.shape[2]):
                folded = _fold_channel(highpassed[:, :, channel_idx], tile_size, shift_y, shift_x)
                folded = folded - float(np.mean(folded))
                channel_scores.append(_safe_corrcoef(folded, templates[channel_idx]))

            score = float(np.mean(channel_scores))
            if score > best_score:
                best_score = score
                best_shift = (shift_y, shift_x)
                best_channels = channel_scores

    return best_score, best_shift, best_channels


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def _detect_nb1(image_array: np.ndarray, codebook: dict) -> SpectralResult:
    target_size = codebook["image_size"]  # 512
    img_resized = cv2.resize(image_array, (target_size, target_size))

    # -- Noise extraction and correlation with reference --
    noise = _extract_noise_fused(img_resized)
    ref_noise = codebook["reference_noise"]
    correlation = _safe_corrcoef(noise, ref_noise)

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
        carrier_scores.append(_wrapped_phase_score(actual_phase, expected_phase))
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
        corr = _safe_corrcoef(noise_scaled, ref_scaled)
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
        profile_name=str(codebook.get("_profile_name", codebook.get("source", "nb1"))),
        detection_mode="nb1-correlation",
        details={
            "corr_score": round(corr_score, 4),
            "phase_score": round(phase_score, 4),
            "phase_excess": round(phase_excess, 4),
            "scale_correlations": scale_scores,
        },
    )


def _detect_nb2(image_array: np.ndarray, codebook: dict) -> SpectralResult:
    target_size = int(codebook["image_size"])
    img_resized = cv2.resize(image_array, (target_size, target_size))
    ref_noise = codebook["reference_noise"]
    noise = _extract_noise_fused(img_resized)
    correlation = _safe_corrcoef(noise, ref_noise)
    tile_size = 8
    shift_period = tile_size
    phase_match, phase_shift, carrier_strength = _nb2_shift_phase_score(
        img_resized, codebook, shift_period
    )
    tile_score, tile_shift, tile_channel_scores = _nb2_tile_score(
        img_resized, codebook, tile_size, shift_period
    )

    noise_gray = np.mean(noise, axis=2) if len(noise.shape) == 3 else noise
    structure_ratio = float(np.std(noise_gray) / (np.mean(np.abs(noise_gray)) + 1e-10))

    corr_score = max(0.0, min(1.0, correlation / 0.02))
    phase_score = max(0.0, min(1.0, (phase_match - 0.58) / 0.22))
    tile_score_norm = max(0.0, min(1.0, (tile_score - 0.08) / 0.35))
    carrier_score = max(0.0, min(1.0, (carrier_strength - 1.05) / 0.55))
    confidence = min(
        1.0,
        0.15 * corr_score
        + 0.35 * phase_score
        + 0.35 * tile_score_norm
        + 0.15 * carrier_score,
    )
    is_watermarked = confidence >= 0.50

    return SpectralResult(
        is_watermarked=bool(is_watermarked),
        confidence=float(confidence),
        correlation=float(correlation),
        phase_match=float(phase_match),
        structure_ratio=structure_ratio,
        carrier_strength=float(carrier_strength),
        multi_scale_consistency=0.0,
        profile_name=str(codebook.get("_profile_name", codebook.get("source", "nb2"))),
        detection_mode="nb2-shifted-lattice",
        details={
            "corr_score": round(corr_score, 4),
            "phase_score": round(phase_score, 4),
            "tile_score": round(tile_score, 4),
            "tile_score_norm": round(tile_score_norm, 4),
            "carrier_score": round(carrier_score, 4),
            "best_phase_shift": phase_shift,
            "best_tile_shift": tile_shift,
            "tile_channel_scores": [round(float(score), 4) for score in tile_channel_scores],
            "tile_size": tile_size,
        },
    )


def _detect(image_array: np.ndarray, codebook: dict) -> SpectralResult:
    if codebook.get("_mode") == "nb2":
        return _detect_nb2(image_array, codebook)
    return _detect_nb1(image_array, codebook)


# ---------------------------------------------------------------------------
# Analyzer entry point
# ---------------------------------------------------------------------------

def run_synthid_spectral(context: AnalysisContext) -> dict[str, object]:
    """Analyzer plugin: spectral SynthID watermark detection."""
    codebooks = _load_codebooks()
    if not codebooks:
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

    results = [_detect(image, codebook) for codebook in codebooks]
    result = max(results, key=lambda item: item.confidence)

    if result.is_watermarked:
        status = "DETECTED"
        summary = (
            f"SynthID watermark detected (confidence {result.confidence:.0%}, profile {result.profile_name})."
        )
    elif result.confidence > 0.20:
        status = "UNCERTAIN"
        summary = (
            f"Possible SynthID signal (confidence {result.confidence:.0%}, profile {result.profile_name})."
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
            "profile_name": result.profile_name,
            "detection_mode": result.detection_mode,
            "details": result.details,
        },
    }
