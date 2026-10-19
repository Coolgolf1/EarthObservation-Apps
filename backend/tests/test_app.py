"""Smoke tests for the pages and health route available without credentials."""

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "path,title",
    [
        ("/", "EarthAnalyser"),
        ("/search/", "EarthSearch"),
        ("/mapper/", "EarthMapper"),
    ],
)
def test_frontend_pages(path: str, title: str):
    response = TestClient(app).get(path)
    assert response.status_code == 200
    assert f"<title>{title}</title>" in response.text
    assert 'aria-label="Explore apps"' in response.text


def test_health_without_external_services():
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
