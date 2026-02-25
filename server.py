"""Main HTTP server entry point and connection lifecycle orchestration."""

from router import Router


class HTTPServer:
    def __init__(self, router: Router | None = None) -> None:
        self.router = router or Router()

    def start(self) -> None:
        raise NotImplementedError("Implemented in phases P01-P10")

    def stop(self) -> None:
        raise NotImplementedError("Implemented in phases P08-P10")

    def _handle_client(self) -> None:
        raise NotImplementedError("Implemented in phases P01-P10")
