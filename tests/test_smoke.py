"""Smoke tests: every module under src/ and the Docker entrypoint import cleanly."""

import importlib
import pkgutil

import pytest

import src

SRC_MODULES = sorted(
    m.name for m in pkgutil.walk_packages(src.__path__, prefix="src.")
)


def test_found_src_modules():
    assert len(SRC_MODULES) > 10


@pytest.mark.parametrize("module_name", SRC_MODULES)
def test_src_module_imports(module_name):
    importlib.import_module(module_name)


def test_docker_entrypoint_imports():
    # Dockerfile CMD runs run_whale_copy_trader.py; importing must not start the bot.
    mod = importlib.import_module("run_whale_copy_trader")
    assert hasattr(mod, "__name__")
