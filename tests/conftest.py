from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from doc_evaluator.config import REPO_ROOT, Settings
from doc_evaluator.guardrails.engine import GuardrailEngine
from doc_evaluator.tools.registry import ToolRegistry

FIXTURE_SPECS = REPO_ROOT / "fixtures" / "specs"

ALLOWED = ("petstore3.swagger.io", "raw.githubusercontent.com")


@pytest.fixture
def settings() -> Settings:
    """Mock mode, deterministic, no network and no API key."""
    return Settings.from_env(mode="mock", allowed_hosts=ALLOWED)


@pytest.fixture
def engine() -> GuardrailEngine:
    return GuardrailEngine(allowed_hosts=ALLOWED)


@pytest.fixture
def approve_all_registry(settings: Settings) -> ToolRegistry:
    return ToolRegistry(settings=settings, approver=lambda decision, args: True)


def load_spec(name: str) -> dict:
    path = FIXTURE_SPECS / name
    text = path.read_text()
    return yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else json.loads(text)


@pytest.fixture
def good_spec() -> dict:
    return load_spec("petstore.json")


@pytest.fixture
def bad_spec() -> dict:
    return load_spec("legacy_billing.yaml")


@pytest.fixture
def broken_spec() -> dict:
    return load_spec("broken_inventory.json")


@pytest.fixture
def spec_path() -> Path:
    return FIXTURE_SPECS / "petstore.json"
