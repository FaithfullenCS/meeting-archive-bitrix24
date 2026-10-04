"""Dedicated relay process. No logs of requests, authorization codes or bodies."""

import uvicorn

from .relay import RelayConfig, create_relay


def main():
    app = create_relay(RelayConfig.from_env())
    uvicorn.run(app, host="0.0.0.0", port=8787, proxy_headers=False,
        access_log=False, log_level="critical", log_config=None,
        limit_concurrency=64, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
