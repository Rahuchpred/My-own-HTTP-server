"""Example route handlers."""

from request import HTTPRequest
from response import HTTPResponse
from utils import get_content_type, resolve_static_file


def home(request: HTTPRequest) -> HTTPResponse:
    _ = request
    return HTTPResponse(
        status_code=200,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body="Hello World",
    )



def submit(request: HTTPRequest) -> HTTPResponse:
    body_text = request.body.decode("utf-8", errors="replace")
    return HTTPResponse(
        status_code=200,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body=f"Received POST body: {body_text}",
    )



def serve_static(request: HTTPRequest) -> HTTPResponse:
    static_path = resolve_static_file(request.path)
    if static_path is None:
        return HTTPResponse(status_code=403, body="Forbidden")

    if not static_path.exists() or not static_path.is_file():
        return HTTPResponse(status_code=404, body="Not Found")

    return HTTPResponse(
        status_code=200,
        headers={"Content-Type": get_content_type(static_path)},
        body=static_path.read_bytes(),
    )
