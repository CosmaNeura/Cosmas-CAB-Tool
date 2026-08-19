"""CPT descriptor ingestion — for practices holding an AMA CPT license.

CPT codes are referenced throughout this engine by number with short generic
functional labels, because the official descriptors are AMA-licensed content.
A practice that licenses CPT (via the AMA Intelligent Platform / CPT
Distribution Program) receives the *CPT Standard Data File*; this module
ingests the descriptor files from that distribution so the UI and worksheet
can show official short descriptors instead of generic labels.

NOTHING in this module ships descriptor content, and descriptor files must
never be committed to the repository. The module only reads (or fetches)
files under the practice's own license, in deployments covered by it.

How descriptors are found, in order:
  1. an explicit ``path`` argument;
  2. ``$CPT_DATA_DIR`` — a directory with the extracted Standard Data File;
  3. a previously auto-fetched copy in the local cache;
  4. **auto-fetch**: when the four ``AMA_*`` credentials are present in the
     environment (AMA_TOKEN_URL, AMA_BASE_URL, AMA_CONSUMER_KEY,
     AMA_CONSUMER_SECRET), the module mints an OAuth2 client-credentials
     token, downloads the newest release from the CPT Zip API, and extracts
     it into the cache — so any environment carrying the credentials gets
     official descriptors with no manual step. Set ``CPT_AUTO_FETCH=0`` to
     disable. Failures are non-fatal (``loaded`` stays False and
     ``fetch_error`` says why).

Accepted file layouts (auto-detected): classic Standard Data File text
(fixed "CODE<ws>DESCRIPTOR" lines, SHORTU/MEDU/LONGULT style) and tab- or
comma-delimited exports with the code first and the descriptor last.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ..settings import Settings, SettingsError

# preferred filenames inside a Standard Data File distribution, best first
_CANDIDATES = ("SHORTU.txt", "SHORTU.TXT", "shortu.txt",
               "MEDU.txt", "MEDU.TXT", "LONGULT.txt", "LONGUF.txt")
_CODE_RE = re.compile(r"^\s*([0-9]{4}[0-9A-Z])\s+(.+?)\s*$")

# The AMA gateway's WAF rejects requests without an explicit User-Agent
# (Python-urllib's default gets 403), so every request sends one.
_UA = "cosmaneura-billing/0.1"


def _find_descriptor_file(d: Path) -> Path | None:
    for name in _CANDIDATES:
        hits = sorted(d.rglob(name))
        if hits:
            return hits[0]
    hits = sorted(d.rglob("*.txt")) + sorted(d.rglob("*.csv"))
    return hits[0] if hits else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _urlopen(req: urllib.request.Request, timeout: int):
    """One HTTP round trip that does NOT auto-follow redirects (the file
    endpoint 303s to pre-signed storage, where forwarding the Authorization
    header would be rejected — and would leak the token). Module-level seam
    so tests can fake HTTP."""
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


class CPTDescriptors:
    def __init__(self, path: str | os.PathLike | None = None,
                 cache_dir: str | os.PathLike | None = None,
                 settings: Settings | None = None):
        self._settings = settings or Settings.from_env()
        self._desc: dict[str, str] = {}
        self._source: str | None = None
        self.fetch_error: str | None = None
        self._cache = Path(cache_dir or self._settings.cpt_cache_dir)
        resolved = self._resolve(path)
        if resolved is not None:
            self._load(resolved)

    def _resolve(self, path: str | os.PathLike | None) -> Path | None:
        if path:
            return Path(path)
        data_dir = self._settings.cpt_data_dir
        if data_dir and Path(data_dir).is_dir():
            found = _find_descriptor_file(Path(data_dir))
            if found:
                return found
        if self._cache.is_dir():
            found = _find_descriptor_file(self._cache)
            if found:
                return found
        if self._settings.cpt_auto_fetch and self._settings.ama_configured:
            return self._auto_fetch()
        return None

    # ---- auto-fetch (AMA CPT Zip API, client-credentials OAuth2) --------- #
    def _auto_fetch(self) -> Path | None:
        try:
            token = self._token()
            credentials = self._settings.ama_credentials
            if credentials is None:
                raise SettingsError("AMA CPT credentials are not configured")
            base = credentials.base_url.rstrip("/")
            releases_path = self._settings.ama_releases_path
            releases = self._get_json(base + releases_path, token)
            release = self._pick_release(releases)
            if not release:
                self.fetch_error = "could not determine a release id from the AMA response"
                return None
            file_path = self._settings.ama_file_path
            blob = self._get_bytes(
                base + file_path.format(release=urllib.parse.quote(str(release))), token)
            self._cache.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                # descriptors live in the .txt data files; skip the PDF/DOCX bulk
                for m in zf.namelist():
                    if m.lower().endswith((".txt", ".csv")):
                        zf.extract(m, self._cache)
            return _find_descriptor_file(self._cache)
        except Exception as e:  # never break the pipeline over descriptors
            self.fetch_error = f"{type(e).__name__}: {e}"
            return None

    def _token(self) -> str:
        credentials = self._settings.ama_credentials
        if credentials is None:
            raise SettingsError("AMA CPT credentials are not configured")
        basic = base64.b64encode(
            f"{credentials.consumer_key}:{credentials.consumer_secret}".encode()
        ).decode()
        req = urllib.request.Request(
            credentials.token_url,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            method="POST",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": _UA})
        with _urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
        tok = payload.get("access_token")
        if not tok:
            raise ValueError("AMA token response did not include access_token")
        return tok

    @staticmethod
    def _get_json(url: str, token: str):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
        with _urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    @staticmethod
    def _get_bytes(url: str, token: str) -> bytes:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "User-Agent": _UA})
        try:
            with _urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location")
                if not loc:
                    raise
                # pre-signed URL: authenticate by URL only — never forward the token
                req2 = urllib.request.Request(loc, headers={"User-Agent": _UA})
                with _urlopen(req2, timeout=300) as r:
                    return r.read()
            raise

    @staticmethod
    def _pick_release(releases) -> str | None:
        items = releases if isinstance(releases, list) else \
            (releases.get("releases") or releases.get("data") or []) \
            if isinstance(releases, dict) else []
        if not items:
            return None
        first = items[0]
        if isinstance(first, str):
            return first
        return first.get("id") or first.get("release") or first.get("name")

    # ---- parsing --------------------------------------------------------- #
    def _load(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if "\t" in first_line or path.suffix.lower() == ".csv":
            delim = "\t" if "\t" in first_line else ","
            for row in csv.reader(io.StringIO(text), delimiter=delim):
                if len(row) >= 2 and row[0].strip():
                    self._desc[row[0].strip().upper()] = row[-1].strip()
        else:
            for line in text.splitlines():
                m = _CODE_RE.match(line)
                if m:
                    self._desc[m.group(1)] = m.group(2)
        self._source = str(path)

    # ---- lookups -------------------------------------------------------- #
    def get(self, code: str) -> str | None:
        return self._desc.get(str(code).strip().upper())

    def relabel(self, suggestions) -> int:
        """Upgrade generic labels to official descriptors in place (E/M and
        procedure codes only). Returns how many labels were upgraded."""
        if not self.loaded:
            return 0
        n = 0
        for s in suggestions:
            official = self.get(s.code)
            if official and official != s.label:
                s.label = official
                n += 1
        return n

    @property
    def loaded(self) -> bool:
        return bool(self._desc)

    @property
    def size(self) -> int:
        return len(self._desc)

    @property
    def source(self) -> str | None:
        return self._source
