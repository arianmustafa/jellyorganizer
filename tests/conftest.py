from pathlib import Path

import pytest

from jellyorganize.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate({
        "movies": {"incoming": tmp_path / "Incoming" / "Movies", "library": tmp_path / "Movies"},
        "tv": {"incoming": tmp_path / "Incoming" / "TV Shows", "library": tmp_path / "TV Shows"},
        "incoming": {"minimum_age_seconds": 0, "stability_seconds": 0},
    })
