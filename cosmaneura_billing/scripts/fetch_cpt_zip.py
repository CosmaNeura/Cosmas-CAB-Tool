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
  * The exact Releases/Files paths below are the documented endpoints, but
    confirm the precise path + params against your portal's API definition
    ("Try Out" / Swagger) and override with --releases-path / --file-path.
  * NEVER commit the key/secret. Read them from env or a secret manager.

This is a template to run in YOUR environment against YOUR licensed credentials;
it is not executed or tested here (no network/credentials in the build sandbox).
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REQUIRED = ["AMA_TOKEN_URL", "AMA_BASE_URL", "AMA_CONSUMER_KEY", "AMA_CONSUMER_SECRET"]


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
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _auth_help(e.read().decode(errors="ignore")[:300])
        raise SystemExit(f"token request failed: HTTP {e.code}")
    tok = payload.get("access_token")
    if not tok:
        raise SystemExit(f"no access_token in token response: {payload}")
    print(f"got access token (expires in {payload.get('expires_in', '?')}s)", file=sys.stderr)
    return tok


def _get(url: str, token: str, binary: bool = False):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return body if binary else json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            _auth_help(e.read().decode(errors="ignore")[:300])
        raise SystemExit(f"GET {url} failed: HTTP {e.code}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="cpt_standard_data_file.zip", help="where to save the ZIP")
    ap.add_argument("--release", help="release id to download (default: newest from Releases)")
    ap.add_argument("--releases-path", default="/cpt-zip/1.0.0/releases")
    ap.add_argument("--file-path", default="/cpt-zip/1.0.0/files/{release}",
                    help="template for the Files endpoint; {release} is substituted")
    args = ap.parse_args()

    missing = [v for v in REQUIRED if not os.environ.get(v)]
    if missing:
        print(__doc__)
        print(f"Set these environment variables first: {', '.join(missing)}", file=sys.stderr)
        return 2

    base = os.environ["AMA_BASE_URL"].rstrip("/")
    token = get_token(os.environ["AMA_TOKEN_URL"],
                      os.environ["AMA_CONSUMER_KEY"], os.environ["AMA_CONSUMER_SECRET"])

    releases = _get(base + args.releases_path, token)
    print("Releases endpoint OK. Available releases:", file=sys.stderr)
    print(json.dumps(releases, indent=2)[:1000], file=sys.stderr)

    release = args.release
    if not release:
        # best-effort: pull an id/name from the releases payload; adjust to the real shape
        items = releases if isinstance(releases, list) else releases.get("releases") or releases.get("data") or []
        if items:
            first = items[0]
            release = first if isinstance(first, str) else (first.get("id") or first.get("release") or first.get("name"))
    if not release:
        raise SystemExit("Could not determine a release id; pass --release after inspecting the Releases output.")

    blob = _get(base + args.file_path.format(release=urllib.parse.quote(str(release))), token, binary=True)
    with open(args.out, "wb") as fh:
        fh.write(blob)
    print(f"downloaded release '{release}' -> {args.out} ({len(blob):,} bytes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
