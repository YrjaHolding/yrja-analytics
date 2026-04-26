"""Gordon Delivery REST API client.

Per the Gordon docs (``developer.gordondelivery.com/reference/authentication``):

    "The Endpoint URL and the API key you use to authenticate the request
     determines whether the request is live mode or test mode."

So *test mode* means:
  * Base URL: ``https://backend.staging.gordondelivery.com``
  * Client ID / Secret issued against a **staging** GLMP account
    (Account → API Credentials).

Production uses the same OAuth2 ``client_credentials`` flow against a different
base URL which Gordon hands out on request — set
``GORDON_PRODUCTION_BASE_URL`` in ``.env`` when you have it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

STAGING_BASE_URL = "https://backend.staging.gordondelivery.com"
# Gordon gives production hosts out on request — read from env when available.
DEFAULT_PRODUCTION_BASE_URL_ENV = "GORDON_PRODUCTION_BASE_URL"

# Refresh tokens this many seconds before their stated expiry
_TOKEN_REFRESH_SAFETY_SECONDS = 60


class GordonAuthError(RuntimeError):
    """OAuth2 token exchange failed."""


class GordonAPIError(RuntimeError):
    """Non-2xx response from a Gordon API call."""

    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"Gordon API error {status_code}: {body!r}")
        self.status_code = status_code
        self.body = body


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # monotonic seconds


class GordonClient:
    """Thin wrapper around the Gordon Delivery REST API."""

    def __init__(
        self,
        *,
        env: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        delivery_group: str | None = None,
        base_url: str | None = None,
    ) -> None:
        resolved_env = (env or os.environ.get("GORDON_ENV", "test")).lower()
        if resolved_env not in {"test", "production"}:
            raise ValueError(
                f"GORDON_ENV must be 'test' or 'production', got {resolved_env!r}"
            )
        self.env = resolved_env

        if base_url:
            self.base_url = base_url.rstrip("/")
        elif self.env == "test":
            self.base_url = STAGING_BASE_URL
        else:
            prod = os.environ.get(DEFAULT_PRODUCTION_BASE_URL_ENV, "").strip()
            if not prod:
                raise ValueError(
                    "Production Gordon base URL is not configured. Set "
                    f"{DEFAULT_PRODUCTION_BASE_URL_ENV} in .env (Gordon "
                    "provides this on request)."
                )
            self.base_url = prod.rstrip("/")

        self.client_id = client_id or os.environ.get("GORDON_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("GORDON_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Gordon credentials required. Set GORDON_CLIENT_ID and "
                "GORDON_CLIENT_SECRET in .env or pass as arguments."
            )

        self.delivery_group = delivery_group or os.environ.get(
            "GORDON_DELIVERY_GROUP"
        )

        self._http = httpx.Client(timeout=30.0)
        self._token: _CachedToken | None = None

    # ── Authentication ────────────────────────────────────────────────

    def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and self._token.expires_at - _TOKEN_REFRESH_SAFETY_SECONDS > now:
            return self._token.token

        url = f"{self.base_url}/oauth/token"
        log.info("Fetching Gordon OAuth token from %s", url)
        resp = self._http.post(
            url,
            json={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if resp.status_code >= 400:
            hint = ""
            if resp.status_code == 401:
                hint = (
                    f"\n\nHint: a 401 at /oauth/token usually means"
                    f"\n  1. GORDON_CLIENT_ID / GORDON_CLIENT_SECRET is wrong or"
                    f" was rotated in GLMP (Account → API Credentials) and .env"
                    f" is out of date."
                    f"\n  2. The credentials belong to a different tenant than"
                    f" the env '{self.env}' you're hitting ({self.base_url})."
                    f" Staging and production have separate GLMP accounts and"
                    f" separate credentials."
                    f"\n  3. The GLMP user that generated the credentials lost"
                    f" Admin permission, or API access is not enabled on the"
                    f" account."
                )
            raise GordonAuthError(
                f"OAuth token request failed ({resp.status_code}): {resp.text}{hint}"
            )
        body = resp.json()
        token = body.get("access_token") or body.get("token") or body.get("jwt")
        expires_in = int(body.get("expires_in", 3600))
        if not token:
            raise GordonAuthError(f"OAuth response missing access_token: {body!r}")
        self._token = _CachedToken(token=token, expires_at=now + expires_in)
        return token

    # ── Request plumbing ──────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        merged_params: dict[str, Any] = {}
        if self.delivery_group:
            merged_params["deliverygroup"] = self.delivery_group
        if params:
            merged_params.update(params)

        url = f"{self.base_url}{path}"
        token = self._get_token()
        resp = self._http.request(
            method,
            url,
            json=json,
            params=merged_params or None,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        if resp.status_code == 401:
            # Token might have been invalidated early; force refresh and retry once.
            log.warning("Gordon returned 401 — refreshing token and retrying once")
            self._token = None
            token = self._get_token()
            resp = self._http.request(
                method,
                url,
                json=json,
                params=merged_params or None,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )

        if resp.status_code >= 400:
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            raise GordonAPIError(resp.status_code, body)

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ── Public API ────────────────────────────────────────────────────

    def test_auth(self) -> dict[str, Any]:
        """Exchange credentials for a token without making any order calls.

        Returns a small dict describing the resolved base URL, the (prefix of
        the) token, and how long it's valid — useful for confirming test-mode
        credentials from the CLI without touching orders.
        """
        token = self._get_token()
        assert self._token is not None
        remaining = max(0, int(self._token.expires_at - time.monotonic()))
        return {
            "env": self.env,
            "base_url": self.base_url,
            "token_prefix": token[:12] + "…",
            "expires_in_seconds": remaining,
            "delivery_group": self.delivery_group,
        }

    def create_orders_bulk(self, orders: list[dict[str, Any]]) -> Any:
        """POST /api/orders/bulk — create many orders in one request.

        Gordon enforces a bulk request limit; callers should chunk as needed.
        """
        if not orders:
            return []
        return self._request("POST", "/api/orders/bulk", json=orders)

    def create_order(self, order: dict[str, Any]) -> Any:
        """POST /api/orders — create a single order."""
        return self._request("POST", "/api/orders", json=order)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GordonClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def has_gordon_credentials() -> bool:
    """Check whether the env has at least a Client ID + Secret configured."""
    return bool(
        os.environ.get("GORDON_CLIENT_ID")
        and os.environ.get("GORDON_CLIENT_SECRET")
    )
