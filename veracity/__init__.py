from flask import Flask
from flask_wtf.csrf import CSRFProtect
import os


csrf = CSRFProtect()


def create_app():
    app = Flask(__name__, instance_relative_config=True)

    app.config.from_mapping(
        SECRET_KEY="dev",  # override in production via env or instance config
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
        ALLOWED_EXTENSIONS={"png", "jpg", "jpeg", "webp", "gif"},
    )

    csrf.init_app(app)

    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass
    from .routes import bp as main_bp

    app.register_blueprint(main_bp)

    return app
