"""Example route handlers."""

from email.utils import formatdate, parsedate_to_datetime

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


def stream_demo(request: HTTPRequest) -> HTTPResponse:
    _ = request
    chunks = [b"chunk-one\n", b"chunk-two\n", b"chunk-three\n"]
    return HTTPResponse(
        status_code=200,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        stream=chunks,
    )


def serve_static(request: HTTPRequest) -> HTTPResponse:
    static_path = resolve_static_file(request.path)
    if static_path is None:
        return HTTPResponse(status_code=403, body="Forbidden")

    if not static_path.exists() or not static_path.is_file():
        return HTTPResponse(status_code=404, body="Not Found")

    file_stat = static_path.stat()
    etag = f'W/"{file_stat.st_mtime_ns:x}-{file_stat.st_size:x}"'
    last_modified = formatdate(file_stat.st_mtime, usegmt=True)

    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None and if_none_match.strip() == etag:
        return HTTPResponse(
            status_code=304,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
            },
            body=b"",
        )

    if_modified_since = request.headers.get("if-modified-since")
    if if_modified_since is not None:
        try:
            since_dt = parsedate_to_datetime(if_modified_since)
            since_ts = since_dt.timestamp()
            if int(file_stat.st_mtime) <= int(since_ts):
                return HTTPResponse(
                    status_code=304,
                    headers={
                        "ETag": etag,
                        "Last-Modified": last_modified,
                    },
                    body=b"",
                )
        except (TypeError, ValueError, OverflowError):
            pass

    return HTTPResponse(
        status_code=200,
        headers={
            "Content-Type": get_content_type(static_path),
            "ETag": etag,
            "Last-Modified": last_modified,
        },
        body=static_path.read_bytes(),
    )
