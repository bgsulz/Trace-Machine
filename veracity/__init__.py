from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
import os


csrf = CSRFProtect()
db = SQLAlchemy()


def create_app():
    app = Flask(__name__, instance_relative_config=True)

    default_db_uri = os.environ.get("DATABASE_URL", "sqlite:///veracity.sqlite")
    app.config.from_mapping(
        SECRET_KEY="dev",  # override in production via env or instance config
        SQLALCHEMY_DATABASE_URI=default_db_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        ALLOWED_EXTENSIONS={"png", "jpg", "jpeg", "webp", "gif"},
    )

    csrf.init_app(app)
    db.init_app(app)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass
    with app.app_context():
        from . import models  # noqa: F401  # register models

        db.create_all()

    from .routes import bp as main_bp

    app.register_blueprint(main_bp)

    return app
