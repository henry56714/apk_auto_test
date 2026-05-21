"""Make the in-tree package importable without `pip install -e`."""

import pathlib
import sys

_SRC = pathlib.Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
