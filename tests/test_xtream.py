from app.sessions import StreamSessionManager
from app.storage import ProviderConfig
from app.xtream import XtreamClient, _base_from_server_info, normalize_base_url, redact_url
import asyncio


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


def test_stream_url_supports_root_style_live_path():
    client = XtreamClient(
        ProviderConfig(
            base_url="http://example.test:8080",
            username="demo",
            password="secret",
            output="ts",
        )
    )
    assert client.stream_url(123, live_prefix=False) == "http://example.test:8080/demo/secret/123.ts"


def test_server_info_builds_advertised_stream_base():
    assert _base_from_server_info(
        {
            "url": "provider.example",
            "port": "25461",
            "https_port": "25463",
            "server_protocol": "http",
        }
    ) == "http://provider.example:25461"


def test_redact_url_removes_credentials_and_query():
    value = redact_url("http://example.test/live/user/password/123.ts?token=secret")
    assert value == "http://example.test/.../123.ts"
    assert "user" not in value
    assert "password" not in value
    assert "token" not in value


def test_auto_copy_for_h264_aac():
    assert StreamSessionManager.choose_mode("auto", {"video": "h264", "audio": "aac"}) == "copy"


def test_auto_transcode_for_hevc():
    assert StreamSessionManager.choose_mode("auto", {"video": "hevc", "audio": "aac"}) == "transcode"


def test_explicit_mode_wins():
    assert StreamSessionManager.choose_mode("copy", {"video": "hevc", "audio": "ac3"}) == "copy"


def test_auto_transcodes_when_probe_is_unknown():
    assert StreamSessionManager.choose_mode("auto", {"video": None, "audio": None}) == "transcode"


def test_stream_candidates_use_server_info_and_common_paths(monkeypatch):
    client = XtreamClient(
        ProviderConfig(
            base_url="https://provider.example",
            username="demo",
            password="secret",
            output="ts",
        )
    )

    async def fake_authenticate():
        return {
            "user_info": {"auth": 1, "status": "Active", "allowed_output_formats": ["ts"]},
            "server_info": {
                "url": "provider.example",
                "port": "80",
                "https_port": "443",
                "server_protocol": "http",
            },
        }

    async def no_playlist(stream_id, output):
        return None

    monkeypatch.setattr(client, "authenticate", fake_authenticate)
    monkeypatch.setattr(client, "_playlist_candidate", no_playlist)
    candidates = asyncio.run(client.stream_candidates(123))

    assert "http://provider.example/live/demo/secret/123.ts" in candidates
    assert "http://provider.example/demo/secret/123.ts" in candidates
    assert "https://provider.example/live/demo/secret/123.ts" in candidates
    assert "http://provider.example:80/live/demo/secret/123.ts" in candidates
