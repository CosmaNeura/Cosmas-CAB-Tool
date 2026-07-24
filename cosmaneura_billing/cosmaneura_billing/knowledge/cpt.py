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

# preferred filenames inside a Standard Data File distribution, best first
_CANDIDATES = ("SHORTU.txt", "SHORTU.TXT", "shortu.txt",
               "MEDU.txt", "MEDU.TXT", "LONGULT.txt", "LONGUF.txt")
_CODE_RE = re.compile(r"^\s*([0-9]{4}[0-9A-Z])\s+(.+?)\s*$")

_AMA_VARS = ("AMA_TOKEN_URL", "AMA_BASE_URL", "AMA_CONSUMER_KEY", "AMA_CONSUMER_SECRET")
_DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) \
    / "cosmaneura" / "cpt"


def _find_descriptor_file(d: Path) -> Path | None:
    for name in _CANDIDATES:
        if (d / name).exists():
            return d / name
    hits = sorted(d.rglob("*.txt")) + sorted(d.rglob("*.csv"))
    return hits[0] if hits else None


class CPTDescriptors:
    def __init__(self, path: str | os.PathLike | None = None,
                 cache_dir: str | os.PathLike | None = None):
        self._desc: dict[str, str] = {}
        self._source: str | None = None
        self.fetch_error: str | None = None
        self._cache = Path(cache_dir or _DEFAULT_CACHE)
        resolved = self._resolve(path)
        if resolved is not None:
            self._load(resolved)

    def _resolve(self, path: str | os.PathLike | None) -> Path | None:
        if path:
            return Path(path)
        env_dir = os.environ.get("CPT_DATA_DIR")
        if env_dir and Path(env_dir).is_dir():
            found = _find_descriptor_file(Path(env_dir))
            if found:
                return found
        if self._cache.is_dir():
            found = _find_descriptor_file(self._cache)
            if found:
                return found
        if os.environ.get("CPT_AUTO_FETCH", "1") != "0" \
                and all(os.environ.get(v) for v in _AMA_VARS):
            return self._auto_fetch()
        return None

    # ---- auto-fetch (AMA CPT Zip API, client-credentials OAuth2) --------- #
    def _auto_fetch(self) -> Path | None:
        try:
            token = self._token()
            base = os.environ["AMA_BASE_URL"].rstrip("/")
            releases_path = os.environ.get("AMA_RELEASES_PATH", "/cpt-zip/1.0.0/releases")
            releases = self._get_json(base + releases_path, token)
            release = self._pick_release(releases)
            if not release:
                self.fetch_error = f"could not determine a release id from {str(releases)[:200]}"
                return None
            file_path = os.environ.get("AMA_FILE_PATH", "/cpt-zip/1.0.0/files/{release}")
            blob = self._get_bytes(
                base + file_path.format(release=urllib.parse.quote(str(release))), token)
            self._cache.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                zf.extractall(self._cache)
            return _find_descriptor_file(self._cache)
        except Exception as e:  # never break the pipeline over descriptors
            self.fetch_error = f"{type(e).__name__}: {e}"
            return None

    @staticmethod
    def _token() -> str:
        basic = base64.b64encode(
            f"{os.environ['AMA_CONSUMER_KEY']}:{os.environ['AMA_CONSUMER_SECRET']}".encode()
        ).decode()
        req = urllib.request.Request(
            os.environ["AMA_TOKEN_URL"],
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            method="POST",
            headers={"Authorization": f"Basic {basic}",
                     "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = json.loads(r.read())
        tok = payload.get("access_token")
        if not tok:
            raise ValueError(f"no access_token in token response: {str(payload)[:200]}")
        return tok

    @staticmethod
    def _get_json(url: str, token: str):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    @staticmethod
    def _get_bytes(url: str, token: str) -> bytes:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.read()

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
