import io
import os
import sys

import pytest
from PIL import Image

from veracity import create_app

# Ensure project root (where the veracity package lives) is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def app():
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,  # disable CSRF for most tests; covered separately
    )
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_test_image_bytes(fmt="PNG", size=(10, 10)) -> bytes:
    img = Image.new("RGB", size, color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()