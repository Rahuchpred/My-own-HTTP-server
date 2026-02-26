"""V33 tests for tuned worker and keep-alive defaults."""

from config import (
    KEEPALIVE_TIMEOUT_SECS,
    MAX_KEEPALIVE_REQUESTS,
    REQUEST_QUEUE_SIZE,
    WORKER_COUNT,
)
from server import HTTPServer


def test_tuned_runtime_defaults_are_applied() -> None:
    assert WORKER_COUNT == 16
    assert REQUEST_QUEUE_SIZE == 1024
    assert KEEPALIVE_TIMEOUT_SECS == 10
    assert MAX_KEEPALIVE_REQUESTS == 1000


def test_server_uses_configured_pool_defaults() -> None:
    server = HTTPServer()
    assert server.worker_count == WORKER_COUNT
    assert server.request_queue_size == REQUEST_QUEUE_SIZE
