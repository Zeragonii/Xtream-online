from app.sessions import StreamSessionManager
from app.storage import ProviderConfig
from app.xtream import XtreamClient, normalize_base_url


def test_normalize_base_url_adds_scheme():
    assert normalize_base_url("example.test:8080") == "http://example.test:8080"


def test_normalize_base_url_removes_player_api():
    assert normalize_base_url("https://example.test/player_api.php") == "https://example.test"


def test_stream_url_encodes_credentials():
    client = XtreamClient(
        ProviderConfig(
            base_url="http://example.test:8080",
            username="user name",
            password="p@ss/word",
            output="ts",
        )
    )
    assert client.stream_url(123) == "http://example.test:8080/live/user%20name/p%40ss%2Fword/123.ts"


def test_auto_copy_for_h264_aac():
    assert StreamSessionManager.choose_mode("auto", {"video": "h264", "audio": "aac"}) == "copy"


def test_auto_transcode_for_hevc():
    assert StreamSessionManager.choose_mode("auto", {"video": "hevc", "audio": "aac"}) == "transcode"


def test_explicit_mode_wins():
    assert StreamSessionManager.choose_mode("copy", {"video": "hevc", "audio": "ac3"}) == "copy"


def test_auto_transcodes_when_probe_is_unknown():
    assert StreamSessionManager.choose_mode("auto", {"video": None, "audio": None}) == "transcode"
