from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_compress import Compress
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import os
import logging

load_dotenv()

try:  # HEIC/HEIF support (e.g. iPhone photos) for every Pillow call in the app
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional at import time
    pass
else:
    register_heif_opener()
csrf = CSRFProtect()
db = SQLAlchemy()
migrate = Migrate()
compress = Compress()
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("LIMITER_STORAGE_URL", "memory://"),
)

def create_app(test_config=None):
    instance_path_override = None
    if test_config is not None:
        instance_path_override = test_config["INSTANCE_PATH"]

    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=instance_path_override,
    )
    app.logger.setLevel(logging.INFO)
    logging.getLogger("veracity").setLevel(logging.INFO)

    migrate.init_app(app, db)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "CRITICAL ERROR: DATABASE_URL is not set. "
            "If you are running locally, ensure you have a .env file. "
            "If you are in production, ensure the environment variable is set."
        )

    secret_key = os.environ.get("SECRET_KEY", "dev")
    kofi_token = os.environ.get("KOFI_TOKEN", "")
    proxy_fix_enabled = os.environ.get("PROXY_FIX_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    proxy_fix_x_for = int(os.environ.get("PROXY_FIX_X_FOR", "1"))
    proxy_fix_x_proto = int(os.environ.get("PROXY_FIX_X_PROTO", "1"))
    # Let outbound fetches reach private/LAN addresses. Local development only:
    # in production this reopens server-side request forgery.
    safe_fetch_allow_private = os.environ.get("SAFE_FETCH_ALLOW_PRIVATE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # Trust X-Forwarded-Host only when the proxy sets it (off by default).
    proxy_fix_x_host = int(os.environ.get("PROXY_FIX_X_HOST", "0"))
    local_matching_enabled = os.environ.get("LOCAL_MATCHING_ENABLED", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    try:
        local_match_max_candidates = int(
            os.environ.get("LOCAL_MATCH_MAX_CANDIDATES", "200")
        )
    except ValueError:
        local_match_max_candidates = 200
    invisible_watermark_decoders = _parse_invisible_watermark_decoders(
        os.environ.get("INVISIBLE_WATERMARK_DECODERS", ""),
        logger=app.logger,
    )
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        ALLOWED_EXTENSIONS={"png", "jpg", "jpeg", "webp", "gif"},
        KOFI_TOKEN=kofi_token,
        RATELIMIT_ENABLED=secret_key != "dev",
        PROXY_FIX_ENABLED=proxy_fix_enabled,
        PROXY_FIX_X_FOR=proxy_fix_x_for,
        PROXY_FIX_X_PROTO=proxy_fix_x_proto,
        PROXY_FIX_X_HOST=proxy_fix_x_host,
        SAFE_FETCH_ALLOW_PRIVATE=safe_fetch_allow_private,
        TRUSTMARK_AUTO_DOWNLOAD=os.environ.get("TRUSTMARK_AUTO_DOWNLOAD", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        TRUSTMARK_THREADS=_positive_int(os.environ.get("TRUSTMARK_THREADS"), default=2, logger=app.logger),
        LOCAL_MATCHING_ENABLED=local_matching_enabled,
        LOCAL_MATCH_MAX_CANDIDATES=local_match_max_candidates,
        INVISIBLE_WATERMARK_DECODERS=invisible_watermark_decoders,
    )

    # Trust upstream proxy headers only when explicitly enabled.
    if app.config.get("PROXY_FIX_ENABLED"):
        app.wsgi_app = ProxyFix(  # type: ignore[assignment]
            app.wsgi_app,
            x_for=int(app.config.get("PROXY_FIX_X_FOR") or 1),
            x_proto=int(app.config.get("PROXY_FIX_X_PROTO") or 1),
            x_host=int(app.config.get("PROXY_FIX_X_HOST") or 0),
        )

    if test_config is not None:
        app.config.update(test_config)

    csrf.init_app(app)
    db.init_app(app)
    limiter.init_app(app)
    compress.init_app(app)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    from .routes import bp as main_bp

    app.register_blueprint(main_bp)

    @app.cli.command("download-models")
    def download_models():
        """Fetch watermark decoder models now instead of on the first analysis."""
        from pathlib import Path

        from .watermarks.trustmark import VARIANTS, ModelUnavailable, ensure_model

        model_dir = Path(app.config.get("TRUSTMARK_MODEL_DIR") or os.path.join(app.instance_path, "models", "trustmark"))
        for variant in VARIANTS:
            try:
                path = ensure_model(model_dir, variant, download=True)
            except ModelUnavailable as exc:
                raise SystemExit(f"TrustMark {variant}: {exc}") from None
            print(f"TrustMark {variant}: {path}")

    @app.after_request
    def _security_headers(response):
        # Never let browsers reinterpret a served file (e.g. an upload) as HTML.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    return app


def _positive_int(raw: str | None, *, default: int, logger) -> int:
    """Parse a positive integer setting, falling back (with a warning) if invalid."""
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        logger.warning("Ignoring invalid setting %r; using %d", raw, default)
        return default
    return value


def _parse_invisible_watermark_decoders(raw_value: str, *, logger) -> set[str]:
    aliases = {
        "imwatermark": "open_dwt_dct",
        "open_dwt_dct": "open_dwt_dct",
        "dwt_dct": "open_dwt_dct",
        "trustmark": "adobe_trustmark",
        "adobe_trustmark": "adobe_trustmark",
    }
    enabled: set[str] = set()
    raw_items = [
        item.strip().lower()
        for item in (raw_value or "").replace(";", ",").split(",")
        if item.strip()
    ]
    if not raw_items:
        # Default: TrustMark runs whenever its lightweight runtime is installed.
        import importlib.util

        if importlib.util.find_spec("onnxruntime") is not None:
            enabled.add("adobe_trustmark")
        return enabled
    if raw_items == ["none"]:
        return enabled
    if "all" in raw_items:
        return {"open_dwt_dct", "adobe_trustmark"}

    for item in raw_items:
        normalized = aliases.get(item)
        if normalized is None:
            logger.warning(
                "Ignoring unknown INVISIBLE_WATERMARK_DECODERS entry %r",
                item,
            )
            continue
        enabled.add(normalized)
    return enabled
