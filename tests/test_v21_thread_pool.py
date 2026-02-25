"""V21 tests for bounded thread pool and queue behavior."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from server import HTTPServer
from thread_pool import ThreadPool


def test_thread_pool_starts_fixed_worker_count() -> None:
    pool = ThreadPool(worker_count=3, queue_size=4, handler=lambda _sock, _addr: None)
    pool.start()

    try:
        assert pool.worker_count == 3
        assert len(pool.threads) == 3
        assert all(thread.is_alive() for thread in pool.threads)
    finally:
        pool.shutdown()


def test_thread_pool_submit_returns_false_when_full() -> None:
    pool = ThreadPool(worker_count=1, queue_size=1, handler=lambda _sock, _addr: None)

    assert pool.submit(object(), ("127.0.0.1", 0)) is True
    assert pool.submit(object(), ("127.0.0.1", 1)) is False


class SlowHTTPServer(HTTPServer):
    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        time.sleep(0.3)
        super()._handle_client(client_socket, address)


def _start_server() -> tuple[HTTPServer, threading.Thread]:
    server = SlowHTTPServer(port=0, worker_count=1, request_queue_size=1)
    thread = threading.Thread(target=server.start, daemon=True)
    thread.start()

    deadline = time.time() + 2
    while server.port == 0 and time.time() < deadline:
        time.sleep(0.01)

    if server.port == 0:
        raise RuntimeError("Server did not bind to a port")
    return server, thread


def _stop_server(server: HTTPServer, thread: threading.Thread) -> None:
    server.stop()
    thread.join(timeout=2)


def _send_request(host: str, port: int) -> bytes:
    payload = b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
    with socket.create_connection((host, port), timeout=2) as sock:
        sock.sendall(payload)
        return sock.recv(4096)


def test_server_returns_503_when_queue_is_saturated() -> None:
    server, thread = _start_server()
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(_send_request, server.host, server.port)
                for _ in range(3)
            ]
            responses = [future.result() for future in futures]
    finally:
        _stop_server(server, thread)

    assert any(response.startswith(b"HTTP/1.1 503 Service Unavailable") for response in responses)
