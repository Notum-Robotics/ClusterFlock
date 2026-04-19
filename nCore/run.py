#!/usr/bin/env python3
"""ClusterFlock nCore Orchestrator — entry point."""

import argparse
import fcntl
import logging
import sys
import os

log = logging.getLogger(__name__)

_LOCK_FD = None

def _acquire_singleton(port: int):
    """Ensure only one nCore instance runs per port via an exclusive file lock."""
    global _LOCK_FD
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f".ncore-{port}.lock")
    _LOCK_FD = open(lock_path, "w")
    try:
        fcntl.flock(_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FD.write(str(os.getpid()))
        _LOCK_FD.flush()
    except OSError:
        log.error(f"[nCore] FATAL: another instance is already running on port {port}")
        sys.exit(1)

def main():
    import cflog
    cflog.setup("ncore")

    p = argparse.ArgumentParser(prog="ncore", description="ClusterFlock nCore Orchestrator")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=1903, help="Listen port (default: 1903)")
    args = p.parse_args()

    _acquire_singleton(args.port)

    from server import serve
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
