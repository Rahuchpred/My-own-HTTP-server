"""Utility helpers shared across server modules."""

import mimetypes
from pathlib import Path
from urllib.parse import unquote

from config import STATIC_DIR


def get_content_type(file_path: Path) -> str:
    content_type, _encoding = mimetypes.guess_type(file_path.name)
    return content_type or "application/octet-stream"



def resolve_static_file(request_path: str, static_dir: str = STATIC_DIR) -> Path | None:
    """Resolve a safe static file path or return None for traversal attempts."""
    if not request_path.startswith("/static/"):
        return None

    raw_relative_path = request_path.removeprefix("/static/")
    decoded_relative_path = unquote(raw_relative_path)

    static_root = Path(static_dir).resolve()
    candidate = (static_root / decoded_relative_path).resolve()

    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None

    return candidate
