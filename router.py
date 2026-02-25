"""Routing table for method/path handlers."""

from collections.abc import Callable

from request import HTTPRequest
from response import HTTPResponse

Handler = Callable[[HTTPRequest], HTTPResponse]


class Router:
    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}

    def add_route(self, method: str, path: str, handler: Handler) -> None:
        raise NotImplementedError("Implemented in phase P05")

    def resolve(self, method: str, path: str) -> Handler | None:
        raise NotImplementedError("Implemented in phase P05")
