"""The NRS MBS API client, shared by SI and APP (context §4.1, §4.2).

Every call sends our `x-api-key` + `x-api-secret` (from Vault; never logged). Failures are one of:
- NrsUnavailableError: timeout, network error or 5xx. Retry later (with backoff).
- NrsRejectedError: 4xx. NRS said no; retrying the same request won't help.

Before calling, the caller must check that the business_id belongs to an authorized taxpayer of
the calling client (context §4.1): NRS doesn't enforce that for us.

Our API carries amounts as JSON strings ("1075.00"); NRS wants JSON numbers. to_nrs_json()
writes Decimals as exact number literals, never through float.
TODO(nrs-confirm): the error body shape and the /entity response fields (sandbox test).
"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Self

import httpx2

TIMEOUT_SECONDS = 10.0
API_PREFIX = "/api/v1"
_AMOUNT = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?$")  # a valid JSON number


class NrsError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NrsUnavailableError(NrsError):
    """Timeout, network error or 5xx: try again later."""


class NrsRejectedError(NrsError):
    """4xx: NRS refused the request."""


@dataclass(frozen=True)
class NrsCredentials:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:  # keep secrets out of logs and tracebacks
        return "NrsCredentials(***)"

    @classmethod
    def from_vault(cls, data: dict[str, str]) -> Self:
        return cls(api_key=data["api_key"], api_secret=data["api_secret"])


# Fields of the NRS invoice that are numbers (Money, Quantity or Rate in our API).
AMOUNT_FIELDS = frozenset(
    {
        "amount",
        "base_quantity",
        "discount_amount",
        "discount_rate",
        "fee_amount",
        "fee_rate",
        "invoiced_quantity",
        "line_extension_amount",
        "payable_amount",
        "percent",
        "price_amount",
        "tax_amount",
        "tax_exclusive_amount",
        "tax_inclusive_amount",
        "taxable_amount",
    }
)


def _write(value: Any, key: str | None = None) -> str:
    if isinstance(value, dict):
        items = (f"{json.dumps(k)}:{_write(v, k)}" for k, v in value.items())
        return "{" + ",".join(items) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_write(item, key) for item in value) + "]"
    if key in AMOUNT_FIELDS and isinstance(value, str) and _AMOUNT.match(value):
        return value  # already exact decimal text, e.g. "1075.00" -> 1075.00
    if isinstance(value, Decimal):
        return format(value, "f")
    return json.dumps(value, ensure_ascii=False)


def to_nrs_json(invoice: dict[str, Any]) -> bytes:
    """The invoice as NRS wants it: amounts as JSON numbers, written exactly (never via float)."""
    return _write(invoice).encode()


def _message(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            for key in ("public_message", "message", "details"):
                if error.get(key):
                    return str(error[key])
        for key in ("message", "error", "detail"):
            if body.get(key):
                return str(body[key])
    return json.dumps(body)[:500]


class NrsClient:
    def __init__(
        self,
        base_url: str,
        credentials: NrsCredentials,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._http = httpx2.AsyncClient(
            base_url=base_url.rstrip("/") + API_PREFIX,
            headers={
                "x-api-key": credentials.api_key,
                "x-api-secret": credentials.api_secret,
                "Accept": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> Any:
        content = to_nrs_json(json_body) if json_body is not None else None
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            response = await self._http.request(method, path, content=content, headers=headers)
        except httpx2.TimeoutException as error:
            raise NrsUnavailableError(f"NRS timed out on {method} {path}") from error
        except httpx2.TransportError as error:
            raise NrsUnavailableError(f"NRS unreachable on {method} {path}: {error}") from error
        if response.status_code >= 500:
            raise NrsUnavailableError(_message(response), response.status_code)
        if response.status_code >= 400:
            raise NrsRejectedError(_message(response), response.status_code)
        if not response.content:
            return None
        body = response.json()
        return body.get("data", body) if isinstance(body, dict) else body

    async def get_entity(self, business_id: str) -> dict[str, Any]:
        data: dict[str, Any] = await self._call("GET", f"/entity/{business_id}")
        return data

    async def validate_irn(self, invoice_reference: str, business_id: str, irn: str) -> Any:
        return await self._call(
            "POST",
            "/invoice/irn/validate",
            {"invoice_reference": invoice_reference, "business_id": business_id, "irn": irn},
        )

    async def validate(self, invoice: dict[str, Any]) -> Any:
        return await self._call("POST", "/invoice/validate", invoice)

    async def sign(self, invoice: dict[str, Any]) -> Any:
        return await self._call("POST", "/invoice/sign", invoice)

    async def confirm(self, irn: str) -> Any:
        return await self._call("GET", f"/invoice/confirm/{irn}")
