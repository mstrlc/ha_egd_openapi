"""Unit tests for EG.D API client helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import pairwise
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("homeassistant")

from custom_components.ha_egd_openapi import api
from custom_components.ha_egd_openapi.api import EgdApiClient, IntervalRecord


def test_profile_to_parameter_uses_exclusive_interval_boundary() -> None:
    """Profile requests should include the requested final quarter-hour."""
    assert (
        api._format_egd_profile_to(  # noqa: SLF001
            datetime(2026, 5, 20, 21, 45, tzinfo=timezone.utc)
        )
        == "2026-05-20T21:59:59.000Z"
    )


def test_profile_to_parameter_for_final_slot_stays_within_yesterday() -> None:
    """The newest day's 23:45 slot must not need a `to` at local midnight."""
    prague = ZoneInfo("Europe/Prague")
    for last_slot in (
        datetime(2026, 9, 23, 23, 45, tzinfo=prague),  # CEST
        datetime(2026, 12, 23, 23, 45, tzinfo=prague),  # CET
    ):
        to_param = api._format_egd_profile_to(last_slot)  # noqa: SLF001
        to_local = datetime.fromisoformat(to_param.replace("Z", "+00:00")).astimezone(prague)
        assert to_local.date() == last_slot.date()
        assert to_local > last_slot


class _RecordingClient(EgdApiClient):
    """API client test double recording requested chunks."""

    def __init__(self) -> None:
        super().__init__(MagicMock(), "fake-client-id", "fake-client-secret")
        self.chunks: list[tuple[datetime, datetime, int]] = []

    async def _async_get_profile_data_chunk(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = api.DEFAULT_PAGE_SIZE,
    ) -> list[IntervalRecord]:
        self.chunks.append((from_dt, to_dt, page_size))
        return []


class _ResponseShapeClient(EgdApiClient):
    """API client test double returning one raw response payload."""

    def __init__(self, payload) -> None:
        super().__init__(MagicMock(), "fake-client-id", "fake-client-secret")
        self.payload = payload

    async def _async_request_profile_data(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
        page_start: int,
        page_size: int,
    ):
        return 200, self.payload


@pytest.mark.asyncio
async def test_profile_data_fetch_splits_initial_history_into_page_sized_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial history fetch should avoid large ranges that depend on paging."""
    from_dt = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    to_dt = datetime(2026, 4, 15, 23, 45, tzinfo=timezone.utc)
    client = _RecordingClient()
    monkeypatch.setattr(api, "get_history_start", lambda: from_dt - timedelta(days=1))

    await client.async_get_profile_data(
        ean="859182400000000000",
        profile="ICQ2",
        from_dt=from_dt,
        to_dt=to_dt,
    )

    assert len(client.chunks) == 4
    assert client.chunks[0] == (
        datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 1, 31, 23, 45, tzinfo=timezone.utc),
        api.DEFAULT_PAGE_SIZE,
    )
    assert client.chunks[-1] == (
        datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 4, 15, 23, 45, tzinfo=timezone.utc),
        api.DEFAULT_PAGE_SIZE,
    )
    assert all(
        chunk_to - chunk_from <= api.MAX_PROFILE_CHUNK
        for chunk_from, chunk_to, _page_size in client.chunks
    )


@pytest.mark.asyncio
async def test_profile_data_chunk_accepts_object_payload() -> None:
    """EG.D may return the profile payload as an object instead of a list."""
    client = _ResponseShapeClient(
        {
            "ean/eic": "859182400000000000",
            "profile": "ICQ2",
            "units": "kWh",
            "total": 1,
            "data": [
                {
                    "timestamp": "2026-05-08T22:15:00Z",
                    "value": 1.25,
                    "status": "W",
                }
            ],
        }
    )

    records = await client._async_get_profile_data_chunk(  # noqa: SLF001
        ean="859182400000000000",
        profile="ICQ2",
        from_dt=datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc),
        to_dt=datetime(2026, 5, 8, 23, 45, tzinfo=timezone.utc),
    )

    assert records == [
        IntervalRecord(
            timestamp=datetime(2026, 5, 8, 22, 15, tzinfo=timezone.utc),
            value=1.25,
            status="W",
        )
    ]


@pytest.mark.parametrize(
    "profile,units",
    [
        ("DCQC", "kWh"),
        ("DSQC", "kWh"),
        ("ICQ2", "KWH"),
        ("ISQ2", "kWh"),
        ("ICC1", "KW"),
        ("ISC1", "kW"),
    ],
)
@pytest.mark.asyncio
async def test_profile_units_and_nullable_missing_values(profile, units) -> None:
    """Keep interval values unchanged and allow missing readings without energy."""
    client = _ResponseShapeClient(
        {
            "profile": profile,
            "units": units,
            "total": 2,
            "data": [
                {"timestamp": "2026-08-01T00:00:00Z", "value": 0.07, "status": "W"},
                {"timestamp": "2026-08-01T00:15:00Z", "value": None, "status": "F"},
            ],
        }
    )
    records = await _fetch_chunk(client, profile=profile)
    assert [record.value for record in records] == [0.07, None]
    assert [record.status for record in records] == ["W", "F"]


@pytest.mark.parametrize(
    "payload",
    [
        {"profile": "DSQC", "units": "kWh", "data": [], "total": 0},
        {"profile": "DCQC", "units": "kW", "data": [], "total": 0},
    ],
)
@pytest.mark.asyncio
async def test_mismatched_profile_or_units_fail_explicitly(payload) -> None:
    """Never checkpoint data for the wrong profile or unit."""
    with pytest.raises(api.EgdApiError):
        await _fetch_chunk(_ResponseShapeClient(payload), profile="DCQC")


@pytest.mark.parametrize(
    "payload",
    [
        {"data": None},
        {"data": [], "total": "invalid"},
        "not a profile response",
        [{"data": []}, {"data": []}],
    ],
)
@pytest.mark.asyncio
async def test_invalid_page_payload_fails_explicitly(payload) -> None:
    with pytest.raises(api.EgdApiError):
        await _fetch_chunk(_ResponseShapeClient(payload))


@pytest.mark.parametrize("count", [0, 1, 2993, 3000, 6001])
@pytest.mark.parametrize("server_page_size", [1000, 3000])
@pytest.mark.asyncio
async def test_pagination_fetches_all_reported_intervals(
    count, server_page_size
) -> None:
    """Follow total even if the server returns fewer rows than requested."""
    items = _interval_payloads(count)

    async def respond(**kwargs):
        offset = kwargs["page_start"] - 1
        return 200, {"total": count, "data": items[offset : offset + server_page_size]}

    client = _build_client(respond)
    records = await _fetch_chunk(client)
    assert len(records) == count
    assert [r.timestamp.isoformat() for r in records] == [r["timestamp"] for r in items]
    assert [
        c.kwargs["page_start"]
        for c in client._async_request_profile_data.call_args_list
    ] == (list(range(1, count + 1, server_page_size)) or [1])
    assert all(
        c.kwargs["page_size"] == 3000
        for c in client._async_request_profile_data.call_args_list
    )


@pytest.mark.parametrize(
    "second_page",
    [
        [],
        {"total": 3, "data": []},
        {
            "total": 3,
            "data": [
                {"timestamp": "2026-08-01T00:00:00+00:00", "value": 0.07, "status": "W"}
            ],
        },
        {"total": 4, "data": []},
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_repeated_pages_fail(second_page) -> None:
    """A truncated or repeated page must not look like a completed import."""
    client = _build_client(
        [(200, {"total": 3, "data": _interval_payloads(2)}), (200, second_page)]
    )
    with pytest.raises(api.EgdApiError):
        await _fetch_chunk(client)
    assert client._async_request_profile_data.call_count == 2


@pytest.mark.asyncio
async def test_pagination_without_total_uses_page_length() -> None:
    """Legacy payloads without total still support full and final short pages."""
    items = _interval_payloads(3)
    client = _build_client([(200, {"data": items[:2]}), (200, {"data": items[2:]})])
    records = await _fetch_chunk(client, page_size=2)
    assert len(records) == 3
    assert client._async_request_profile_data.call_args.kwargs["page_start"] == 3


@pytest.mark.asyncio
async def test_history_and_revalidation_preserve_every_interval(
    monkeypatch,
) -> None:
    """Long queries stay below 3000 rows with no gaps or overlaps at chunk edges."""
    profile = "ICQ2"
    days = 62
    start = datetime(2025, 9, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=days, minutes=-15)
    monkeypatch.setattr(api, "get_history_start", lambda: start)

    async def respond(**kwargs):
        chunk_from, chunk_to = kwargs["from_dt"], kwargs["to_dt"]
        count = (chunk_to - chunk_from) // timedelta(minutes=15) + 1
        assert count <= api.DEFAULT_PAGE_SIZE
        assert kwargs["page_start"] == 1
        assert kwargs["profile"] == profile
        return 200, {
            "profile": profile,
            "units": "kWh",
            "total": count,
            "data": _interval_payloads(count, start=chunk_from),
        }

    client = _build_client(respond)
    records = await client.async_get_profile_data(
        ean="859182400000000000",
        profile=profile,
        from_dt=start,
        to_dt=end,
    )
    assert len(records) == days * 96
    assert records[0].timestamp == start
    assert records[-1].timestamp == end
    assert all(
        b.timestamp - a.timestamp == timedelta(minutes=15) for a, b in pairwise(records)
    )
    assert sum(r.value for r in records) == pytest.approx(days * 96 * 0.07)


@pytest.mark.asyncio
async def test_access_probe_reads_only_one_page() -> None:
    """Authorization probing should not download a whole day one record at a time."""
    client = _build_client([(200, {"total": 96, "data": _interval_payloads(1)})])
    assert await client.async_probe_access(
        ean="859182400000000000",
        profile="ICQ2",
        from_dt=datetime(2026, 8, 1, tzinfo=timezone.utc),
        to_dt=datetime(2026, 8, 1, 23, 45, tzinfo=timezone.utc),
    )
    client._async_request_profile_data.assert_awaited_once()
    assert client._async_request_profile_data.call_args.kwargs["page_size"] == 1


async def _fetch_chunk(client, *, profile="ICQ2", page_size=3000):
    return await client._async_get_profile_data_chunk(
        ean="859182400000000000",
        profile=profile,
        from_dt=datetime(2026, 8, 1, tzinfo=timezone.utc),
        to_dt=datetime(2026, 10, 31, 23, 45, tzinfo=timezone.utc),
        page_size=page_size,
    )


def _build_client(responses):
    client = EgdApiClient(MagicMock(), "fake-id", "fake-secret")
    client._async_request_profile_data = AsyncMock(side_effect=responses)
    return client


def _interval_payloads(count, *, start=datetime(2026, 8, 1, tzinfo=timezone.utc)):
    return [
        {
            "timestamp": (start + timedelta(minutes=i * 15)).isoformat(),
            "value": 0.07,
            "status": "W",
        }
        for i in range(count)
    ]
