"""Combined TCP + WebSocket relay service for ai-wechat.

Run this entrypoint when CLI clients and browser GUI clients should chat with
each other live. Both gateways attach to the same `ChatRelayService`, so online
users, private messages, group broadcasts, status updates, and history all pass
through one runtime.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from server.database import DEFAULT_DB_PATH
from server.relay import ChatRelayService
from server.server import DEFAULT_HOST as DEFAULT_TCP_HOST
from server.server import DEFAULT_PORT as DEFAULT_TCP_PORT
from server.server import ChatServer
from server.web_server import WebChatServer


logger = logging.getLogger(__name__)
DEFAULT_WEB_HOST = "0.0.0.0"
DEFAULT_WEB_PORT = 8080


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the unified CLI + GUI chat relay service.")
    parser.add_argument("--tcp-host", default=DEFAULT_TCP_HOST, help="TCP host for CLI clients")
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="TCP port for CLI clients")
    parser.add_argument("--web-host", default=DEFAULT_WEB_HOST, help="HTTP/WebSocket host for browser clients")
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT, help="HTTP/WebSocket port")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite database path")
    parser.add_argument("--heartbeat-timeout", type=float, default=60.0)
    parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    parser.add_argument("--recv-timeout", type=float, default=30.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    relay = ChatRelayService(
        db_path=args.db,
        heartbeat_timeout=args.heartbeat_timeout,
        heartbeat_interval=args.heartbeat_interval,
    )
    tcp_server = ChatServer(
        host=args.tcp_host,
        port=args.tcp_port,
        recv_timeout=args.recv_timeout,
        relay=relay,
    )
    web_server = WebChatServer(
        host=args.web_host,
        port=args.web_port,
        relay=relay,
    )

    relay.start()
    tcp_port = tcp_server.start()
    web_port = web_server.start()
    logger.info("relay TCP gateway listening on %s:%d", args.tcp_host, tcp_port)
    logger.info("relay web gateway listening on http://%s:%d", args.web_host, web_port)

    stop_event = threading.Event()

    def _signal_handler(signum, _frame):  # noqa: ANN001 - signal callback
        logger.info("received signal %s, shutting down relay service", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)

    try:
        stop_event.wait()
    finally:
        tcp_server.stop()
        web_server.stop()
        relay.stop()


if __name__ == "__main__":
    main()
