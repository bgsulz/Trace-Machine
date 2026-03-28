import io

import numpy as np
from PIL import Image

from veracity.analyzers.context import AnalysisContext
from veracity.analyzers.synthid_spectral import (
    SpectralResult,
    _compute_codebook_tile_templates,
    _nb2_shift_phase_score,
    _nb2_tile_score,
    run_synthid_spectral,
)


def _pattern_image_bytes(image: np.ndarray) -> bytes:
    image_u8 = np.clip(image, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(image_u8, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


def _make_nb2_like_codebook() -> dict:
    tile = np.array(
        [
            [0.9, -0.2, 0.4, -0.8, 0.7, -0.1, 0.2, -0.5],
            [-0.6, 0.3, -0.9, 0.1, -0.4, 0.8, -0.2, 0.5],
            [0.2, -0.7, 0.6, -0.3, 0.1, -0.8, 0.9, -0.4],
            [-0.1, 0.5, -0.4, 0.7, -0.9, 0.2, -0.6, 0.3],
            [0.8, -0.5, 0.2, -0.6, 0.4, -0.7, 0.1, -0.3],
            [-0.3, 0.7, -0.1, 0.5, -0.2, 0.6, -0.8, 0.4],
            [0.4, -0.9, 0.7, -0.2, 0.3, -0.5, 0.6, -0.1],
            [-0.8, 0.1, -0.5, 0.9, -0.6, 0.4, -0.3, 0.2],
        ],
        dtype=np.float32,
    )
    tiled = np.tile(tile, (64, 64))
    reference_noise = np.stack([tiled, tiled * 0.9, tiled * 1.1], axis=2).astype(np.float32)
    canonical = _make_shifted_cosine_image(shift_y=0, shift_x=0) - 127.0
    gray = canonical.mean(axis=2)
    f = np.fft.fftshift(np.fft.fft2(gray))
    reference_phase = np.angle(f).astype(np.float32)
    return {
        "image_size": 512,
        "reference_noise": reference_noise,
        "reference_phase": reference_phase,
        "carriers": [
            {"frequency": (64, 64), "phase": float(reference_phase[320, 320]), "coherence": 0.995},
            {"frequency": (-64, -64), "phase": float(reference_phase[192, 192]), "coherence": 0.995},
            {"frequency": (64, -64), "phase": float(reference_phase[320, 192]), "coherence": 0.99},
            {"frequency": (-64, 64), "phase": float(reference_phase[192, 320]), "coherence": 0.99},
        ],
        "_profile_name": "nb2-blackonly",
        "_mode": "nb2",
    }


def _make_shifted_cosine_image(shift_y: int, shift_x: int, phase: float = 0.7) -> np.ndarray:
    coords = np.arange(512, dtype=np.float32)
    yy, xx = np.meshgrid(coords, coords, indexing="ij")
    base_diag = np.cos(
        2 * np.pi * (64 * (yy - shift_y) + 64 * (xx - shift_x)) / 512.0 + phase
    )
    base_cross = np.cos(
        2 * np.pi * (64 * (yy - shift_y) - 64 * (xx - shift_x)) / 512.0 - 0.35
    )
    signal = base_diag + 0.7 * base_cross
    channel_r = 127.0 + 32.0 * signal
    channel_g = 127.0 + 28.0 * signal
    channel_b = 127.0 + 24.0 * signal
    return np.stack([channel_r, channel_g, channel_b], axis=2)


def test_nb2_tile_score_recovers_modulo_shift():
    codebook = _make_nb2_like_codebook()
    template = _compute_codebook_tile_templates(codebook, tile_size=8)
    shifted = np.roll(codebook["reference_noise"], shift=(3, 5), axis=(0, 1))
    image = 127.0 + 50.0 * shifted

    score, shift, per_channel = _nb2_tile_score(
        image,
        codebook,
        tile_size=8,
        shift_period=8,
    )

    assert score > 0.95
    assert shift == (3, 5)
    assert min(per_channel) > 0.9
    assert template.shape == (3, 8, 8)


def test_nb2_phase_search_prefers_aligned_shift():
    codebook = _make_nb2_like_codebook()
    image = _make_shifted_cosine_image(shift_y=2, shift_x=6)

    phase_score, best_shift, carrier_strength = _nb2_shift_phase_score(
        image,
        codebook,
        shift_period=8,
    )

    assert phase_score > 0.9
    assert best_shift == (2, 6)
    assert carrier_strength > 1.0


def test_run_synthid_spectral_uses_highest_confidence_profile(monkeypatch):
    image = np.full((32, 32, 3), 127, dtype=np.uint8)
    context = AnalysisContext(
        image_bytes=_pattern_image_bytes(image),
        phash="phash",
        whash="whash",
        registry_id=1,
        neighbors=[],
        width=32,
        height=32,
    )

    codebooks = [
        {"_profile_name": "nb1", "_mode": "nb1"},
        {"_profile_name": "nb2-blackonly", "_mode": "nb2"},
    ]

    def fake_detect(_image, codebook):
        if codebook["_profile_name"] == "nb2-blackonly":
            return SpectralResult(
                is_watermarked=True,
                confidence=0.77,
                correlation=0.01,
                phase_match=0.9,
                structure_ratio=1.0,
                carrier_strength=1.5,
                multi_scale_consistency=0.0,
                profile_name="nb2-blackonly",
                detection_mode="nb2-shifted-lattice",
                details={"best_phase_shift": (1, 2)},
            )
        return SpectralResult(
            is_watermarked=False,
            confidence=0.12,
            correlation=0.0,
            phase_match=0.5,
            structure_ratio=1.0,
            carrier_strength=1.0,
            multi_scale_consistency=0.0,
            profile_name="nb1",
            detection_mode="nb1-correlation",
            details={},
        )

    monkeypatch.setattr("veracity.analyzers.synthid_spectral._load_codebooks", lambda: codebooks)
    monkeypatch.setattr("veracity.analyzers.synthid_spectral._detect", fake_detect)

    result = run_synthid_spectral(context)

    assert result["status"] == "DETECTED"
    assert result["data"]["profile_name"] == "nb2-blackonly"
    assert result["data"]["detection_mode"] == "nb2-shifted-lattice"
    assert "nb2-blackonly" in result["summary"]
