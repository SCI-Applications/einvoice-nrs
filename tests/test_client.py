"""The NRS client against the fake NRS (in-process, nothing real is called)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

from einvoice_nrs.client import (
    NrsClient,
    NrsCredentials,
    NrsRejectedError,
    NrsUnavailableError,
    to_nrs_json,
)
from einvoice_nrs.fake import MAX_CALLS, Business, Config, Failure, create_app
from einvoice_nrs.fake.__main__ import DEFAULT_CONFIG, load_config

BUSINESS = Business(
    business_id="7d3e1f20-8a4b-4c6d-9e0f-1a2b3c4d5e6f",
    tin="12345678-0001",
    name="Acme Trading Ltd",
    service_id="94ND90NR",
)
IRN = "INV2026001-94ND90NR-20261029"
KEYS = NrsCredentials(api_key="fake-nrs-key", api_secret="fake-nrs-secret")  # noqa: S106
INVOICE: dict[str, Any] = {
    "business_id": BUSINESS.business_id,
    "irn": IRN,
    "legal_monetary_total": {
        "line_extension_amount": "1000.00",
        "tax_exclusive_amount": "1000.00",
        "tax_inclusive_amount": "1075.00",
        "payable_amount": "1075.00",
    },
}


def run(
    action: Callable[[NrsClient], Awaitable[Any]],
    keys: NrsCredentials = KEYS,
    **config: Any,
) -> Any:
    app = create_app(Config(businesses=[BUSINESS], **config))

    async def go() -> Any:
        transport = httpx2.ASGITransport(app=app)
        async with NrsClient("http://fake-nrs", keys, transport=transport) as client:
            return await action(client)

    return asyncio.run(go())


def test_entity() -> None:
    entity = run(lambda nrs: nrs.get_entity(BUSINESS.business_id))
    assert entity["tin"] == BUSINESS.tin
    with pytest.raises(NrsRejectedError, match="entity not found") as error:
        run(lambda nrs: nrs.get_entity("unknown"))
    assert error.value.status_code == 404


def test_wrong_keys_are_rejected() -> None:
    wrong = NrsCredentials(api_key="fake-nrs-key", api_secret="wrong")  # noqa: S106
    with pytest.raises(NrsRejectedError, match="invalid api key") as error:
        run(lambda nrs: nrs.get_entity(BUSINESS.business_id), keys=wrong)
    assert error.value.status_code == 401


def test_irn_validate() -> None:
    assert run(lambda nrs: nrs.validate_irn("INV2026001", BUSINESS.business_id, IRN))
    with pytest.raises(NrsRejectedError, match="irn rejected"):
        run(
            lambda nrs: nrs.validate_irn("INV2026001", BUSINESS.business_id, IRN),
            reject_irns=[IRN],
        )
    other_service = "INV2026001-ZZZZ9999-20261029"
    with pytest.raises(NrsRejectedError, match="invalid irn"):
        run(lambda nrs: nrs.validate_irn("INV2026001", BUSINESS.business_id, other_service))


def test_5xx_is_unavailable_then_works() -> None:
    async def twice(nrs: NrsClient) -> Any:
        with pytest.raises(NrsUnavailableError) as error:
            await nrs.validate_irn("INV2026001", BUSINESS.business_id, IRN)
        assert error.value.status_code == 503
        return await nrs.validate_irn("INV2026001", BUSINESS.business_id, IRN)

    assert run(twice, failures=[Failure(operation="irn_validate", status=503, times=1)])


def test_timeout_is_unavailable() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    async def go() -> None:
        transport = httpx2.MockTransport(timeout)
        async with NrsClient("http://fake-nrs", KEYS, transport=transport) as nrs:
            await nrs.confirm(IRN)

    with pytest.raises(NrsUnavailableError, match="timed out"):
        asyncio.run(go())


def test_amounts_go_to_nrs_as_exact_numbers() -> None:
    sent = to_nrs_json(
        {
            "invoice_number": "007",  # not an amount: stays a string
            "tax_total": [{"tax_amount": "75.00", "tax_subtotal": [{"percent": "7.5"}]}],
            "legal_monetary_total": {"payable_amount": "123456789012345.67"},
            "note": "1075.00",
        }
    )
    assert sent == (
        b'{"invoice_number":"007","tax_total":[{"tax_amount":75.00,"tax_subtotal":'
        b'[{"percent":7.5}]}],"legal_monetary_total":{"payable_amount":123456789012345.67},'
        b'"note":"1075.00"}'
    )


def test_validate_sign_confirm() -> None:
    async def flow(nrs: NrsClient) -> Any:
        await nrs.validate(INVOICE)  # the fake rejects amounts that aren't JSON numbers
        await nrs.sign(to_nrs_json(INVOICE))
        with pytest.raises(NrsRejectedError, match="already signed"):
            await nrs.sign(to_nrs_json(INVOICE))
        return await nrs.confirm(IRN)

    assert run(flow)["irn"] == IRN


def test_fake_rejects_string_amounts() -> None:
    app = create_app(Config(businesses=[BUSINESS]))

    async def go() -> httpx2.Response:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://fake-nrs") as http:
            return await http.post(
                "/api/v1/invoice/validate",
                content=json.dumps(INVOICE),
                headers={"x-api-key": KEYS.api_key, "x-api-secret": KEYS.api_secret},
            )

    response = asyncio.run(go())
    assert response.status_code == 400
    assert "must be a number" in response.text


def test_credentials_never_show_in_repr() -> None:
    assert "fake-nrs-secret" not in repr(KEYS)


def test_nrs_errors_give_details_and_the_nrs_error_id() -> None:
    with pytest.raises(NrsRejectedError) as error:
        run(lambda nrs: nrs.validate_irn("INV2026001", BUSINESS.business_id, "inv1-94nd90nr-1"))
    assert str(error.value) == "invalid irn"  # NRS's `details`, not its generic public_message
    assert error.value.error_id


def test_nrs_shaped_error_body_is_read() -> None:
    def nrs_style(request: httpx2.Request) -> httpx2.Response:
        # Copied from the sandbox (probe run 2, 2026-10-30).
        return httpx2.Response(
            400,
            json={
                "code": 400,
                "data": None,
                "message": "error has occurred",
                "error": {
                    "id": "5c751690-d344-4272-a1be-095442c8e650",
                    "handler": "invoice_actions",
                    "details": "this business does not exist",
                    "public_message": "validation failed: we are unable to process your "
                    "request. also confirm this is not a duplicate request",
                },
            },
        )

    async def go() -> None:
        transport = httpx2.MockTransport(nrs_style)
        async with NrsClient("http://nrs", KEYS, transport=transport) as nrs:
            await nrs.validate_irn("X", "b", "X-ABCDEFGH-20261030")

    with pytest.raises(NrsRejectedError, match="this business does not exist") as error:
        asyncio.run(go())
    assert error.value.error_id == "5c751690-d344-4272-a1be-095442c8e650"


def test_fake_says_it_is_the_fake() -> None:
    async def go() -> dict[str, Any]:
        transport = httpx2.ASGITransport(app=create_app(Config(businesses=[BUSINESS])))
        async with httpx2.AsyncClient(transport=transport, base_url="http://fake-nrs") as http:
            body: dict[str, Any] = (await http.get("/fake-nrs")).json()
            return body

    assert asyncio.run(go())["fake_nrs"] is True


def test_fake_keeps_only_the_latest_calls() -> None:
    app = create_app(Config(businesses=[BUSINESS]))

    async def go() -> None:
        transport = httpx2.ASGITransport(app=app)
        async with NrsClient("http://fake-nrs", KEYS, transport=transport) as nrs:
            for _ in range(MAX_CALLS + 5):
                await nrs.validate_irn("INV2026001", BUSINESS.business_id, IRN)

    asyncio.run(go())
    assert len(app.state.calls) == MAX_CALLS


def test_render_keys_replace_the_config_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NRS_API_KEY", "server-key")
    monkeypatch.setenv("FAKE_NRS_API_SECRET", "server-secret")
    config = load_config(DEFAULT_CONFIG)
    assert (config.api_key, config.api_secret) == ("server-key", "server-secret")
    monkeypatch.delenv("FAKE_NRS_API_KEY")
    assert load_config(DEFAULT_CONFIG).api_key == "fake-nrs-key"


def test_transmit_then_lookups_step_through_delivery() -> None:
    async def flow(nrs: NrsClient) -> list[Any]:
        await nrs.sign(to_nrs_json(INVOICE))
        accepted = await nrs.transmit(IRN)
        steps = [(await nrs.transmit_lookup(IRN))["status"] for _ in range(4)]
        return [accepted["status"], *steps]

    assert run(flow) == [
        "TRANSMITTING",
        "TRANSMITTED",
        "ACKNOWLEDGED",
        "ACKNOWLEDGED",
        "ACKNOWLEDGED",
    ]


def test_transmit_needs_a_signed_invoice_and_can_fail() -> None:
    async def unsigned(nrs: NrsClient) -> None:
        await nrs.transmit(IRN)

    with pytest.raises(NrsRejectedError, match="not signed"):
        run(unsigned)

    async def lookup_unknown(nrs: NrsClient) -> None:
        await nrs.transmit_lookup(IRN)

    with pytest.raises(NrsRejectedError, match="transmission not found"):
        run(lookup_unknown)

    async def outage(nrs: NrsClient) -> Any:
        await nrs.sign(to_nrs_json(INVOICE))
        with pytest.raises(NrsUnavailableError):
            await nrs.transmit(IRN)
        return await nrs.transmit(IRN)  # the simulated outage was once

    assert run(outage, failures=[Failure(operation="transmit", status=503)])["irn"] == IRN


def test_buyer_tin_lookup_and_health() -> None:
    async def flow(nrs: NrsClient) -> Any:
        assert (await nrs.transmit_health())["ok"] is True
        with pytest.raises(NrsRejectedError, match="tin not found"):
            await nrs.transmit_lookup_tin("99999999-0001")
        return await nrs.transmit_lookup_tin(BUSINESS.tin)

    assert run(flow)["name"] == BUSINESS.name


def test_health_needs_no_key_and_can_fail() -> None:
    async def get(fake: Any) -> httpx2.Response:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=fake), base_url="http://fake-nrs"
        ) as http:
            return await http.get("/api")

    healthy = asyncio.run(get(create_app(Config())))
    assert (healthy.status_code, healthy.json()) == (200, {"healthy": True})
    down = asyncio.run(get(create_app(Config(failures=[Failure(operation="health", status=503)]))))
    assert down.status_code == 503


def test_payment_updates_need_a_signed_invoice() -> None:
    async def unsigned(nrs: NrsClient) -> None:
        await nrs.update_payment(IRN, "PAID", "REF-1")

    with pytest.raises(NrsRejectedError, match="not found"):
        run(unsigned)

    async def paid(nrs: NrsClient) -> Any:
        await nrs.sign(to_nrs_json(INVOICE))
        await nrs.update_payment(IRN, "PARTIAL", "REF-1", amount="500.00")
        with pytest.raises(NrsRejectedError, match="amount is required"):
            await nrs.update_payment(IRN, "PARTIAL", "REF-2")
        return await nrs.update_payment(IRN, "PAID", "REF-3")

    assert run(paid) == {"irn": IRN, "payment_status": "PAID"}
