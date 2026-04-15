"""One-time Shopify OAuth helper — obtain a non-expiring offline access token.

Usage:
    1. Create an app in the Shopify Dev Dashboard (https://dev.shopify.com).
    2. Set the redirect URI to: http://localhost:8976/callback
    3. Add the required Admin API scopes (read_products, read_orders, etc.).
    4. Copy your Client ID and Client Secret.
    5. Run:
           python shopify_oauth.py \
               --shop YOUR_STORE.myshopify.com \
               --client-id YOUR_CLIENT_ID \
               --client-secret YOUR_CLIENT_SECRET \
               --scopes read_products,read_orders

    The script opens a browser for you to authorise the app, then exchanges the
    code for a *non-expiring* offline access token and prints it.  Optionally it
    writes/updates SHOPIFY_ACCESS_TOKEN in your .env file (pass --save-env).
"""

from __future__ import annotations

import argparse
import hmac
import hashlib
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key

REDIRECT_PORT = 8976
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
ENV_PATH = Path(__file__).resolve().parent / ".env"


# ── Tiny HTTP server to capture the OAuth callback ───────────────────────

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Handles a single GET /callback request from Shopify."""

    auth_code: str | None = None
    received_state: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)

        _OAuthCallbackHandler.auth_code = params.get("code", [None])[0]
        _OAuthCallbackHandler.received_state = params.get("state", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Done! You can close this tab.</h2></body></html>"
        )

    def log_message(self, *_args: object) -> None:  # silence logs
        pass


def _wait_for_callback() -> tuple[str, str]:
    """Start a local server, wait for one request, return (code, state)."""
    server = http.server.HTTPServer(("localhost", REDIRECT_PORT), _OAuthCallbackHandler)
    server.handle_request()  # blocks until the browser redirects back
    server.server_close()

    code = _OAuthCallbackHandler.auth_code
    state = _OAuthCallbackHandler.received_state
    if not code:
        print("ERROR: No authorization code received.", file=sys.stderr)
        sys.exit(1)
    return code, state or ""


# ── HMAC verification (optional but recommended) ─────────────────────────

def _verify_hmac(query_string: str, client_secret: str) -> bool:
    """Verify the HMAC Shopify appends to the redirect URL."""
    params = urllib.parse.parse_qs(query_string)
    hmac_value = params.pop("hmac", [None])[0]
    if not hmac_value:
        return False

    sorted_params = "&".join(
        f"{k}={v[0]}" for k, v in sorted(params.items())
    )
    digest = hmac.new(
        client_secret.encode(), sorted_params.encode(), hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(digest, hmac_value)


# ── Token exchange ───────────────────────────────────────────────────────

def exchange_code_for_token(
    shop: str, client_id: str, client_secret: str, code: str
) -> dict:
    """POST to Shopify to exchange the authorization code for an access token."""
    url = f"https://{shop}/admin/oauth/access_token"
    resp = requests.post(
        url,
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obtain a non-expiring Shopify offline access token via OAuth."
    )
    parser.add_argument("--shop", required=True, help="e.g. your-store.myshopify.com")
    parser.add_argument("--client-id", required=True, help="App Client ID from Dev Dashboard")
    parser.add_argument("--client-secret", required=True, help="App Client Secret")
    parser.add_argument(
        "--scopes",
        default="read_products,read_orders",
        help="Comma-separated API scopes (default: read_products,read_orders)",
    )
    parser.add_argument(
        "--save-env",
        action="store_true",
        help="Write/update SHOPIFY_ACCESS_TOKEN in .env",
    )
    args = parser.parse_args()

    state = secrets.token_urlsafe(16)

    # Build the authorization URL
    auth_params = urllib.parse.urlencode({
        "client_id": args.client_id,
        "scope": args.scopes,
        "redirect_uri": REDIRECT_URI,
        "state": state,
    })
    auth_url = f"https://{args.shop}/admin/oauth/authorize?{auth_params}"

    print(f"\nOpening browser for Shopify authorization …")
    print(f"  If it doesn't open, visit:\n  {auth_url}\n")

    # Open browser in a thread so we don't block the server
    threading.Timer(0.5, webbrowser.open, args=(auth_url,)).start()

    # Wait for the redirect
    code, received_state = _wait_for_callback()

    if received_state != state:
        print("WARNING: State mismatch — potential CSRF. Proceeding anyway for local use.", file=sys.stderr)

    # Exchange code for token (non-expiring by default)
    print("Exchanging authorization code for access token …")
    token_data = exchange_code_for_token(args.shop, args.client_id, args.client_secret, code)

    access_token = token_data.get("access_token", "")
    scope = token_data.get("scope", "")

    print(f"\n{'=' * 60}")
    print(f"  Access token : {access_token}")
    print(f"  Scopes       : {scope}")

    if "expires_in" in token_data:
        print(f"  Expires in   : {token_data['expires_in']}s  (use --scopes + expiring=0 if you want non-expiring)")
    else:
        print(f"  Expires      : never (offline, non-expiring)")

    print(f"{'=' * 60}\n")

    if args.save_env:
        if not ENV_PATH.exists():
            ENV_PATH.touch()
        set_key(str(ENV_PATH), "SHOPIFY_ACCESS_TOKEN", access_token)
        set_key(str(ENV_PATH), "SHOPIFY_SHOP_DOMAIN", args.shop)
        print(f"Saved to {ENV_PATH}")
    else:
        print("Tip: run with --save-env to write directly to .env")


if __name__ == "__main__":
    main()
