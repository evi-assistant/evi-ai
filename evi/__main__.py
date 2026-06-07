"""Run the Evi CLI as `python -m evi`.

Handy when the `evi` console script isn't on PATH — notably for MCP clients
(Claude Desktop / Cursor / Cline) that spawn Evi as a server subprocess by
interpreter path, e.g. `python -m evi mcp serve`.
"""

from evi.apps.cli.main import app

if __name__ == "__main__":
    app()
