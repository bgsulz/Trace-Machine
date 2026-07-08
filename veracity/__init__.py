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
    tineye_persistence_mode = os.environ.get("TINEYE_PERSISTENCE_MODE", "none").strip().lower()
    if tineye_persistence_mode not in {"none", "derived"}:
        app.logger.warning(
            "Invalid TINEYE_PERSISTENCE_MODE=%r; defaulting to 'none'",
            tineye_persistence_mode,
        )
        tineye_persistence_mode = "none"
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
        LOCAL_MATCHING_ENABLED=local_matching_enabled,
        LOCAL_MATCH_MAX_CANDIDATES=local_match_max_candidates,
        TINEYE_PERSISTENCE_MODE=tineye_persistence_mode,
        INVISIBLE_WATERMARK_DECODERS=invisible_watermark_decoders,
    )

    # Trust upstream proxy headers only when explicitly enabled.
    if app.config.get("PROXY_FIX_ENABLED"):
        app.wsgi_app = ProxyFix(  # type: ignore[assignment]
            app.wsgi_app,
            x_for=int(app.config.get("PROXY_FIX_X_FOR") or 1),
            x_proto=int(app.config.get("PROXY_FIX_X_PROTO") or 1),
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

    return app


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
