"""Run the fake NRS locally:

uv run python -m einvoice_nrs.fake                      # the packaged config.json, port 8090
uv run python -m einvoice_nrs.fake --config my.json --port 8091
"""

import argparse
from pathlib import Path

import uvicorn

from einvoice_nrs.fake import Config, create_app

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake NRS MBS API (dev only)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    uvicorn.run(create_app(Config.load(args.config)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
