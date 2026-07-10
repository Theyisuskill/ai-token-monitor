"""Make daemon/src importable without installing the package.

Everything under test (store, config, adapters, pricing) is pure Python —
no PyGObject/dasbus — so the suite runs on any CI box with pyyaml + pytest.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
