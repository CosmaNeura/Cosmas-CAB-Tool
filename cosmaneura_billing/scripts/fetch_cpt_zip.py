#!/usr/bin/env python3
"""
Fetch the CPT Standard Data File from the AMA CPT Zip API (AMA Intelligent
Platform). This is the step that fixes the `900901 Invalid Credentials` error:
it exchanges your Consumer Key/Secret for an OAuth2 access token and sends it as
a Bearer header on every call.

The AMA gateway is WSO2-style, so this uses the OAuth2 *client_credentials* grant.
Fill in the four values from your Developer Portal application, then run:

    export AMA_TOKEN_URL='https://<gateway-host>/oauth2/token'   # from the portal (Keys tab)
    export AMA_BASE_URL='https://<gateway-host>'                 # gateway base, no trailing slash
    export AMA_CONSUMER_KEY='...'                                # Application Consumer Key
    export AMA_CONSUMER_SECRET='...'                             # Application Consumer Secret
    python scripts/fetch_cpt_zip.py --out cpt_standard_data_file.zip

Notes:
  * Use STAGING/sample credentials while debugging — Production is capped at 3
    users and ~10 calls/day across endpoints; sample-data Staging is unlimited.
    Run `--check` first: it costs 2 calls and stops before the download.
  * The default paths below were verified against the production gateway
    (July 2026, CPT 2026 Standard Update 4). If your portal's API definition
    ("Try Out" / Swagger) shows different paths, override with
    --releases-path / --file-path.
  * NEVER commit the key/secret. Read them from env or a secret manager.

Note: you rarely need this script — the engine itself auto-fetches at startup
when the same four AMA_* variables are set (see knowledge/cpt.py). Use this to
pre-download the ZIP for an offline/air-gapped deployment, or `--check` to
validate credentials without waiting for an engine boot.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# allow running as `python scripts/fetch_cpt_zip.py` from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cosmaneura_billing.settings import (
    AMA_CREDENTIAL_ENV_VARS, Settings, SettingsError,
)

REQUIRED = list(AMA_CREDENTIAL_ENV_VARS)

# The AMA gateway's WAF rejects Python-urllib's default User-Agent with 403.
_UA = "cosmaneura-billing/0.1"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _urlopen(req: urllib.request.Request, timeout: int):
    # No auto-redirect: the Files endpoint 303s to pre-signed storage, where a
    # forwarded Authorization header is both rejected and a token leak.
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _auth_help(detail: str = "") -> None:
    print("\nAuthentication failed (the 900901 family). Check, in order:", file=sys.stderr)
    print("  1. You're sending the Bearer ACCESS TOKEN (this script does) — not the raw key.", file=sys.stderr)
    print("  2. Your Application is SUBSCRIBED to the CPT Zip API in the portal.", file=sys.stderr)
    print("  3. The token isn't expired (this script mints a fresh one each run).", file=sys.stderr)
    print("  4. Key environment matches the endpoint (Staging key -> Staging URL; Prod -> Prod),", file=sys.stderr)
    print("     and you're one of the provisioned Production users (max 3).", file=sys.stderr)
    if detail:
        print(f"  server said: {detail}", file=sys.stderr)


def get_token(token_url: str, key: str, secret: str) -> str:
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        token_url, data=data, method="POST",
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": _UA})
    try:
        with _urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _auth_help(e.read().decode(errors="ignore")[:300])
        raise SystemExit(f"token request failed: HTTP {e.code}")
    tok = payload.get("access_token")
    if not tok:
        raise SystemExit("token response did not include access_token")
    print(f"got access token (expires in {payload.get('expires_in', '?')}s)", file=sys.stderr)
    return tok


def _get(url: str, token: str, binary: bool = False):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
    try:
        with _urlopen(req, timeout=300 if binary else 120) as r:
            body = r.read()
            return body if binary else json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            loc = e.headers.get("Location")
            if not loc:
                raise SystemExit(f"GET {url} redirected with no Location header")
            # pre-signed URL: authenticate by URL only — never forward the token
            req2 = urllib.request.Request(loc, headers={"User-Agent": _UA})
            with _urlopen(req2, timeout=300) as r:
                body = r.read()
                return body if binary else json.loads(body)
        if e.code in (401, 403):
            _auth_help(e.read().decode(errors="ignore")[:300])
        raise SystemExit(f"GET {url} failed: HTTP {e.code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cpt_standard_data_file.zip", help="where to save the ZIP")
    ap.add_argument("--release", help="release id to download (default: newest from Releases)")
    ap.add_argument("--releases-path")
    ap.add_argument("--file-path",
                    help="template for the Files endpoint; {release} is substituted "
                         "(?release= verified against the production gateway)")
    ap.add_argument("--check", action="store_true",
                    help="validate credentials + subscription and stop before downloading "
                         "(2 API calls — safe under the 10-call/day production cap)")
    args = ap.parse_args()

    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    credentials = settings.ama_credentials
    if credentials is None:
        print(__doc__)
        print(f"Set these environment variables first: {', '.join(REQUIRED)}", file=sys.stderr)
        return 2

    base = credentials.base_url.rstrip("/")
    token = get_token(
        credentials.token_url, credentials.consumer_key,
        credentials.consumer_secret,
    )

    releases_path = args.releases_path or settings.ama_releases_path
    file_path = args.file_path or settings.ama_file_path
    releases = _get(base + releases_path, token)
    print("Releases endpoint OK. Available releases:", file=sys.stderr)
    print(json.dumps(releases, indent=2)[:1000], file=sys.stderr)

    release = args.release
    if not release:
        # best-effort: pull an id/name from the releases payload; adjust to the real shape
        items = releases if isinstance(releases, list) else releases.get("releases") or releases.get("data") or []
        if items:
            first = items[0]
            release = first if isinstance(first, str) else (first.get("id") or first.get("release") or first.get("name"))

    if args.check:
        print(f"\nCheck PASSED: credentials valid, CPT Zip API subscription active, "
              f"newest release: {release or 'could not parse — inspect the payload above'}.",
              file=sys.stderr)
        print("The engine will auto-fetch on next start with these same AMA_* variables set.",
              file=sys.stderr)
        return 0

    if not release:
        raise SystemExit("Could not determine a release id; pass --release after inspecting the Releases output.")

    blob = _get(
        base + file_path.format(release=urllib.parse.quote(str(release))),
        token, binary=True,
    )
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(f"downloaded release '{release}' -> {args.out} ({len(blob):,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
