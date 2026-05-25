"""Generic Android APK stability auto-test tool.

Public entry points:
- StabilityTest / StabilityConfig — library API
- main (CLI)                       — `python -m sat`
"""

from .api import StabilityConfig, StabilityTest

__version__ = "0.1.0.dev0"
__all__ = ["StabilityConfig", "StabilityTest"]
