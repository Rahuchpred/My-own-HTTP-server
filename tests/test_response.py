"""Unit tests for HTTP response serialization."""

from response import HTTPResponse


def test_response_serialization_sets_length_and_default_content_type() -> None:
    response = HTTPResponse(status_code=200, body="hello")

    raw = response.to_bytes()

    assert raw.startswith(b"HTTP/1.1 200 OK\r\n")
    assert b"Content-Type: text/plain; charset=utf-8\r\n" in raw
    assert b"Content-Length: 5\r\n" in raw
    assert raw.endswith(b"\r\n\r\nhello")


def test_response_serialization_preserves_custom_content_type() -> None:
    response = HTTPResponse(
        status_code=404,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=b"<h1>Not Found</h1>",
    )

    raw = response.to_bytes()

    assert raw.startswith(b"HTTP/1.1 404 Not Found\r\n")
    assert b"Content-Type: text/html; charset=utf-8\r\n" in raw
    assert b"Content-Length: 18\r\n" in raw
