"""Example route handlers."""

from request import HTTPRequest
from response import HTTPResponse


def home(request: HTTPRequest) -> HTTPResponse:
    raise NotImplementedError("Implemented in phase P04")


def submit(request: HTTPRequest) -> HTTPResponse:
    raise NotImplementedError("Implemented in phase P07")


def serve_static(request: HTTPRequest) -> HTTPResponse:
    raise NotImplementedError("Implemented in phase P06")
