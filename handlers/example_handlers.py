"""Example route handlers."""

from request import HTTPRequest
from response import HTTPResponse


def home(request: HTTPRequest) -> HTTPResponse:
    _ = request
    return HTTPResponse(
        status_code=200,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body="Hello World",
    )



def submit(request: HTTPRequest) -> HTTPResponse:
    raise NotImplementedError("Implemented in phase P07")



def serve_static(request: HTTPRequest) -> HTTPResponse:
    raise NotImplementedError("Implemented in phase P06")
