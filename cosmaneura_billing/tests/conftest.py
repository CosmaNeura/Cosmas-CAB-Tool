"""Make the project directory importable so tests run without installation.

The project root (which contains the ``cosmaneura_billing`` package and the
demo modules ``demo_visit`` / ``prove_no_upcode``) is one level up from tests/.
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
