"""Unit tests for HTTP request parsing."""

import pytest

from request import HTTPRequest


def test_parse_get_with_query_params() -> None:
    raw = (
        b"GET /search?q=hello&q=world&lang=en HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"User-Agent: pytest\r\n"
        b"\r\n"
    )

    request = HTTPRequest.from_bytes(raw)

    assert request.method == "GET"
    assert request.path == "/search"
    assert request.http_version == "HTTP/1.1"
    assert request.headers["host"] == "localhost"
    assert request.query_params == {"q": ["hello", "world"], "lang": ["en"]}
    assert request.body == b""


def test_parse_post_with_body() -> None:
    raw = (
        b"POST /submit HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: 9\r\n"
        b"\r\n"
        b"name=test"
    )

    request = HTTPRequest.from_bytes(raw)

    assert request.method == "POST"
    assert request.path == "/submit"
    assert request.headers["content-length"] == "9"
    assert request.body == b"name=test"


def test_parse_invalid_request_line_raises_value_error() -> None:
    raw = b"BROKEN-LINE\r\nHost: localhost\r\n\r\n"

    with pytest.raises(ValueError, match="Invalid request line"):
        HTTPRequest.from_bytes(raw)


def test_parse_invalid_content_length_raises_value_error() -> None:
    raw = (
        b"POST /submit HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: abc\r\n"
        b"\r\n"
        b"name=test"
    )

    with pytest.raises(ValueError, match="Invalid Content-Length"):
        HTTPRequest.from_bytes(raw)
