"""Bounded worker pool for accepted client sockets."""

from __future__ import annotations

import queue
import threading
import time
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
        self._active_jobs = 0
        self._active_lock = threading.Lock()
        self._drain_condition = threading.Condition(self._active_lock)
        self._shutdown_lock = threading.Lock()
        self._shutdown_started = False

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
        if self._stop_event.is_set():
            return False
        try:
            self._queue.put_nowait((client_socket, address))
        except queue.Full:
            return False
        return True

    def wait_for_drain(self, timeout: float | None = None) -> bool:
        with self._drain_condition:
            if timeout is None:
                while not self._is_drained_locked():
                    self._drain_condition.wait(timeout=0.1)
                return True

            deadline = time.monotonic() + timeout
            while not self._is_drained_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._drain_condition.wait(timeout=min(remaining, 0.1))
            return True

    def shutdown(self, *, graceful: bool = False, timeout: float | None = None) -> None:
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True

        if graceful:
            self.wait_for_drain(timeout=timeout)

        self._stop_event.set()
        for _ in self._threads:
            self._queue.put(None)

        for thread in self._threads:
            thread.join(timeout=1.0)

    def _is_drained_locked(self) -> bool:
        return self._active_jobs == 0 and self._queue.empty()

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
                with self._drain_condition:
                    self._active_jobs += 1
                self._handler(client_socket, address)
            finally:
                if item is not None:
                    with self._drain_condition:
                        self._active_jobs = max(0, self._active_jobs - 1)
                        self._drain_condition.notify_all()
                self._queue.task_done()
