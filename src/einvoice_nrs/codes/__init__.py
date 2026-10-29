"""NRS code lists, as downloaded from the NRS sandbox by scripts/fetch_nrs_code_lists.py.

Every list is checked against manifest.json (count and sha256) when first loaded: a damaged
or hand-edited file raises CodeListError instead of judging invoices with a wrong list.
CODES_VERSION names the set of lists, so each check result can record which lists it used.
"""

import functools
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

PACKAGE_DIR = files(__name__)


class CodeListError(Exception):
    """A code-list file doesn't match the manifest."""


def _read_manifest(directory: Traversable) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(directory.joinpath("manifest.json").read_text("utf-8"))
    return manifest


CODES_VERSION: str = _read_manifest(PACKAGE_DIR)["codes_version"]


@dataclass(frozen=True)
class CodeLists:
    version: str
    invoice_types: frozenset[str]
    payment_means: frozenset[str]
    countries: frozenset[str]  # ISO alpha-2, e.g. "NG"
    states: frozenset[str]  # e.g. "NG-AB"
    lga_states: dict[str, str]  # LGA code -> state code, e.g. "NG-AB-ANO" -> "NG-AB"
    currency_decimals: dict[str, int]  # e.g. "NGN" -> 2
    tax_category_percents: dict[str, Decimal | None]  # None where NRS gives no percent
    hs_codes: frozenset[str]  # e.g. "2523.29"
    service_codes: frozenset[str]  # ISIC, e.g. "0111"
    unit_codes: frozenset[str]  # UN/ECE, e.g. "XBG"
    vat_exempt_hs_codes: frozenset[str]  # e.g. "2915.3100"

    def lga_in_state(self, lga: str, state: str) -> bool:
        return self.lga_states.get(lga) == state


def _percent(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None  # "Not Available" or empty


def load(directory: Traversable = PACKAGE_DIR) -> CodeLists:
    """Read and verify every list in `directory` (the packaged lists by default)."""
    manifest = _read_manifest(directory)
    lists: dict[str, list[dict[str, Any]]] = {}
    for name, expected in manifest["lists"].items():
        data = directory.joinpath(f"{name}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise CodeListError(f"{name}.json doesn't match manifest.json; re-run the fetch script")
        items = json.loads(data)
        if len(items) != expected["count"]:
            raise CodeListError(
                f"{name}.json has {len(items)} codes, manifest says {expected['count']}"
            )
        lists[name] = items

    def codes(name: str, key: str = "code") -> frozenset[str]:
        return frozenset(item[key] for item in lists[name])

    return CodeLists(
        version=manifest["codes_version"],
        invoice_types=codes("invoice_types"),
        payment_means=codes("payment_means"),
        countries=codes("countries", "alpha_2"),
        states=codes("states"),
        lga_states={item["code"]: item["state_code"] for item in lists["lgas"]},
        currency_decimals={item["code"]: item["decimal_digits"] for item in lists["currencies"]},
        tax_category_percents={
            item["code"]: _percent(item["percent"]) for item in lists["tax_categories"]
        },
        hs_codes=codes("hs_codes", "hscode"),
        service_codes=codes("services_codes"),
        unit_codes=codes("invoice_quantity_codes"),
        vat_exempt_hs_codes=codes("vat_exemptions", "harmonized_system_code"),
    )


@functools.cache
def code_lists() -> CodeLists:
    """The packaged lists, loaded and verified on first use."""
    return load()
