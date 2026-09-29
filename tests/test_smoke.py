"""Smoke test: the package skeleton imports cleanly."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "src",
        "src.scraper",
        "src.scraper.client",
        "src.scraper.fests_crawler",
        "src.scraper.bouts_parser",
        "src.pipeline",
        "src.pipeline.cleaner",
        "src.pipeline.elo_engine",
        "src.exporter",
        "src.exporter.static_builder",
    ],
)
def test_module_imports(module: str) -> None:
    assert importlib.import_module(module) is not None
