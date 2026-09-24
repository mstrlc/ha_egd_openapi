"""API client for EG.D OpenAPI."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from aiohttp import ClientError

from .const import DATA_URL, OAUTH_URL, PROFILE_UNITS

_LOGGER = logging.getLogger(__name__)


AUTHORIZATION_ERROR_FRAGMENT = "nemáte oprávnění na data odběrného místa"
VALIDATION_ERROR_FRAGMENT = "validation_error"
DEFAULT_PAGE_SIZE = 3000
MAX_PROFILE_CHUNK = timedelta(days=30, hours=23, minutes=45)
INTERVAL_LENGTH = timedelta(minutes=15)


def _format_egd_timestamp(value: datetime) -> str:
    """Format a timestamp for EG.D query parameters."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _format_egd_profile_to(value: datetime) -> str:
    """Format EG.D profile upper bound.

    The API treats the `to` parameter as an exclusive interval boundary, so
    `to_dt` itself is not returned. Point just before the next quarter-hour so
    callers can keep using `to_dt` as the last requested measurement timestamp.
    Stopping one second short matters for the newest day: its final 23:45 slot
    would otherwise need a `to` of local midnight, which EG.D rejects as today.
    """
    return _format_egd_timestamp(value + INTERVAL_LENGTH - timedelta(seconds=1))


def get_history_start(reference: datetime | None = None) -> datetime:
    """Return the first UTC quarter-hour inside the three-year safety margin."""
    ref = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        capped = ref.replace(year=ref.year - 3)
    except ValueError:
        capped = ref.replace(month=2, day=28, year=ref.year - 3)
    # Keep a full day inside the server's rolling retention boundary.
    cutoff = capped + timedelta(days=1)
    interval_start = cutoff.replace(
        minute=cutoff.minute // 15 * 15, second=0, microsecond=0
    )
    return interval_start + INTERVAL_LENGTH if interval_start < cutoff else interval_start


class EgdApiError(Exception):
    """Base API error."""


class EgdAuthError(EgdApiError):
    """Authentication error."""


class EgdValidationError(EgdApiError):
    """Validation error returned by EG.D."""

    def __init__(self, message: str, payload: Any | None = None) -> None:
        super().__init__(message)
        self.payload = payload

    @property
    def is_authorization_window_error(self) -> bool:
        payload_str = str(self.payload).lower()
        msg_str = str(self).lower()
        return AUTHORIZATION_ERROR_FRAGMENT in payload_str or AUTHORIZATION_ERROR_FRAGMENT in msg_str


@dataclass(slots=True)
class IntervalRecord:
    """One interval record from EG.D."""

    timestamp: datetime
    value: float | None
    status: str


class EgdApiClient:
    """Simple async client for EG.D OpenAPI."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        client_id: str,
        client_secret: str,
        *,
        diagnostic_logger: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self._session = session
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._diagnostic_logger = diagnostic_logger

    def set_diagnostic_logger(
        self,
        diagnostic_logger: Callable[[str, str, dict[str, Any] | None], None] | None,
    ) -> None:
        """Update diagnostic logger callback."""
        self._diagnostic_logger = diagnostic_logger

    def _log_diagnostic(
        self,
        level: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit a structured diagnostic event when collection is enabled."""
        if self._diagnostic_logger is None:
            return
        self._diagnostic_logger(level, message, details)

    async def _async_read_json_response(
        self,
        response: aiohttp.ClientResponse,
        *,
        context: str,
    ) -> Any:
        """Decode JSON response and surface non-JSON bodies clearly."""
        try:
            return await response.json(content_type=None)
        except aiohttp.ContentTypeError as err:
            body = (await response.text()).strip()
            snippet = body[:300]
            raise EgdApiError(
                f"{context} failed: HTTP {response.status}, non-JSON response"
                f" (content-type={response.headers.get('Content-Type')}, body={snippet!r})"
            ) from err
        except ValueError as err:
            body = (await response.text()).strip()
            snippet = body[:300]
            raise EgdApiError(
                f"{context} failed: HTTP {response.status}, invalid JSON response"
                f" (content-type={response.headers.get('Content-Type')}, body={snippet!r})"
            ) from err

    async def async_get_token(self) -> str:
        """Get bearer token."""
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "scope": "namerena_data_openapi",
        }
        _LOGGER.debug("Requesting EG.D OAuth token")
        self._log_diagnostic("debug", "token_request_started")
        try:
            async with self._session.post(OAUTH_URL, json=payload, timeout=30) as response:
                if response.status in (401, 403):
                    body = (await response.text()).strip()
                    self._log_diagnostic(
                        "error",
                        "token_request_auth_failed",
                        {"status": response.status, "body": body[:300]},
                    )
                    raise EgdAuthError(f"Authentication failed: HTTP {response.status}")
                data = await self._async_read_json_response(
                    response,
                    context="Token request",
                )
        except (TimeoutError, ClientError) as err:
            self._log_diagnostic(
                "error",
                "token_request_failed",
                {"reason": str(err)},
            )
            raise EgdApiError(f"Token request failed: {err}") from err

        if response.status >= 400:
            self._log_diagnostic(
                "error",
                "token_request_http_error",
                {"status": response.status},
            )
            raise EgdApiError(f"Token request failed: HTTP {response.status} {data}")

        token = data.get("access_token")
        if not token:
            self._log_diagnostic("error", "token_missing_access_token")
            raise EgdApiError("Token response does not contain access_token")

        self._access_token = token
        self._log_diagnostic("info", "token_request_succeeded")
        return token

    async def _async_request_profile_data(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
        page_start: int,
        page_size: int,
    ) -> tuple[int, Any]:
        """Request one page of profile data and refresh token once on auth failure."""
        params = {
            "ean": ean,
            "profile": profile,
            "from": _format_egd_timestamp(from_dt),
            "to": _format_egd_profile_to(to_dt),
            "pageStart": str(page_start),
            "pageSize": str(page_size),
        }
        _LOGGER.debug(
            "Requesting EG.D data for EAN %s profile %s from %s to %s (pageStart=%s pageSize=%s)",
            ean,
            profile,
            params["from"],
            params["to"],
            page_start,
            page_size,
        )
        self._log_diagnostic(
            "debug",
            "profile_request_started",
            {
                "ean_suffix": ean[-4:],
                "profile": profile,
                "from": params["from"],
                "to": params["to"],
                "page_start": page_start,
                "page_size": page_size,
            },
        )

        for attempt in range(2):
            token = self._access_token or await self.async_get_token()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                async with self._session.get(
                    DATA_URL, params=params, headers=headers, timeout=60
                ) as response:
                    if response.status in (401, 403):
                        body = (await response.text()).strip()
                        self._access_token = None
                        self._log_diagnostic(
                            "warning",
                            "profile_request_auth_retry",
                            {
                                "status": response.status,
                                "attempt": attempt + 1,
                                "body": body[:300],
                            },
                        )
                        if attempt == 0:
                            continue
                        raise EgdAuthError(f"Authentication failed: HTTP {response.status}")
                    data = await self._async_read_json_response(
                        response,
                        context="Data request",
                    )
            except (TimeoutError, ClientError) as err:
                self._log_diagnostic(
                    "error",
                    "profile_request_failed",
                    {"reason": str(err), "attempt": attempt + 1},
                )
                raise EgdApiError(f"Data request failed: {err}") from err
            self._log_diagnostic(
                "debug",
                "profile_request_finished",
                {"status": response.status, "attempt": attempt + 1},
            )
            return response.status, data

        raise EgdAuthError("Authentication failed after token refresh")

    async def async_probe_access(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> bool:
        """Return whether the given period is accessible for the EAN/profile.

        Uses a very small request to determine whether the server accepts the
        requested time window.
        """
        if from_dt.tzinfo is None or to_dt.tzinfo is None:
            raise ValueError("from_dt and to_dt must be timezone-aware")
        from_dt = max(from_dt.astimezone(timezone.utc), get_history_start())
        to_dt = to_dt.astimezone(timezone.utc)
        if from_dt > to_dt:
            return False

        try:
            await self._async_get_profile_data_chunk(
                ean=ean,
                profile=profile,
                from_dt=from_dt,
                to_dt=to_dt,
                page_size=1,
                first_page_only=True,
            )
        except EgdValidationError as err:
            if err.is_authorization_window_error:
                _LOGGER.debug(
                    "Probe denied for %s/%s in %s -> %s: %s",
                    ean,
                    profile,
                    from_dt.isoformat(),
                    to_dt.isoformat(),
                    err,
                )
                self._log_diagnostic(
                    "info",
                    "access_probe_denied",
                    {
                        "ean_suffix": ean[-4:],
                        "profile": profile,
                        "from": from_dt.isoformat(),
                        "to": to_dt.isoformat(),
                    },
                )
                return False
            raise
        self._log_diagnostic(
            "debug",
            "access_probe_succeeded",
            {
                "ean_suffix": ean[-4:],
                "profile": profile,
                "from": from_dt.isoformat(),
                "to": to_dt.isoformat(),
            },
        )
        return True

    async def async_get_profile_data(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
    ) -> list[IntervalRecord]:
        """Fetch all pages of profile data.

        EG.D pagination is not reliable for large cold-start windows. Keep each
        request below the default 3000-row page size so initial imports behave
        like the rolling revalidation window.
        """
        if from_dt.tzinfo is None or to_dt.tzinfo is None:
            raise ValueError("from_dt and to_dt must be timezone-aware")
        effective_from = max(from_dt.astimezone(timezone.utc), get_history_start())
        effective_to = to_dt.astimezone(timezone.utc)
        if effective_from > effective_to:
            _LOGGER.debug(
                "Skipping fetch for %s/%s because effective_from > effective_to after 3-year clamp",
                ean,
                profile,
            )
            self._log_diagnostic(
                "info",
                "profile_fetch_skipped_after_clamp",
                {
                    "ean_suffix": ean[-4:],
                    "profile": profile,
                    "effective_from": effective_from.isoformat(),
                    "effective_to": effective_to.isoformat(),
                },
            )
            return []

        all_records: list[IntervalRecord] = []
        chunk_start = effective_from
        final_to = effective_to
        while chunk_start <= final_to:
            chunk_end = min(chunk_start + MAX_PROFILE_CHUNK, final_to)
            chunk_records = await self._async_get_profile_data_chunk(
                ean=ean,
                profile=profile,
                from_dt=chunk_start,
                to_dt=chunk_end,
            )
            all_records.extend(chunk_records)
            self._log_diagnostic(
                "debug",
                "profile_chunk_loaded",
                {
                    "ean_suffix": ean[-4:],
                    "profile": profile,
                    "from": chunk_start.isoformat(),
                    "to": chunk_end.isoformat(),
                    "records": len(chunk_records),
                },
            )
            chunk_start = chunk_end + timedelta(minutes=15)

        all_records.sort(key=lambda rec: rec.timestamp)
        self._log_diagnostic(
            "info",
            "profile_fetch_completed",
            {
                "ean_suffix": ean[-4:],
                "profile": profile,
                "records": len(all_records),
            },
        )
        return all_records

    async def _async_get_profile_data_chunk(
        self,
        *,
        ean: str,
        profile: str,
        from_dt: datetime,
        to_dt: datetime,
        page_size: int = DEFAULT_PAGE_SIZE,
        first_page_only: bool = False,
    ) -> list[IntervalRecord]:
        """Fetch one API chunk including paging."""
        # In practice EG.D pagination behaves as 1-based even though the PDF
        # example shows PageStart=0. Using 0 can return an empty first page
        # without an explicit API error.
        page_start = 1
        records: list[IntervalRecord] = []
        seen_timestamps: set[datetime] = set()
        total: int | None = None

        while True:
            status, data = await self._async_request_profile_data(
                ean=ean,
                profile=profile,
                from_dt=from_dt,
                to_dt=to_dt,
                page_start=page_start,
                page_size=page_size,
            )

            if status >= 400:
                if status == 400:
                    raise EgdValidationError(
                        f"Data request failed: HTTP {status} {data}", payload=data
                    )
                raise EgdApiError(f"Data request failed: HTTP {status} {data}")
            if isinstance(data, dict):
                payloads = [data]
            elif isinstance(data, list):
                payloads = data
            else:
                raise EgdApiError("Unexpected profile response format")

            if not payloads:
                if total is not None and len(records) < total:
                    raise EgdApiError("Incomplete profile response: missing page")
                self._log_diagnostic(
                    "debug",
                    "profile_chunk_empty",
                    {
                        "ean_suffix": ean[-4:],
                        "profile": profile,
                        "page_start": page_start,
                    },
                )
                return records

            if len(payloads) != 1 or not isinstance(payloads[0], dict):
                raise EgdApiError("Unexpected profile response format")
            payload = payloads[0]
            if payload.get("profile", profile) != profile:
                raise EgdApiError("Unexpected profile in response")
            units = payload.get("units")
            if units is not None and str(units).casefold() != PROFILE_UNITS[profile].casefold():
                raise EgdApiError(f"Unexpected units for {profile}: {units}")
            batch = payload.get("data")
            if not isinstance(batch, list):
                raise EgdApiError("Profile response does not contain a data list")
            if "total" in payload:
                try:
                    page_total = int(payload["total"])
                except (TypeError, ValueError) as err:
                    raise EgdApiError("Invalid profile response total") from err
                if page_total < 0 or (total is not None and page_total != total):
                    raise EgdApiError("Profile response total changed during pagination")
                total = page_total
            if not batch and total is not None and len(records) < total:
                raise EgdApiError("Incomplete profile response: empty page before total")

            for item in batch:
                ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if ts in seen_timestamps:
                    raise EgdApiError("Duplicate interval in profile response")
                seen_timestamps.add(ts)
                value = item.get("value")
                records.append(
                    IntervalRecord(
                        timestamp=ts,
                        value=float(value) if value is not None else None,
                        status=str(item.get("status", "")),
                    )
                )

            if total is not None and len(records) > total:
                raise EgdApiError("Profile response exceeds reported total")
            if first_page_only or (total is not None and len(records) == total):
                break
            if total is None and len(batch) < page_size:
                break
            page_start += len(batch)

        self._log_diagnostic(
            "debug",
            "profile_chunk_paginated",
            {
                "ean_suffix": ean[-4:],
                "profile": profile,
                "records": len(records),
                "total": total,
            },
        )
        return records
