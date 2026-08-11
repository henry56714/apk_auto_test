"""Plugin discovery / isolation."""

from __future__ import annotations

import importlib.metadata
import threading
from typing import Dict, List

PLUGIN_GROUP = "sat.plugins"


def discover_plugins(*, enabled: bool = False, group: str = PLUGIN_GROUP) -> List[str]:
    """Return installed entry-point names (empty unless explicitly enabled)."""
    if not enabled:
        return []
    try:
        eps = importlib.metadata.entry_points(group=group)
    except (importlib.metadata.PackageNotFoundError, TypeError):
        return []
    return sorted({ep.name for ep in eps})


def load_plugin(name: str, *, group: str = PLUGIN_GROUP):
    try:
        eps = importlib.metadata.entry_points(group=group)
    except (importlib.metadata.PackageNotFoundError, TypeError):
        return None
    for ep in eps:
        if ep.name == name:
            return ep.load()
    return None


class PluginRunner:
    def __init__(self) -> None:
        self.health: Dict[str, str] = {}

    def call(
        self,
        plugin_name: str,
        func,
        *args,
        timeout_sec: float = 5.0,
        **kwargs,
    ):
        """Run a plugin call with timeout; failures land in health, never raise."""
        result = {"ok": None}

        def target():
            try:
                result["ok"] = func(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - isolation
                result["ok"] = None
                result["error"] = f"{type(e).__name__}: {e}"

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=timeout_sec)
        if thread.is_alive():
            self.health[plugin_name] = "timed_out"
            return None
        if "error" in result:
            self.health[plugin_name] = "failed"
            return None
        self.health[plugin_name] = "ok"
        return result["ok"]

    def namespace(self, plugin_name: str, output: Dict) -> Dict:
        return {f"plugins.{plugin_name}": output}
