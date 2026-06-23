from __future__ import annotations

import argparse
import sys

from . import __version__
from .mcp_server import run_stdio


def main() -> None:
    parser = argparse.ArgumentParser(description="Open Subagent MCP server")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--stdio", action="store_true", help="Run the MCP stdio server")
    args = parser.parse_args()
    if args.stdio or len(sys.argv) == 1:
        run_stdio()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
