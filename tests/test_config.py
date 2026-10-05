import pytest
from pydantic import ValidationError

from jellyorganize.config import Config


def test_overlapping_roots_rejected(tmp_path):
    with pytest.raises(ValidationError, match="separate, non-nested"):
        Config.model_validate({
            "movies": {"incoming": tmp_path / "media", "library": tmp_path / "media" / "Movies"},
        })


def test_shared_incoming_root_can_contain_legacy_incoming_paths(tmp_path):
    config = Config.model_validate({"incoming": {"path": tmp_path / "Incoming"}})
    assert config.incoming_root("movie") == tmp_path / "Incoming"
    assert config.incoming_root("tv") == tmp_path / "Incoming"


def test_shared_incoming_root_cannot_contain_library(tmp_path):
    with pytest.raises(ValidationError, match="separate, non-nested"):
        Config.model_validate({
            "incoming": {"path": tmp_path / "media"},
            "movies": {"library": tmp_path / "media" / "Movies"},
        })


@pytest.mark.parametrize('url', ['https://user:secret@example.test', 'file:///tmp/client',
                                'http://example.test/?token=secret', 'http://example.test/\napi'])
def test_download_api_url_rejects_unsafe_configuration(url):
    with pytest.raises(ValidationError):
        Config.model_validate({'qbittorrent': {'url': url}})


def test_download_root_must_not_overlap_incoming(tmp_path):
    with pytest.raises(ValidationError, match='separate, non-nested'):
        Config.model_validate({'downloads': {'path': tmp_path / 'downloads'},
                              'incoming': {'path': tmp_path / 'downloads' / 'Incoming'}})
