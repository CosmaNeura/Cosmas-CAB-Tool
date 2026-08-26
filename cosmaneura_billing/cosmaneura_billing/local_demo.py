"""Deterministic, synthetic-only local Cosmas CAB demonstration.

Run from the ``cosmaneura_billing`` project directory with::

    python -m cosmaneura_billing.local_demo

The parent process builds the already-installed frontend into a temporary
directory, launches a child API with a deliberately small environment, waits
for application readiness, and removes every mutable demo artifact at exit.
The child always binds to loopback and its route boundary excludes ad-hoc
clinical input, imports, scheduling, and outbound-message operations.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import errno
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Sequence
from uuid import uuid4

from .chase.synthetic_demo import prepare_synthetic_chase_seed
from .chase.worklist import Worklist
from .corpus.demo import SAMPLE_VISIT
from .persistence import (
    LOCAL_SYNTHETIC_DEMO_SOURCE,
    MemoryEncounterStore,
    SYNTHETIC_DEMO_SOURCE,
)
from .schemas import VisitBundle
from .seed_demo_release_candidate import prepare_synthetic_demo_seed
from .settings import Settings


LOCAL_DEMO_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
READINESS_TIMEOUT_SECONDS = 120.0
LOCAL_DEMO_PRACTICE_ID = "cosmas-local-synthetic-demo"

_PERSONA_ROLES = {
    "admin": ("Admin",),
    "provider": ("Provider",),
    "coder": ("Coder",),
    "billing-staff": ("BillingStaff",),
}

# This is an allowlist, not a denylist. A newly added operational/import route
# remains unavailable until somebody deliberately proves it belongs in the
# synthetic UI. In particular, POST /api/billing and /api/chase/detect are not
# present, so the launcher exposes no ad-hoc ingestion workflow. The allowed
# review routes still contain free-text fields; operators must use synthetic
# content only.
LOCAL_DEMO_API_ROUTES = frozenset({
    ("GET", "/api/health"),
    ("GET", "/api/session"),
    ("GET", "/api/ready"),
    ("GET", "/api/live"),
    ("GET", "/api/encounters"),
    ("POST", "/api/encounters/detail"),
    ("POST", "/api/encounters/coder-view"),
    ("POST", "/api/encounters/billing-tab"),
    ("POST", "/api/completeness"),
    ("POST", "/api/feedback"),
    ("POST", "/api/chase/worklist"),
    ("POST", "/api/chase/task"),
    ("POST", "/api/chase/demo-seed"),
    ("POST", "/api/chase/action"),
})

_SAFE_ENVIRONMENT_NAMES = frozenset({
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TMPDIR",
    "VIRTUAL_ENV",
})


# The checked-in strong example predates the telehealth completeness rule pack.
# This local-only derivative retains the exact clinical evidence that the
# offline MockLLM supports while adding wholly synthetic encounter logistics.
# It is never labeled as a release-candidate pack case or written to Postgres.
LOCAL_TEACHING_VISIT = replace(
    SAMPLE_VISIT,
    encounter_type="telehealth_established",
    place_of_service="10",
    signed_note=(
        SAMPLE_VISIT.signed_note
        + " This visit was conducted via two-way audio-video telehealth."
        + " Total time spent on the date of the encounter: 25 minutes."
        + " Patient was at home in New Hampshire."
        + " Provider was at the clinic office."
        + " Patient verbally consented to this telehealth visit."
    ),
)


class LocalDemoBoundaryMiddleware:
    """Permit only static reads and the bounded synthetic UI API contract."""

    def __init__(self, app, *, readiness_token: str = "") -> None:
        self.app = app
        self.readiness_token = readiness_token

    async def _reject(self, scope, receive, send, status: int, detail: str) -> None:
        # This boundary is installed after create_app and is therefore the
        # outermost middleware. Explicitly run its short response through the
        # same security-header component so rejection cannot bypass CSP,
        # framing protections, or the API no-store policy.
        from .security import SecurityHeadersMiddleware, send_json

        async def rejection(_scope, _receive, rejection_send) -> None:
            await send_json(rejection_send, status, {"detail": detail})

        secured = SecurityHeadersMiddleware(rejection, deployed_https=False)
        await secured(scope, receive, send)

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if path.startswith("/api"):
            allowed = (method, path) in LOCAL_DEMO_API_ROUTES
            if method == "OPTIONS":
                allowed = any(
                    route_path == path
                    for _, route_path in LOCAL_DEMO_API_ROUTES
                )
            if not allowed:
                await self._reject(
                    scope,
                    receive,
                    send,
                    403,
                    "the local demo accepts only fixed synthetic UI workflows",
                )
                return
        elif method not in {"GET", "HEAD"}:
            await self._reject(scope, receive, send, 405, "method is not allowed")
            return
        if path == "/api/ready" and self.readiness_token:
            async def send_with_run_identity(message) -> None:
                if message.get("type") == "http.response.start":
                    message = dict(message)
                    headers = list(message.get("headers", []))
                    headers.append((
                        b"x-cosmas-demo-run",
                        self.readiness_token.encode("ascii"),
                    ))
                    message["headers"] = headers
                await send(message)

            await self.app(scope, receive, send_with_run_identity)
            return
        await self.app(scope, receive, send)


def sanitized_child_environment(
    environ: Mapping[str, str] | None = None,
    *,
    isolated_home: Path | None = None,
) -> dict[str, str]:
    """Return the minimal non-secret environment used for build and serving."""
    source = os.environ if environ is None else environ
    clean = {
        name: value
        for name, value in source.items()
        if name in _SAFE_ENVIRONMENT_NAMES and value
    }
    if isolated_home is not None:
        clean["HOME"] = str(isolated_home)
    # Keep child output immediate so the supervisor can report startup failures.
    clean["PYTHONUNBUFFERED"] = "1"
    return clean


def _validate_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1_024 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be from 1024 through 65535")
    return port


def local_demo_settings(
    *, frontend_dist: Path, state_dir: Path, persona: str = "admin",
) -> Settings:
    """Construct configuration without consulting process environment."""
    try:
        roles = _PERSONA_ROLES[persona]
    except KeyError as exc:
        raise ValueError("unknown local demo persona") from exc
    settings = Settings(
        environment="development",
        log_level="INFO",
        frontend_dist=str(frontend_dist),
        telemetry_enabled=False,
        azure_client_id="",
        azure_openai_enabled=False,
        azure_openai_endpoint="",
        azure_openai_deployment="",
        azure_openai_model="",
        azure_openai_synthetic_only=False,
        azure_language_enabled=False,
        azure_language_shadow_enabled=False,
        azure_language_endpoint="",
        azure_language_mock=False,
        auth_mode="disabled",
        development_user_id=f"local-synthetic-{persona}",
        development_roles=roles,
        cors_origins=(),
        openai_api_key="",
        database_url="",
        state_store="local",
        chase_database_url="",
        chase_store="memory",
        practice_id=LOCAL_DEMO_PRACTICE_ID,
        practice_name="Cosmas synthetic demonstration",
        upload_url="https://example.invalid/insurance",
        direct_contract_list=str(state_dir / "no-operational-roster"),
        chase_gate_hmac_key="",
        patient_messaging_enabled=False,
        patient_messaging_operationally_activated=False,
        chase_scheduler_enabled=False,
        chase_transport="record_only",
        acs_email_connection_string="",
        acs_email_endpoint="",
        acs_email_sender_address="",
        acs_email_reply_to_address="",
        cpt_auto_fetch=False,
        xdg_cache_home=state_dir / "cache",
        ama_token_url=None,
        ama_base_url=None,
        ama_consumer_key=None,
        ama_consumer_secret=None,
        feedback_path=str(state_dir / "coder-feedback.jsonl"),
        access_audit_path=str(state_dir / "access-audit.jsonl"),
        preferences_enabled=False,
    )
    _assert_local_demo_settings(settings)
    return settings


def _assert_local_demo_settings(settings: Settings) -> None:
    """Fail closed if the explicit local composition ever gains a live path."""
    safe = (
        settings.environment == "development"
        and settings.auth_mode == "disabled"
        and settings.state_store == "local"
        and settings.chase_store == "memory"
        and not settings.telemetry_enabled
        and not settings.azure_openai_enabled
        and not settings.azure_openai_synthetic_only
        and not settings.azure_language_enabled
        and not settings.azure_language_shadow_enabled
        and not settings.azure_language_mock
        and not settings.openai_api_key
        and not settings.database_url
        and not settings.chase_database_url
        and not settings.cpt_auto_fetch
        and not settings.ama_configured
        and not settings.patient_messaging_enabled
        and not settings.patient_messaging_operationally_activated
        and not settings.chase_scheduler_enabled
        and settings.chase_transport == "record_only"
        and not settings.acs_email_connection_string
        and not settings.acs_email_endpoint
    )
    if not safe:
        raise RuntimeError("local demo configuration is not transport-inert")


def build_local_demo_encounter_store() -> MemoryEncounterStore:
    """Load exactly the local teaching case plus 12 digest-approved cases."""
    prepared = prepare_synthetic_demo_seed()
    store = MemoryEncounterStore(practice_id=LOCAL_DEMO_PRACTICE_ID)
    store.upsert(LOCAL_TEACHING_VISIT, source=LOCAL_SYNTHETIC_DEMO_SOURCE)
    for case in prepared.cases:
        store.upsert(
            VisitBundle(**case.visit),
            source=SYNTHETIC_DEMO_SOURCE,
        )
    expected_ids = {
        LOCAL_TEACHING_VISIT.encounter_id,
        *(case.case_id for case in prepared.cases),
    }
    actual_ids = {row.bundle.encounter_id for row in store.all()}
    if len(actual_ids) != 13 or actual_ids != expected_ids:
        raise RuntimeError("the fixed synthetic encounter set is incomplete")
    return store


def build_local_demo_chase_store() -> Worklist:
    """Return a fresh worklist containing the exact four approved demo rows."""
    seed = prepare_synthetic_chase_seed()
    store = Worklist()
    installed = store.extend(seed.items)
    if len(installed) != 4 or {item.chase_id for item in installed} != {
        case.item.chase_id for case in seed.cases
    }:
        raise RuntimeError("the fixed synthetic Card Chase set is incomplete")
    return store


def build_local_demo_app(
    *, frontend_dist: Path, state_dir: Path, persona: str = "admin",
    readiness_token: str = "",
):
    """Compose a fresh, synthetic-only application for one local run."""
    from .api import create_app

    if (
        readiness_token
        and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", readiness_token) is None
    ):
        raise ValueError("invalid local demo readiness token")
    settings = local_demo_settings(
        frontend_dist=frontend_dist, state_dir=state_dir, persona=persona,
    )
    application = create_app(
        settings=settings,
        frontend_dist=frontend_dist,
        encounter_store=build_local_demo_encounter_store(),
        chase_store=build_local_demo_chase_store(),
    )
    application.add_middleware(
        LocalDemoBoundaryMiddleware, readiness_token=readiness_token,
    )
    application.state.local_demo_mode = True
    return application


def build_frontend(
    frontend_dist: Path, *, environment: Mapping[str, str] | None = None,
) -> None:
    """Build with existing pinned dependencies; never install or fetch packages."""
    project_root = Path(__file__).resolve().parents[1]
    frontend_root = project_root / "frontend"
    vite = frontend_root / "node_modules" / "vite" / "bin" / "vite.js"
    clean_env = sanitized_child_environment(environment)
    if not _frontend_tools_available(frontend_root, clean_env):
        raise RuntimeError(
            "frontend dependencies are not installed; run `npm ci` in "
            "cosmaneura_billing/frontend before starting the demo"
        )
    frontend_dist.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        [
            "node", str(vite), "build",
            "--configLoader", "runner",
            "--mode", "local-demo",
            "--outDir", str(frontend_dist),
            "--emptyOutDir",
        ],
        cwd=frontend_root,
        env=clean_env,
        check=True,
    )
    if not (frontend_dist / "index.html").is_file():
        raise RuntimeError("frontend build completed without an index.html")


def _frontend_tools_available(
    frontend_root: Path, environment: Mapping[str, str],
) -> bool:
    """Check local prerequisites without attempting package installation."""
    vite = frontend_root / "node_modules" / "vite" / "bin" / "vite.js"
    return (
        vite.is_file()
        and shutil.which("node", path=environment.get("PATH")) is not None
    )


def _require_available_port(port: int) -> None:
    """Fail before build/start if another process already owns the endpoint."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((LOCAL_DEMO_HOST, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            detail = f"local demo port {port} is already in use on {LOCAL_DEMO_HOST}"
        else:
            detail = f"local demo could not reserve {LOCAL_DEMO_HOST}:{port}"
        raise RuntimeError(detail) from exc
    finally:
        probe.close()


def _wait_until_ready(
    process: subprocess.Popen, port: int, readiness_token: str,
) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"local demo API exited before readiness (status {return_code})"
            )
        connection = HTTPConnection(LOCAL_DEMO_HOST, port, timeout=1.0)
        try:
            connection.request("GET", "/api/ready")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            if (
                response.status == 200
                and response.getheader("X-Cosmas-Demo-Run") == readiness_token
                and payload == {
                "status": "ready",
                "ncciLoaded": True,
                "coverageLoaded": True,
                }
            ):
                return
        except (ConnectionError, OSError, TimeoutError, json.JSONDecodeError):
            pass
        finally:
            connection.close()
        time.sleep(0.1)
    raise RuntimeError("local demo API did not become ready before the timeout")


def _stop_child(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def _serve_child(
    *, frontend_dist: Path, state_dir: Path, port: int, persona: str,
    readiness_token: str,
) -> int:
    # Imported only inside the sanitized child process. No production settings
    # or credentials are inherited by this point.
    import uvicorn

    application = build_local_demo_app(
        frontend_dist=frontend_dist,
        state_dir=state_dir,
        persona=persona,
        readiness_token=readiness_token,
    )
    uvicorn.run(
        application,
        host=LOCAL_DEMO_HOST,
        port=port,
        proxy_headers=False,
        access_log=False,
        reload=False,
        workers=1,
    )
    return 0


def run_supervisor(*, port: int = DEFAULT_PORT, persona: str = "admin") -> int:
    """Build, start, health-check, and cleanly stop one disposable demo."""
    port = _validate_port(port)
    if persona not in _PERSONA_ROLES:
        raise ValueError("unknown local demo persona")
    _require_available_port(port)
    readiness_token = uuid4().hex
    with tempfile.TemporaryDirectory(prefix="cosmas-synthetic-demo-") as temp:
        root = Path(temp)
        frontend_dist = root / "frontend"
        state_dir = root / "state"
        isolated_home = root / "home"
        state_dir.mkdir(mode=0o700)
        isolated_home.mkdir(mode=0o700)
        child_environment = sanitized_child_environment(
            isolated_home=isolated_home,
        )
        build_frontend(frontend_dist, environment=child_environment)

        command = [
            sys.executable,
            "-m",
            "cosmaneura_billing.local_demo",
            "--serve-child",
            "--frontend-dist",
            str(frontend_dist),
            "--state-dir",
            str(state_dir),
            "--port",
            str(port),
            "--persona",
            persona,
            "--readiness-token",
            readiness_token,
        ]
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=child_environment,
        )
        previous_handlers: dict[int, object] = {}

        def request_stop(_signum, _frame) -> None:
            _stop_child(process)

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        try:
            _wait_until_ready(process, port, readiness_token)
            print("READY — Cosmas synthetic demo", flush=True)
            print(f"Open http://{LOCAL_DEMO_HOST}:{port}/", flush=True)
            print(
                "Safety: synthetic data only; cloud inference, Athena, imports, "
                "scheduling, messaging, and EHR writes are off.",
                flush=True,
            )
            print("DO NOT ENTER REAL PATIENT INFORMATION.", flush=True)
            print(
                "State is disposable and resets when this command stops. "
                "Press Ctrl-C to stop.",
                flush=True,
            )
            return_code = process.wait()
            if return_code not in {0, -signal.SIGINT, -signal.SIGTERM}:
                raise RuntimeError(f"local demo API exited with status {return_code}")
            return 0
        finally:
            _stop_child(process)
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic, synthetic-only Cosmas CAB demo.",
    )
    parser.add_argument("--port", type=_validate_port, default=DEFAULT_PORT)
    parser.add_argument(
        "--persona", choices=tuple(_PERSONA_ROLES), default="admin",
        help="synthetic local persona (default: admin)",
    )
    parser.add_argument("--serve-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--frontend-dist", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--readiness-token", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.serve_child:
        if (
            args.frontend_dist is None
            or args.state_dir is None
            or not args.readiness_token
        ):
            raise SystemExit("child mode requires temporary frontend and state paths")
        return _serve_child(
            frontend_dist=args.frontend_dist,
            state_dir=args.state_dir,
            port=args.port,
            persona=args.persona,
            readiness_token=args.readiness_token,
        )
    if (
        args.frontend_dist is not None
        or args.state_dir is not None
        or args.readiness_token is not None
    ):
        raise SystemExit("temporary child paths are internal options")
    return run_supervisor(port=args.port, persona=args.persona)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
