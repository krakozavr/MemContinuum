#!/usr/bin/env python
"""nomemory -- the floor. Returns nothing, ever.

This is the baseline every real system must beat: a coding agent with no
decision-memory tool at all. See bench/README.md "Runner interface" for the
CLI contract every runner in this directory implements.
"""
from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nomemory")
    parser.add_argument("--corpus", required=True, help="unused; accepted for interface parity")
    parser.add_argument("--kind", required=True, choices=["path", "question"])
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10, help="unused; accepted for interface parity")
    parser.add_argument("--mode", default=None, help="unused; accepted for interface parity")
    parser.parse_args(argv)
    # Print nothing. Empty stdout is a valid, meaningful answer here.
    return 0


if __name__ == "__main__":
    sys.exit(main())
