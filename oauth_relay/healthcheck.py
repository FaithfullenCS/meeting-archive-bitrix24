"""Container-internal probe; no credentials and no printing of response details."""

import json
import sys
from urllib.request import Request, urlopen

from . import PROTOCOL, PROTOCOL_VERSION
from .relay import RelayConfig


def main():
    try:
        config = RelayConfig.from_env()
        request = Request("http://127.0.0.1:8787" + config.path("health"),
            headers={"Host": config.public_host})
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read(4096))
        valid = payload.get("protocol") == PROTOCOL and payload.get("version") == PROTOCOL_VERSION
        return 0 if valid else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
