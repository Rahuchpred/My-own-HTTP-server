"""Bounded worker pool for accepted client sockets."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

ClientAddress = tuple[str, int]
ClientJob = tuple[object, ClientAddress]
ClientHandler = Callable[[object, ClientAddress], None]


class ThreadPool:
    """Simple fixed-size thread pool with bounded queue."""

    def __init__(self, worker_count: int, queue_size: int, handler: ClientHandler) -> None:
        if worker_count <= 0:
            raise ValueError("worker_count must be positive")
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")

        self._handler = handler
        self._queue: queue.Queue[ClientJob | None] = queue.Queue(maxsize=queue_size)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._worker_count = worker_count

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def threads(self) -> tuple[threading.Thread, ...]:
        return tuple(self._threads)

    def start(self) -> None:
        for index in range(self._worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"http-worker-{index}",
                daemon=True,
            )
            self._threads.append(worker)
            worker.start()

    def submit(self, client_socket: object, address: ClientAddress) -> bool:
        try:
            self._queue.put_nowait((client_socket, address))
        except queue.Full:
            return False
        return True

    def shutdown(self) -> None:
        self._stop_event.set()
        sentinels_sent = 0
        while sentinels_sent < len(self._threads):
            try:
                self._queue.put_nowait(None)
                sentinels_sent += 1
            except queue.Full:
                try:
                    item = self._queue.get_nowait()
                    if item is not None:
                        client_socket, _address = item
                        close = getattr(client_socket, "close", None)
                        if callable(close):
                            close()
                    self._queue.task_done()
                except queue.Empty:
                    continue

        for thread in self._threads:
            thread.join(timeout=1.0)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is None:
                    return
                client_socket, address = item
                self._handler(client_socket, address)
            finally:
                self._queue.task_done()
