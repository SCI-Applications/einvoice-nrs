"""A fake NRS MBS API for tests and offline work. Dev only: never deployed.

It answers the calls our NRS client makes (entity, irn/validate, validate, sign, confirm) in the
shapes the NRS docs show, and a config decides which businesses exist and which failures to
simulate:

    {
      "api_key": "...", "api_secret": "...",
      "businesses": [{"business_id": "...", "tin": "...", "name": "...", "service_id": "..."}],
      "reject_irns": ["INV1-94ND90NR-20261029"],        # irn/validate says no
      "failures": [{"operation": "irn_validate", "status": 503, "times": 2}],
      "delay_seconds": 0                                  # > client timeout = simulated timeout
    }

Errors use NRS's real shape (sandbox probe, 2026-10-30). Like NRS, it wants the IRN uppercase.
TODO(nrs-confirm): the success bodies and the /entity fields (no real business seen yet).
"""

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

IRN = re.compile(r"^(?P<number>[A-Z0-9]+)-(?P<service_id>[A-Z0-9]{8})-(?P<date>[0-9]{8})$")
AMOUNTS = ("payable_amount", "tax_inclusive_amount", "line_extension_amount")


class Business(BaseModel):
    business_id: str
    tin: str
    name: str
    service_id: str


class Failure(BaseModel):
    operation: str  # entity, irn_validate, validate, sign, confirm
    status: int = 503
    times: int = 1


class Config(BaseModel):
    api_key: str = "fake-nrs-key"
    api_secret: str = "fake-nrs-secret"  # noqa: S105 (a fake, local-only value)
    businesses: list[Business] = Field(default_factory=list)
    reject_irns: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    delay_seconds: float = 0

    @classmethod
    def load(cls, path: Path) -> "Config":
        return cls.model_validate(json.loads(path.read_text("utf-8")))


def _error(status: int, details: str) -> JSONResponse:
    """NRS's real error shape (sandbox probe, 2026-10-30)."""
    return JSONResponse(
        {
            "code": status,
            "data": None,
            "message": "error has occurred",
            "error": {
                "id": str(uuid.uuid4()),
                "handler": "fake_nrs",
                "details": details,
                "public_message": "validation failed: we are unable to process your request. "
                "also confirm this is not a duplicate request",
            },
        },
        status_code=status,
    )


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"code": 200, "data": data})


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Fake NRS")

    def business(business_id: object) -> Business | None:
        # Looked up on every call, so a test can add businesses to the config later.
        return next((b for b in config.businesses if b.business_id == business_id), None)

    failures = [f.model_copy() for f in config.failures]
    signed: set[str] = set()
    app.state.calls = []  # (operation, body) for tests to inspect

    async def gate(request: Request, operation: str) -> JSONResponse | None:
        """Auth, simulated delay and simulated failures, in that order."""
        app.state.calls.append((operation, await request.body()))
        if (
            request.headers.get("x-api-key") != config.api_key
            or request.headers.get("x-api-secret") != config.api_secret
        ):
            return _error(401, "invalid api key or secret")
        if config.delay_seconds:
            await asyncio.sleep(config.delay_seconds)
        for failure in failures:
            if failure.operation == operation and failure.times > 0:
                failure.times -= 1
                return _error(failure.status, f"simulated {failure.status} on {operation}")
        return None

    def check_invoice(body: dict[str, Any]) -> JSONResponse | None:
        if business(body.get("business_id")) is None:
            return _error(404, "business not found")
        if not IRN.match(str(body.get("irn", ""))):
            return _error(400, "invalid irn")
        total = body.get("legal_monetary_total", {})
        for field in AMOUNTS:
            if field in total and not isinstance(total[field], int | float):
                return _error(400, f"legal_monetary_total.{field} must be a number")
        return None

    @app.get("/api/v1/entity/{business_id}")
    async def entity(business_id: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "entity"):
            return failed
        found = business(business_id)
        if found is None:
            return _error(404, "entity not found")
        return _ok({"id": found.business_id, "tin": found.tin, "name": found.name})

    @app.post("/api/v1/invoice/irn/validate")
    async def irn_validate(request: Request) -> JSONResponse:
        if failed := await gate(request, "irn_validate"):
            return failed
        body = await request.json()
        found = business(body.get("business_id"))
        if found is None:
            return _error(404, "business not found")
        irn = str(body.get("irn", ""))
        match = IRN.match(irn)
        if match is None or match["service_id"] != found.service_id:
            return _error(400, "invalid irn")
        if body.get("invoice_reference") != match["number"]:
            return _error(400, "invoice_reference does not match the irn")
        if irn in config.reject_irns:
            return _error(400, "irn rejected")
        return _ok({"ok": True})

    @app.post("/api/v1/invoice/validate")
    async def validate(request: Request) -> JSONResponse:
        if failed := await gate(request, "validate"):
            return failed
        return check_invoice(await request.json()) or _ok({"ok": True})

    @app.post("/api/v1/invoice/sign")
    async def sign(request: Request) -> JSONResponse:
        if failed := await gate(request, "sign"):
            return failed
        body = await request.json()
        if invalid := check_invoice(body):
            return invalid
        if body["irn"] in signed:
            return _error(400, "invoice already signed")
        signed.add(body["irn"])
        return _ok({"ok": True})

    @app.get("/api/v1/invoice/confirm/{irn}")
    async def confirm(irn: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "confirm"):
            return failed
        if irn not in signed:
            return _error(404, "invoice not found")
        return _ok({"irn": irn, "payment_status": "PENDING"})

    return app
