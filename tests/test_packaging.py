from __future__ import annotations

import importlib.metadata
from pathlib import Path

import noisegate


def test_import_smoke_exports_plugin_entrypoints() -> None:
    assert noisegate.__version__
    assert callable(noisegate.register)
    assert callable(noisegate.transform_tool_result)


def test_plugin_manifest_declares_hook() -> None:
    manifest = Path(noisegate.__file__).resolve().parent / "plugin.yaml"

    text = manifest.read_text(encoding="utf-8")

    assert "name: noisegate" in text
    assert "transform_tool_result" in text


def test_distribution_entrypoint_is_declared_when_installed() -> None:
    eps = importlib.metadata.entry_points()
    group = eps.select(group="hermes_agent.plugins")
    matching = [ep for ep in group if ep.name == "noisegate"]

    assert matching
    assert matching[0].value == "noisegate"
