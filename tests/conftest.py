"""Shared pytest fixtures for chorus-forms-web tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def csd_fixtures_dir() -> Path:
    """Path to a directory containing real .CSD/.LKP fixture files."""
    raw = os.environ.get("CSD_FIXTURES_DIR")
    if not raw:
        pytest.skip("CSD_FIXTURES_DIR environment variable not set")
    path = Path(raw)
    if not path.exists():
        pytest.skip(f"CSD_FIXTURES_DIR path does not exist: {path}")
    return path
