import pytest

from vigia.config import Settings
from vigia.store import PriceStore, open_store


def make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "travelpayouts_token": "test-token",
        "db_path": ":memory:",
        "discovery": False,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


@pytest.fixture
async def store(tmp_path):
    async with open_store(str(tmp_path / "test.db")) as s:
        await s.init_schema()
        yield s


@pytest.fixture
def cfg() -> Settings:
    return make_settings()


__all__ = ["PriceStore", "make_settings"]
