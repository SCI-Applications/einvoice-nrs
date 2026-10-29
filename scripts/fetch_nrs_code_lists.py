"""Download the NRS invoice code lists into shared/src/einvoice_shared/nrs_codes/.

    uv run python scripts/fetch_nrs_code_lists.py
    uv run python scripts/fetch_nrs_code_lists.py --base-url https://<other-nrs-host>

The resource endpoints need no API key. Run it where NRS is reachable, review the diff, and
commit the files: code-list changes go through review like code (context §7.4). The files keep
NRS's own field names; einvoice_shared.nrs_codes reads them.
"""

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SANDBOX = "https://eivc-k6z6d.ondigitalocean.app"
OUT = Path(__file__).resolve().parent.parent / "shared" / "src" / "einvoice_shared" / "nrs_codes"

# Output file -> endpoint names to try (the NRS docs spell some of them two ways).
RESOURCES: dict[str, list[str]] = {
    "invoice_types": ["invoice-types"],
    "payment_means": ["payment-means", "payment_means"],
    "tax_categories": ["tax-categories"],
    "currencies": ["currencies"],
    "invoice_quantity_codes": ["invoice-quantity-codes"],
    "hs_codes": ["hs-codes"],
    "services_codes": ["services-codes"],
    "countries": ["countries"],
    "states": ["states"],
    "lgas": ["lgas"],
    "vat_exemptions": ["vat-exemptions"],
}


def fetch(base_url: str, endpoint: str) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v1/invoice/resources/{endpoint}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (https only)
        body = json.load(response)
    data = body["data"] if isinstance(body, dict) and "data" in body else body
    if not isinstance(data, list):
        raise ValueError(f"{url}: expected a list, got {type(data).__name__}")
    return data


def to_lines(items: list[dict[str, Any]]) -> str:
    """One compact JSON object per line: small files, readable diffs."""
    rows = [
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in items
    ]
    return "[\n" + ",\n".join(rows) + "\n]\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="fetch_nrs_code_lists")
    parser.add_argument("--base-url", default=SANDBOX)
    base_url = parser.parse_args().base_url
    if not base_url.startswith("https://"):
        parser.error("--base-url must start with https://")

    OUT.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "source": base_url,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "lists": {},
    }
    failed = []
    for name, endpoints in RESOURCES.items():
        for endpoint in endpoints:
            try:
                items = fetch(base_url, endpoint)
            except (urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
                last_error = error
                continue
            text = to_lines(items)
            (OUT / f"{name}.json").write_text(text, encoding="utf-8")
            manifest["lists"][name] = {
                "endpoint": endpoint,
                "count": len(items),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            print(f"{name:24} {len(items):6} entries  ({endpoint})")
            break
        else:
            failed.append(name)
            print(f"{name:24} FAILED: {last_error}", file=sys.stderr)

    # CODES_VERSION: date + short hash of every list, recorded on each checked invoice.
    combined = "".join(v["sha256"] for _, v in sorted(manifest["lists"].items()))
    manifest["codes_version"] = (
        f"{manifest['fetched_at'][:10]}-{hashlib.sha256(combined.encode()).hexdigest()[:8]}"
    )
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"codes_version: {manifest['codes_version']}")
    if failed:
        print(f"Could not download: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
