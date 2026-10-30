"""Ask the NRS sandbox what it really does, and save its exact answers to a report.

    uv run python scripts/nrs_sandbox_probe.py
    uv run python scripts/nrs_sandbox_probe.py --business-id <uuid> --service-id <8 chars>

Run by hand on a machine whose local Vault holds our sandbox key (secret/nrs/si); never in CI.
It only reads and validates: it never calls sign, so nothing is cleared. It refuses any host but
the sandbox and this machine. The report holds NRS's status codes and response bodies, never our
key or the request headers: share the report, not the key.

What it checks (context §11 and the TODO(nrs-confirm) notes in the code):
1. Health, the entity list our key can see, and one entity in detail (field names: TIN, ...).
2. irn/validate: which invoice_reference NRS expects, wrong Service ID, wrong format.
3. invoice/validate with invoices that each break one rule our checks enforce, to learn which
   rules NRS itself enforces (addresses, totals, rounding, discount rate, HS code, unit code).
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx2
from pydantic import SecretStr

from einvoice_shared.nrs_client import API_PREFIX, NrsCredentials, to_nrs_json
from einvoice_shared.vault import Vault

SANDBOX = "https://eivc-k6z6d.ondigitalocean.app"
ALLOWED_HOSTS = {"eivc-k6z6d.ondigitalocean.app", "127.0.0.1", "localhost"}
LAGOS = timezone(timedelta(hours=1))
DEFAULT_REPORT = Path.home() / "nrs-probe-report.json"
PLACEHOLDER_BUSINESS_ID = "00000000-0000-4000-8000-000000000000"
PLACEHOLDER_SERVICE_ID = "PRB00001"
# The first run (2026-10-30) got an empty list from ?size=10&page=1; try other shapes too.
ENTITY_LISTS = [
    ("?size=10&page=1", "/entity?size=10&page=1"),
    ("(no query)", "/entity"),
    ("?size=10&page=0", "/entity?size=10&page=0"),
    (
        "?size=100&page=1, sorted",
        "/entity?size=100&page=1&sort_by=created_at&sort_direction_desc=true",
    ),
]


def _label(step: str, placeholder: bool) -> str:
    return f"{step} (made-up business)" if placeholder else step


def _find(data: Any, *names: str) -> Any:
    """The first value whose key contains one of the names (case-insensitive), searched deeply."""
    if isinstance(data, dict):
        for key, value in data.items():
            if any(name in key.lower() for name in names) and isinstance(value, str | int):
                return value
        for value in data.values():
            if (found := _find(value, *names)) is not None:
                return found
    if isinstance(data, list):
        for item in data:
            if (found := _find(item, *names)) is not None:
                return found
    return None


class Probe:
    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http
        self.results: list[dict[str, Any]] = []

    async def call(
        self, step: str, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        content = to_nrs_json(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            response = await self.http.request(method, path, content=content, headers=headers)
            try:
                answer: Any = response.json()
            except ValueError:
                answer = response.text[:2000]
            status: int | str = response.status_code
        except httpx2.HTTPError as error:
            answer, status = f"{type(error).__name__}: {error}", "no response"
        self.results.append(
            {
                "step": step,
                "request": {
                    "method": method,
                    "path": path,
                    "body": content.decode() if content else None,  # exactly as sent
                },
                "status": status,
                "response": answer,
            }
        )
        print(f"{status!s:>12}  {step}")
        return answer


def _invoice(business_id: str, irn: str, tin: str, issue_date: str) -> dict[str, Any]:
    """A correct invoice (our maths rules hold); the variants each break one rule."""
    address = {"street_name": "12 Marina Rd", "city_name": "Lagos", "country": "NG"}
    return {
        "business_id": business_id,
        "irn": irn,
        "issue_date": issue_date,
        "invoice_type_code": "381",
        "document_currency_code": "NGN",
        "tax_currency_code": "NGN",  # required (run 2, 2026-10-30)
        "invoice_kind": "B2B",
        "accounting_supplier_party": {
            "party_name": "Test Business",
            "tin": tin,
            "email": "probe-supplier@example.com",
            "postal_address": address,
        },
        "accounting_customer_party": {
            "party_name": "Probe Buyer Ltd",
            "tin": "TIN-000001",
            "email": "probe-buyer@example.com",
            "postal_address": address,
        },
        "tax_total": [
            {
                "tax_amount": "75.00",
                "tax_subtotal": [
                    {
                        "taxable_amount": "1000.00",
                        "tax_amount": "75.00",
                        "tax_category": {"id": "STANDARD_VAT", "percent": "7.5"},
                    }
                ],
            }
        ],
        "legal_monetary_total": {
            "line_extension_amount": "1000.00",
            "tax_exclusive_amount": "1000.00",
            "tax_inclusive_amount": "1075.00",
            "payable_amount": "1075.00",
        },
        "invoice_line": [
            {
                "hsn_code": "2523.29",
                "product_category": "Building materials",
                "invoiced_quantity": "20",
                "line_extension_amount": "1000.00",
                "item": {"name": "Cement", "description": "Portland cement, 50kg"},
                "price": {"price_amount": "50.00", "base_quantity": "1", "price_unit": "XBG"},
            }
        ],
    }


def _variants(base: dict[str, Any]) -> dict[str, dict[str, Any]]:
    def changed(change: Any) -> dict[str, Any]:
        invoice: dict[str, Any] = json.loads(json.dumps(base))
        change(invoice)
        return invoice

    def no_addresses(i: dict[str, Any]) -> None:
        del i["accounting_supplier_party"]["postal_address"]
        del i["accounting_customer_party"]["postal_address"]

    def bad_total(i: dict[str, Any]) -> None:
        i["legal_monetary_total"]["payable_amount"] = "1000.00"

    def rounding(i: dict[str, Any]) -> None:  # 1000.10 x 7.5% = 75.0075: we say 75.01
        i["invoice_line"][0]["price"]["price_amount"] = "50.005"
        i["invoice_line"][0]["line_extension_amount"] = "1000.10"
        i["legal_monetary_total"] = {
            "line_extension_amount": "1000.10",
            "tax_exclusive_amount": "1000.10",
            "tax_inclusive_amount": "1075.10",
            "payable_amount": "1075.10",
        }
        i["tax_total"][0]["tax_amount"] = "75.00"  # rounded down instead of half up
        i["tax_total"][0]["tax_subtotal"][0].update(taxable_amount="1000.10", tax_amount="75.00")

    def discount_rate_only(i: dict[str, Any]) -> None:  # a rate with no amount change
        i["invoice_line"][0]["discount_rate"] = "10"

    def hs_code(i: dict[str, Any]) -> None:
        i["invoice_line"][0]["hsn_code"] = "CC-001"

    def unit(i: dict[str, Any]) -> None:
        i["invoice_line"][0]["price"]["price_unit"] = "NGN per 1"

    def invoice_type(i: dict[str, Any]) -> None:
        i["invoice_type_code"] = "396"

    return {
        "validate: correct invoice (baseline)": base,
        "validate: no postal addresses": changed(no_addresses),
        "validate: payable doesn't add up": changed(bad_total),
        "validate: tax rounded down, not half up": changed(rounding),
        "validate: discount_rate without discount_amount": changed(discount_rate_only),
        "validate: HS code CC-001": changed(hs_code),
        "validate: price_unit 'NGN per 1'": changed(unit),
        "validate: invoice_type_code 396": changed(invoice_type),
    }


async def run(args: argparse.Namespace) -> int:
    host = urlparse(args.base_url).hostname
    if host not in ALLOWED_HOSTS:
        print(f"Refusing {args.base_url}: this probe only talks to the sandbox or this machine.")
        return 2
    token = os.environ.get("VAULT_TOKEN", "dev-root-token")
    vault = Vault(os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200"), SecretStr(token), "secret")
    try:
        keys = NrsCredentials.from_vault(await vault.read(args.vault_path))
    except Exception as error:  # any failure here means: no usable keys
        print(f"Can't read our NRS keys from Vault at secret/{args.vault_path}: {error}")
        return 2

    headers = {"x-api-key": keys.api_key, "x-api-secret": keys.api_secret}
    async with httpx2.AsyncClient(
        base_url=args.base_url.rstrip("/") + API_PREFIX, headers=headers, timeout=30
    ) as http:
        probe = Probe(http)
        await probe.call("health", "GET", args.base_url.rstrip("/") + "/api")
        business_id = args.business_id
        for label, path in ENTITY_LISTS:
            listing = await probe.call(f"entity list {label}", "GET", path)
            business_id = business_id or _find(listing, "id")
        placeholder = not business_id
        if placeholder:
            # Nothing to work with: carry on with made-up ids, to learn NRS's error format and
            # what it checks first. These steps are labelled "(made-up business)".
            print("No business id found; continuing with a made-up business to learn the errors.")
            business_id = PLACEHOLDER_BUSINESS_ID
        entity = await probe.call(
            _label("entity detail", placeholder), "GET", f"/entity/{business_id}"
        )
        tin = args.tin or _find(entity, "tin") or "UNKNOWN-TIN"
        service_id = args.service_id or _find(entity, "service") or PLACEHOLDER_SERVICE_ID
        placeholder = placeholder or service_id == PLACEHOLDER_SERVICE_ID

        today = datetime.now(UTC).astimezone(LAGOS).date()
        issue_date = today.isoformat()
        number = "PROBE" + datetime.now(UTC).strftime("%H%M%S")
        irn = f"{number}-{service_id}-{today:%Y%m%d}"
        validate_irn = "/invoice/irn/validate"
        body = {"business_id": business_id, "irn": irn}
        await probe.call(
            _label("irn/validate: invoice_reference = invoice number", placeholder),
            "POST",
            validate_irn,
            {**body, "invoice_reference": number},
        )
        await probe.call(
            _label("irn/validate: invoice_reference = whole IRN", placeholder),
            "POST",
            validate_irn,
            {**body, "invoice_reference": irn},
        )
        await probe.call(
            _label("irn/validate: wrong Service ID", placeholder),
            "POST",
            validate_irn,
            {**body, "irn": f"{number}-ZZZZ9999-{today:%Y%m%d}", "invoice_reference": number},
        )
        await probe.call(
            _label("irn/validate: wrong format", placeholder),
            "POST",
            validate_irn,
            {**body, "irn": "not-an-irn", "invoice_reference": number},
        )
        for step, invoice in _variants(_invoice(business_id, irn, tin, issue_date)).items():
            await probe.call(_label(step, placeholder), "POST", "/invoice/validate", invoice)
    return _save(probe, args.out)


def _save(probe: Probe, out: Path) -> int:
    report = {"run_at": datetime.now(UTC).isoformat(), "results": probe.results}
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nReport: {out}  (status codes and NRS's answers only; no keys)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default=SANDBOX)
    parser.add_argument("--vault-path", default="nrs/si")
    parser.add_argument("--business-id", help="Default: the first business in the entity list")
    parser.add_argument("--tin", help="The business's TIN, if /entity doesn't show it")
    parser.add_argument("--service-id", help="The business's 8-character NRS Service ID")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
