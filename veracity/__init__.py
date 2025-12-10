from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_migrate import Migrate
from werkzeug.middleware.proxy_fix import ProxyFix
from dotenv import load_dotenv
import os

load_dotenv()
csrf = CSRFProtect()
db = SQLAlchemy()
migrate = Migrate()


def create_app(test_config=None):
    instance_path_override = None
    if test_config is not None:
        instance_path_override = test_config["INSTANCE_PATH"]

    app = Flask(
        __name__,
        instance_relative_config=True,
        instance_path=instance_path_override,
    )
    migrate.init_app(app, db)

    # Trust a single upstream proxy (e.g. Nginx) for X-Forwarded-* headers.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)  # type: ignore[assignment]

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "CRITICAL ERROR: DATABASE_URL is not set. "
            "If you are running locally, ensure you have a .env file. "
            "If you are in production, ensure the environment variable is set."
        )

    secret_key = os.environ.get("SECRET_KEY", "dev")
    app.config.from_mapping(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        ALLOWED_EXTENSIONS={"png", "jpg", "jpeg", "webp", "gif"},
    )

    if test_config is not None:
        app.config.update(test_config)

    csrf.init_app(app)
    db.init_app(app)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    from .routes import bp as main_bp

    app.register_blueprint(main_bp)

    return app
