"""`python -m codetest_mcp` 진입점."""

from __future__ import annotations

import uvicorn

from codetest_mcp.config import settings


def main() -> None:
    uvicorn.run("codetest_mcp.main:app", host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
