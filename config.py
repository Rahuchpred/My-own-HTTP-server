"""Configuration constants for the custom HTTP server."""

HOST: str = "127.0.0.1"
PORT: int = 8080
BUFFER_SIZE: int = 1024
STATIC_DIR: str = "static"
SOCKET_TIMEOUT_SECS: int = 5
MAX_REQUEST_BYTES: int = 1_048_576
MAX_HEADER_BYTES: int = 16_384
MAX_BODY_BYTES: int = 524_288
DEBUG: bool = True
