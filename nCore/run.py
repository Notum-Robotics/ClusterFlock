#!/usr/bin/env python3
"""ClusterFlock nCore Orchestrator — entry point."""

import argparse

def main():
    p = argparse.ArgumentParser(prog="ncore", description="ClusterFlock nCore Orchestrator")
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=1903, help="Listen port (default: 1903)")
    args = p.parse_args()

    from server import serve
    serve(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
