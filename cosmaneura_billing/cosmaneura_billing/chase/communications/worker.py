"""Separate Twilio worker and callback composition. The staff API never sends."""
from dataclasses import asdict
import json
import os

from .config import TwilioServiceConfig
from .runtime import dispatcher_from_settings, public_sender
from .transport import TwilioHttpsTransport
from .twilio import TwilioAdapter, TwilioConfig


def configured_dispatcher(*, outbound=False, environ=None):
    env = os.environ if environ is None else environ
    config = TwilioServiceConfig.from_env(env)
    # Credential access belongs only to this separately launched service. The
    # callback service has no outbound transport even though it verifies signatures.
    token = env.get("COSMANEURA_TWILIO_AUTH_TOKEN", "")
    twilio = TwilioConfig(**asdict(public_sender(config)), auth_token=token,
                          enabled_channels=config.communications_enabled_channels)
    adapter = TwilioAdapter(twilio,
        transport=TwilioHttpsTransport(twilio.account_sid, enabled_channels=config.communications_enabled_channels) if outbound else None)
    return dispatcher_from_settings(config, dsn=config.chase_worker_database_url, adapter=adapter)


def main():
    """One bounded batch; schedule this command as the dedicated worker job."""
    try:
        dispatcher = configured_dispatcher(outbound=True)
        results = dispatcher.run_once(limit=25)
        counts = {}
        for item in results:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        print(json.dumps({"processed": len(results), "outcomes": counts}))
        return 0
    except Exception:
        # No exception text: provider bodies, credentials and routes are private.
        print(json.dumps({"status": "communications_worker_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
