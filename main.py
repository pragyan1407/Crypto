"""Entry point: python main.py [--port 18018] [--data-dir data] [--advertise host:port]"""

import argparse
import asyncio
import logging
import os
import signal
import sys

from node import DEFAULT_BOOTSTRAP, DEFAULT_PORT, Node
from protocol import is_valid_peer_address


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Drachma - a Kerma node")
    parser.add_argument("--host", default="0.0.0.0", help="interface to listen on")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("KERMA_PORT", DEFAULT_PORT)), help="TCP port to listen on"
    )
    parser.add_argument(
        "--data-dir", default=os.environ.get("KERMA_DATA_DIR", "data"), help="where the peer list is stored"
    )
    parser.add_argument(
        "--advertise",
        default=os.environ.get("KERMA_ADVERTISE") or None,
        help="our public host:port, put first in every peers message",
    )
    parser.add_argument(
        "--bootstrap",
        action="append",
        help="bootstrap peer host:port (repeatable, replaces the default)",
    )
    parser.add_argument("--no-bootstrap", action="store_true", help="do not use any bootstrap peer")
    parser.add_argument("--log-level", default=os.environ.get("KERMA_LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


async def amain(args):
    if args.no_bootstrap:
        bootstrap = ()
    else:
        bootstrap = tuple(args.bootstrap) if args.bootstrap else (DEFAULT_BOOTSTRAP,)

    node = Node(
        data_dir=args.data_dir,
        host=args.host,
        port=args.port,
        advertise=args.advertise,
        bootstrap=bootstrap,
    )
    await node.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    await stop.wait()
    await node.stop()


def main():
    args = parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.advertise and not is_valid_peer_address(args.advertise):
        sys.exit(f"--advertise {args.advertise!r} is not a valid public host:port")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
