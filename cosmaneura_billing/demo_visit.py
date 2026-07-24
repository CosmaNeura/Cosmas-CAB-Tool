"""Back-compat shim: the demo encounter now lives inside the package so the API
server can serve it. Import from cosmaneura_billing.corpus.demo going forward."""
from cosmaneura_billing.corpus.demo import SAMPLE_VISIT

__all__ = ["SAMPLE_VISIT"]
