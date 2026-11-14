"""Run the Flask mock repository on an ephemeral loopback port."""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Iterator
from urllib.parse import urlsplit

from flask import Flask
from werkzeug.serving import make_server

from .config import MockConfig
from .mock_repository import create_app


@dataclass
class RunningMock:
    server: object
    thread: threading.Thread
    config: MockConfig
    endpoint: str

    def __enter__(self) -> RunningMock:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop()

    def stop(self) -> None:
        self.server.shutdown()  # type: ignore[attr-defined]
        self.thread.join(timeout=5)


def serve_mock(config: MockConfig) -> RunningMock:
    path = urlsplit(config.repository.base_url).path or "/oai"
    server = make_server("127.0.0.1", 0, Flask(__name__), threaded=True)
    port = int(server.server_port)
    endpoint = f"http://127.0.0.1:{port}{path}"
    effective_config = replace(
        config,
        host="127.0.0.1",
        port=port,
        repository=replace(config.repository, base_url=endpoint),
    )
    server.app = create_app(effective_config)
    thread = threading.Thread(target=server.serve_forever, name="mock-oai", daemon=True)
    thread.start()
    _wait_for_port(port)
    return RunningMock(
        server=server, thread=thread, config=effective_config, endpoint=endpoint
    )


def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            threading.Event().wait(0.01)
    raise RuntimeError("mock OAI-PMH 服务未能启动")


@contextmanager
def running_mock(config: MockConfig) -> Iterator[RunningMock]:
    instance = serve_mock(config)
    try:
        yield instance
    finally:
        instance.stop()
