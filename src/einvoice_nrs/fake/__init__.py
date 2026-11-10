"""A fake NRS MBS API for tests, offline work and the test server (Render). It never talks to
NRS and holds no real data.

It answers the calls our NRS client makes (health, entity, irn/validate, validate, sign, confirm,
payment updates,
transmit and its lookups) in the shapes the NRS docs show, and a config decides which businesses
exist and which failures to simulate:

    {
      "api_key": "...", "api_secret": "...",
      "businesses": [{"business_id": "...", "tin": "...", "name": "...", "service_id": "..."}],
      "reject_irns": ["INV1-94ND90NR-20261029"],        # irn/validate says no
      "failures": [{"operation": "irn_validate", "status": 503, "times": 2},
                   {"operation": "sign", "status": 504, "after": true}],  # signs, then fails
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

from einvoice_nrs import __version__

IRN = re.compile(r"^(?P<number>[A-Z0-9]+)-(?P<service_id>[A-Z0-9]{8})-(?P<date>[0-9]{8})$")
AMOUNTS = ("payable_amount", "tax_inclusive_amount", "line_extension_amount")


class Business(BaseModel):
    business_id: str
    tin: str
    name: str
    service_id: str


class Failure(BaseModel):
    operation: (
        str  # health, entity, irn_validate, validate, sign, confirm, transmit, transmit_lookup
    )
    status: int = 503
    times: int = 1
    # True: do the work, then answer with the failure, as when NRS signs an invoice but the
    # answer is lost (a timeout). Only `sign` honours it.
    after: bool = False


MAX_CALLS = 1000


class Config(BaseModel):
    api_key: str = "fake-nrs-key"
    api_secret: str = "fake-nrs-secret"  # noqa: S105 (a fake, local-only value)
    businesses: list[Business] = Field(default_factory=list)
    reject_irns: list[str] = Field(default_factory=list)
    failures: list[Failure] = Field(default_factory=list)
    delay_seconds: float = 0
    # The transmit answer is the first step; each lookup then moves one step on (the last repeats).
    transmit_steps: list[str] = Field(
        default_factory=lambda: ["TRANSMITTING", "TRANSMITTED", "ACKNOWLEDGED"]
    )

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
    app.state.signed = signed  # for tests to inspect
    transmissions: dict[str, int] = {}  # irn -> lookups answered so far
    app.state.transmissions = transmissions
    payments: dict[str, list[dict[str, Any]]] = {}  # irn -> payment updates received, in order
    app.state.payments = payments
    app.state.calls = []  # (operation, body) for tests to inspect; only the latest MAX_CALLS

    async def gate(request: Request, operation: str) -> JSONResponse | None:
        """Auth, simulated delay and simulated failures, in that order."""
        app.state.calls.append((operation, await request.body()))
        del app.state.calls[:-MAX_CALLS]  # a long-running copy (Render) must not grow forever
        if (
            request.headers.get("x-api-key") != config.api_key
            or request.headers.get("x-api-secret") != config.api_secret
        ):
            return _error(401, "invalid api key or secret")
        if config.delay_seconds:
            await asyncio.sleep(config.delay_seconds)
        return failed_now(operation, after=False)

    def failed_now(operation: str, after: bool) -> JSONResponse | None:
        for failure in failures:
            if failure.operation == operation and failure.after == after and failure.times > 0:
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

    @app.get("/fake-nrs")
    async def identity() -> dict[str, object]:
        """Says "I am the fake". The real NRS has no such route, so a caller can make sure it
        is talking to a fake before sending test invoices (scripts/end_to_end.py does)."""
        return {"fake_nrs": True, "version": __version__}

    @app.get("/api")
    async def health() -> JSONResponse:
        """NRS's health check (sandbox: 200 {"healthy": true}, no key needed). A failure for
        operation "health" in the config makes it answer that status instead."""
        app.state.calls.append(("health", b""))
        if failed := failed_now("health", after=False):
            return failed
        return JSONResponse({"healthy": True})

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
        return failed_now("sign", after=True) or _ok({"ok": True})

    @app.get("/api/v1/invoice/confirm/{irn}")
    async def confirm(irn: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "confirm"):
            return failed
        if irn not in signed:
            return _error(404, "invoice not found")
        return _ok({"irn": irn, "payment_status": "PENDING"})

    @app.patch("/api/v1/invoice/update/{irn}")
    async def update_payment(irn: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "update_payment"):
            return failed
        if irn not in signed:
            return _error(404, "invoice not found")
        body = await request.json()
        status = body.get("payment_status")
        if status not in {"PENDING", "PARTIAL", "PAID", "REJECTED"}:
            return _error(400, "payment_status must be PENDING, PARTIAL, PAID or REJECTED")
        if status == "PARTIAL" and body.get("amount") is None:
            return _error(400, "amount is required for PARTIAL")
        payments.setdefault(irn, []).append(body)
        return _ok({"irn": irn, "payment_status": status})

    @app.post("/api/v1/invoice/transmit/{irn}")
    async def transmit(irn: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "transmit"):
            return failed
        if irn not in signed:
            return _error(404, "invoice not found or not signed")
        transmissions.setdefault(irn, 1)  # the first lookup answers the next step
        return _ok({"irn": irn, "status": config.transmit_steps[0]})

    @app.get("/api/v1/invoice/transmit/lookup/{irn}")
    async def transmit_lookup(irn: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "transmit_lookup"):
            return failed
        if irn not in transmissions:
            return _error(404, "transmission not found")
        steps = config.transmit_steps
        status = steps[min(transmissions[irn], len(steps) - 1)]
        transmissions[irn] += 1
        return _ok({"irn": irn, "status": status})

    @app.get("/api/v1/invoice/transmit/lookup/tin/{tin}")
    async def transmit_lookup_tin(tin: str, request: Request) -> JSONResponse:
        if failed := await gate(request, "transmit_lookup_tin"):
            return failed
        found = next((b for b in config.businesses if b.tin == tin), None)
        if found is None:
            return _error(404, "tin not found")
        return _ok({"tin": tin, "name": found.name})

    @app.get("/api/v1/invoice/transmit/self-health-check")
    async def transmit_health(request: Request) -> JSONResponse:
        return await gate(request, "transmit_health") or _ok({"ok": True})

    return app
