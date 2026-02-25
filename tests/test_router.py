"""Unit tests for method/path router behavior."""

from request import HTTPRequest
from response import HTTPResponse
from router import Router


def _handler_ok(_request: HTTPRequest) -> HTTPResponse:
    return HTTPResponse(status_code=200, body="ok")



def test_router_resolves_exact_method_and_path() -> None:
    router = Router()
    router.add_route("get", "/", _handler_ok)

    resolved = router.resolve("GET", "/")

    assert resolved is _handler_ok



def test_router_returns_none_for_unknown_path() -> None:
    router = Router()
    router.add_route("GET", "/", _handler_ok)

    resolved = router.resolve("GET", "/missing")

    assert resolved is None



def test_router_rejects_invalid_path() -> None:
    router = Router()

    try:
        router.add_route("GET", "missing-slash", _handler_ok)
    except ValueError as exc:
        assert "path must start" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid route path")
