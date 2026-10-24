import sys

from ci_common import logs, server
from ci_common.config import Config


def main() -> None:
    config = Config.from_env()
    if "--healthcheck" in sys.argv:
        server.healthcheck(config)
    logs.init(config.service)
    from forecast.app import app

    server.run(app, config)


if __name__ == "__main__":
    main()
