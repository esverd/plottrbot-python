from __future__ import annotations

from pathlib import Path

import pytest

from plottrbot.config.settings import SettingsStore


@pytest.fixture()
def settings_store(tmp_path: Path) -> SettingsStore:
    return SettingsStore(path=tmp_path / "config.json")
