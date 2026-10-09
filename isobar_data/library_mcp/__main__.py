"""Run the aviation library MCP server over newline-delimited stdio."""
from __future__ import annotations

from .protocol import MCPServer


def main() -> None:
    MCPServer().run_stdio()


if __name__ == "__main__":
    main()
