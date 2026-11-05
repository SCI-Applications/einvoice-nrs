"""The NRS MBS API client, used by the SI and the APP (context §4.1, §4.2).

Every call sends our `x-api-key` + `x-api-secret` (from Vault; never logged). Failures are one of:
- NrsUnavailableError: timeout, network error or 5xx. Retry later (with backoff).
- NrsRejectedError: 4xx. NRS said no; retrying the same request won't help.

Before calling, the caller must check that the business_id belongs to an authorized taxpayer of
the calling client (context §4.1): NRS doesn't enforce that for us.

Our API carries amounts as JSON strings ("1075.00"); NRS wants JSON numbers. to_nrs_json()
writes Decimals as exact number literals, never through float.
TODO(nrs-confirm): the /entity response fields (sandbox: our key sees no entities yet).
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
    def __init__(
        self, message: str, status_code: int | None = None, error_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_id = error_id  # NRS's id for this error: quote it to NRS support


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


def _error_of(response: httpx2.Response) -> tuple[str, str | None]:
    """(message, NRS error id). NRS answers errors as
    {"code", "data": null, "message": "error has occurred",
     "error": {"id", "handler", "details", "public_message"}}  (sandbox probe, 2026-10-30):
    `details` says what's wrong; `public_message` is always the same generic sentence."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase, None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error_id = str(error["id"]) if error.get("id") else None
            for key in ("details", "public_message", "message"):
                if error.get(key):
                    return str(error[key]), error_id
        for key in ("message", "error", "detail"):
            if body.get(key):
                return str(body[key]), None
    return json.dumps(body)[:500], None


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

    async def _call(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> Any:
        if json_body is not None:
            content = to_nrs_json(json_body)
        headers = {"Content-Type": "application/json"} if content is not None else None
        try:
            response = await self._http.request(method, path, content=content, headers=headers)
        except httpx2.TimeoutException as error:
            raise NrsUnavailableError(f"NRS timed out on {method} {path}") from error
        except httpx2.TransportError as error:
            raise NrsUnavailableError(f"NRS unreachable on {method} {path}: {error}") from error
        if response.status_code >= 400:
            message, error_id = _error_of(response)
            kind = NrsUnavailableError if response.status_code >= 500 else NrsRejectedError
            raise kind(message, response.status_code, error_id)
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

    async def sign(self, signed_text: bytes) -> Any:
        """Sign exactly these bytes (to_nrs_json of the invoice): the caller keeps the same bytes
        as the signed text, so what we store is byte for byte what NRS signed."""
        return await self._call("POST", "/invoice/sign", content=signed_text)

    async def confirm(self, irn: str) -> Any:
        return await self._call("GET", f"/invoice/confirm/{irn}")
