"""Run the fake NRS:

uv run python -m einvoice_nrs.fake                      # the packaged config.json, 127.0.0.1:8090
uv run python -m einvoice_nrs.fake --config my.json --port 8091
python -m einvoice_nrs.fake --host 0.0.0.0              # on Render (port from $PORT)

FAKE_NRS_API_KEY / FAKE_NRS_API_SECRET, when set, replace the key pair in the config: a public
copy (Render) then accepts only the keys our server holds in Vault, not the ones in this repo.
"""

import argparse
import os
from pathlib import Path

import uvicorn

from einvoice_nrs.fake import Config, create_app

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"


def load_config(path: Path) -> Config:
    config = Config.load(path)
    key, secret = os.environ.get("FAKE_NRS_API_KEY"), os.environ.get("FAKE_NRS_API_SECRET")
    if key and secret:
        config = config.model_copy(update={"api_key": key, "api_secret": secret})
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake NRS MBS API (dev and test only)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8090")))
    args = parser.parse_args()
    uvicorn.run(create_app(load_config(args.config)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
