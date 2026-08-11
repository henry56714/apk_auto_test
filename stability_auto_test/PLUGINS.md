# Plugin development

`stability_auto_test` exposes four stable plugin interfaces under
`scripts/sat/plugins/interfaces.py`. Third-party plugins are **disabled by
default**; enable them with `--enable-plugins` or `plugins.enabled: true`.

## Interfaces

| Interface | Responsibility | Method |
|---|---|---|
| `Collector` | produce observations | `collect(adb) -> Iterable[Dict]` |
| `Analyzer` | emit / enrich events | `analyze(observations) -> Iterable[Dict]` |
| `EvidenceProvider` | gather evidence | `provide(incident) -> Dict` |
| `Reporter` | consume unified result model | `render(result)` |

## Rules

- Plugin output must be a dict namespaced as `plugins.<name>.<field>` (use
  `PluginRunner.namespace`), so two plugins can never collide.
- Plugins run through `PluginRunner.call()` with a timeout; a crash or timeout
  lands in `report["plugins"]["health"]` and never interrupts collection.
- Example: `scripts/sat/plugins/example.py` (`ExampleCollector`).

## Entry points

Register plugins with the `sat.plugins` entry-point group in your package:

```toml
[project.entry-points."sat.plugins"]
my-collector = "myplugin:MyCollector"
```

## Verify

```bash
cd stability_auto_test/scripts
python -m pytest tests/test_plugins.py tests/test_plugin_isolation.py -q
```

## Community adapters

Slack / Teams / GitHub notification adapters are intentionally not part of the
core package; implement them as small plugins or standalone scripts using the
generic webhook (`webhook.url` in config) so the core stays cloud-free.
