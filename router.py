"""Routing table for method/path handlers."""

from collections.abc import Callable

from request import HTTPRequest
from response import HTTPResponse

Handler = Callable[[HTTPRequest], HTTPResponse]


class Router:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}

    def add_route(self, method: str, path: str, handler: Handler) -> None:
        normalized_method = method.upper().strip()
        if not normalized_method:
            raise ValueError("method cannot be empty")
        if not path.startswith("/"):
            raise ValueError("path must start with '/'")
        self._routes[(normalized_method, path)] = handler

    def resolve(self, method: str, path: str) -> Handler | None:
        normalized_method = method.upper().strip()
        return self._routes.get((normalized_method, path))
