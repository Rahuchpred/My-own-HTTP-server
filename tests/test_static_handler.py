"""Unit tests for static file serving behavior."""

from handlers.example_handlers import serve_static
from request import HTTPRequest


def _build_request(path: str) -> HTTPRequest:
    return HTTPRequest(
        method="GET",
        path=path,
        http_version="HTTP/1.1",
        headers={"host": "localhost"},
        body=b"",
        query_params={},
    )



def test_serve_existing_static_file() -> None:
    response = serve_static(_build_request("/static/test.html"))

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "text/html"
    assert b"Static file is working" in response.body



def test_missing_static_file_returns_404() -> None:
    response = serve_static(_build_request("/static/does-not-exist.css"))

    assert response.status_code == 404



def test_directory_traversal_attempt_returns_403() -> None:
    response = serve_static(_build_request("/static/../plan.md"))

    assert response.status_code == 403
