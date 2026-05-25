"""Make the `sat` package importable in tests without installing the project."""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is the project root for the sat package; conftest lives in scripts/tests/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
