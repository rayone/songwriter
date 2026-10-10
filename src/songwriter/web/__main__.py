from __future__ import annotations

import uvicorn

from songwriter.config import HTTP_HOST, HTTP_PORT


def main() -> None:
    uvicorn.run(
        "songwriter.web.app:app",
        host=HTTP_HOST,
        port=HTTP_PORT,
        log_level="info",
        # Behind a reverse proxy, trust its X-Forwarded-* headers for client IPs.
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
