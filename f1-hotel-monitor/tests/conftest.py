import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from f1hotel.config import load_config  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("F1HOTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RAKUTEN_APP_ID", "test-app-id")
    monkeypatch.setenv("RAKUTEN_ACCESS_KEY", "pk_test")
    monkeypatch.delenv("NOTIFY_CHANNELS", raising=False)
    c = load_config(ROOT / "config.toml", env_path=tmp_path / "nonexistent.env")
    c.data_dir.mkdir(parents=True, exist_ok=True)
    return c


FIXTURES = Path(__file__).parent / "fixtures"
