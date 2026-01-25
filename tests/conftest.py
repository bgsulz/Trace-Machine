import io
import os
import sys

import pytest
from PIL import Image

# Ensure project root (where the veracity package lives) is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


from veracity import create_app, db  # noqa: E402


@pytest.fixture
def app(tmp_path_factory):
    instance_path = tmp_path_factory.mktemp("instance")
    yield _create_testing_app(instance_path=str(instance_path))

@pytest.fixture
def app_csrf(tmp_path_factory):
    instance_path = tmp_path_factory.mktemp("instance-csrf")
    yield _create_testing_app(enable_csrf=True, instance_path=str(instance_path))


@pytest.fixture
def app_ratelimited(tmp_path_factory):
    instance_path = tmp_path_factory.mktemp("instance-ratelimited")
    yield _create_testing_app(enable_ratelimit=True, instance_path=str(instance_path))


def _create_testing_app(*, enable_csrf=False, enable_ratelimit=False, instance_path):
    test_config = {
        "TESTING": True,
        "WTF_CSRF_ENABLED": enable_csrf,
        "RATELIMIT_ENABLED": enable_ratelimit,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "INSTANCE_PATH": instance_path,
        "KOFI_TOKEN": "test-token",
        "SERVER_NAME": "localhost.localdomain",
        "APPLICATION_ROOT": "/",
        "PREFERRED_URL_SCHEME": "http",
    }
    app = create_app(test_config)
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app

@pytest.fixture
def client(app):
    return app.test_client()


def _make_test_image_bytes(fmt="PNG", size=(10, 10)) -> bytes:
    img = Image.new("RGB", size, color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _make_entropy_image_bytes(fmt="PNG", size=(256, 256)) -> bytes:
    """Generate a larger, higher-entropy image for crop/entropy tests."""
    noise = Image.effect_noise(size, 64)
    img = noise.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()
