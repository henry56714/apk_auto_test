"""Generic Android APK performance auto-test tool.

Public entry points:
- PerfTest / PerfConfig  — library API
- main (CLI)             — `python -m pat`
"""

from .api import PerfConfig, PerfTest

__version__ = "0.1.0.dev0"
__all__ = ["PerfConfig", "PerfTest"]
