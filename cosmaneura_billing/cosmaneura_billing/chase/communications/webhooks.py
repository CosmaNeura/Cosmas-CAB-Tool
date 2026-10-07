"""Signed provider callback ASGI service, hosted separately from the Entra API."""
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .runtime import CALLBACK_PATHS
from .store import CommunicationsError
from .twilio import MAX_BODY
from .worker import configured_dispatcher

BODY_DEADLINE_SECONDS = 5


class CallbackTooLarge(ValueError):
    pass


async def _body(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BODY:
            raise CallbackTooLarge()
        raw.extend(chunk)
    return bytes(raw)


def create_app(dispatcher=None):
    service = dispatcher if dispatcher is not None else configured_dispatcher(outbound=False)
    if service.adapter is None or service.adapter.transport is not None:
        raise ValueError("callback service requires a signature-only adapter")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def endpoint(kind):
        async def receive(request: Request):
            if (request.url.query or request.headers.get("content-type", "").split(";", 1)[0].lower()
                    != "application/x-www-form-urlencoded"):
                return JSONResponse({"status": "invalid_callback"}, status_code=400)
            signatures = request.headers.getlist("x-twilio-signature")
            if len(signatures) != 1:
                return JSONResponse({"status": "invalid_callback"}, status_code=403)
            try:
                raw = await asyncio.wait_for(_body(request), timeout=BODY_DEADLINE_SECONDS)
            except CallbackTooLarge:
                return JSONResponse({"status": "callback_too_large"}, status_code=413)
            except asyncio.TimeoutError:
                return JSONResponse({"status": "callback_timeout"}, status_code=408)
            except Exception:
                return JSONResponse({"status": "invalid_callback"}, status_code=400)
            try:
                result = await asyncio.to_thread(service.receive_webhook, kind, raw, signatures[0])
                return Response(result["twiml"], media_type="application/xml",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            except CommunicationsError as exc:
                return JSONResponse({"status": "callback_unavailable" if exc.status >= 500 else "invalid_callback"}, status_code=exc.status)
            except Exception:
                return JSONResponse({"status": "callback_unavailable"}, status_code=503)
        return receive

    for kind, path in CALLBACK_PATHS.items():
        app.add_api_route(path, endpoint(kind), methods=["POST"])

    @app.get("/live")
    def live():
        return {"status": "alive"}

    @app.get("/ready")
    def ready():
        try:
            with service.store._db() as db:
                db.execute("SELECT error_code FROM chase_communication_provider_events LIMIT 0")
                for table in ("chase_communication_senders", "chase_communication_provider_events",
                              "chase_communication_bindings", "chase_contact_snapshots"):
                    db.execute(f"SELECT 1 FROM {table} LIMIT 0")
                row = db.execute("""SELECT has_table_privilege(current_user,'chase_communication_provider_events','INSERT')
                    AND has_sequence_privilege(current_user,'chase_communication_provider_events_sequence_no_seq','USAGE') AS allowed""").fetchone()
                if not row or row["allowed"] is not True:
                    raise ValueError()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
    return app
