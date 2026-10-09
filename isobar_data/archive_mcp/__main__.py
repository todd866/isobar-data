"""Run the Isobar archive MCP server over newline-delimited stdio."""

from __future__ import annotations

import argparse
from pathlib import Path

from .protocol import MCPServer


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Isobar archive MCP server")
    parser.add_argument("--store", required=True, type=Path, help="Archive store directory")
    args = parser.parse_args()
    from .archive import ArchiveStore

    MCPServer(ArchiveStore(args.store)).run_stdio()


if __name__ == "__main__":
    main()
