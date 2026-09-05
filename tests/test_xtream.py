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


def test_ffmpeg_stderr_codec_parser():
    from app.sessions import parse_ffmpeg_codec_line

    codecs = {"video": None, "audio": None}
    assert not parse_ffmpeg_codec_line("Stream #0:0: Video: h264 (High), yuv420p, 1920x1080", codecs)
    assert not parse_ffmpeg_codec_line("Stream #0:1: Audio: eac3, 48000 Hz, stereo", codecs)
    assert parse_ffmpeg_codec_line("Stream mapping:", codecs)
    assert codecs == {"video": "h264", "audio": "eac3"}


def test_stream_candidates_do_not_fetch_m3u_on_hot_path(monkeypatch):
    from app.xtream import _AUTH_CACHE, _ROUTE_CACHE

    _AUTH_CACHE.clear()
    _ROUTE_CACHE.clear()
    client = XtreamClient(
        ProviderConfig(
            base_url="https://provider.example",
            username="demo",
            password="secret",
            output="ts",
        )
    )

    async def fake_authenticate(*, force=False):
        return {
            "user_info": {"auth": 1, "status": "Active", "allowed_output_formats": ["ts"]},
            "server_info": {
                "url": "provider.example",
                "port": "80",
                "server_protocol": "http",
            },
        }

    async def forbidden_playlist(*args, **kwargs):
        raise AssertionError("get.php must not be used on the normal candidate path")

    monkeypatch.setattr(client, "authenticate", fake_authenticate)
    monkeypatch.setattr(client, "_playlist_candidate", forbidden_playlist)
    candidates = asyncio.run(client.stream_candidates(777))
    assert candidates
    assert candidates[0].endswith("/live/demo/secret/777.ts")


def test_successful_route_is_learned_and_reused_first(monkeypatch):
    from app.xtream import _AUTH_CACHE, _ROUTE_CACHE

    _AUTH_CACHE.clear()
    _ROUTE_CACHE.clear()
    config = ProviderConfig(
        base_url="https://provider.example",
        username="demo",
        password="secret",
        output="ts",
    )

    async def fake_authenticate(*, force=False):
        return {
            "user_info": {"auth": 1, "status": "Active", "allowed_output_formats": ["ts"]},
            "server_info": {
                "url": "provider.example",
                "port": "80",
                "server_protocol": "http",
            },
        }

    first = XtreamClient(config)
    monkeypatch.setattr(first, "authenticate", fake_authenticate)
    candidates = asyncio.run(first.stream_candidates(100))
    root_candidate = next(url for url in candidates if url == "http://provider.example/demo/secret/100.ts")
    first.remember_success(root_candidate)

    second = XtreamClient(config)
    monkeypatch.setattr(second, "authenticate", fake_authenticate)
    next_candidates = asyncio.run(second.stream_candidates(101))
    assert next_candidates[0] == "http://provider.example/demo/secret/101.ts"


def test_duplicate_concurrent_start_spawns_one_process(monkeypatch):
    from app.sessions import ProcessAttempt

    class FakeProcess:
        def __init__(self):
            self.returncode = None

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def run_test():
        manager = StreamSessionManager()
        spawn_count = 0

        async def fake_spawn(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            process = FakeProcess()
            task = asyncio.create_task(asyncio.sleep(3600))
            return ProcessAttempt(
                process=process,
                stderr_lines=__import__("collections").deque(),
                stderr_task=task,
                source_codecs={"video": None, "audio": None},
                probe_complete=asyncio.Event(),
            )

        async def fake_wait_for_playlist(attempt, directory):
            await asyncio.sleep(0.05)
            return True

        monkeypatch.setattr(manager, "_spawn", fake_spawn)
        monkeypatch.setattr(manager, "_wait_for_playlist", fake_wait_for_playlist)

        first, second = await asyncio.gather(
            manager.start(123, ["http://provider/live/u/p/123.ts"], "copy"),
            manager.start(123, ["http://provider/live/u/p/123.ts"], "copy"),
        )
        assert first.id == second.id
        assert spawn_count == 1
        await manager.stop_all()

    asyncio.run(run_test())


def test_session_diagnostics_reports_hls_health_without_upstream(tmp_path):
    from app.main import session_diagnostics
    from app.sessions import StreamSession, session_manager
    from collections import deque

    class FakeProcess:
        returncode = None

    session_id = "diagtest123"
    playlist = tmp_path / "index.m3u8"
    segment = tmp_path / "segment_000000.ts"
    playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nsegment_000000.ts\n")
    segment.write_bytes(b"test-segment")
    upstream = "http://provider.example/live/secret-user/secret-password/123.ts"

    session_manager.sessions[session_id] = StreamSession(
        id=session_id,
        stream_id=123,
        upstream_url=upstream,
        directory=tmp_path,
        process=FakeProcess(),
        mode="copy",
        source_codecs={"video": "h264", "audio": "aac"},
        stderr_lines=deque(["Input #0, mpegts, from '<upstream>':"]),
    )
    try:
        result = asyncio.run(session_diagnostics(session_id))
    finally:
        session_manager.sessions.pop(session_id, None)

    assert result["process_alive"] is True
    assert result["playlist_exists"] is True
    assert result["segment_count"] == 1
    assert result["newest_segment_bytes"] == len(b"test-segment")
    assert upstream not in str(result)
    assert "secret-password" not in str(result)


def test_frontend_prefers_hlsjs_before_native_hls():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "app" / "static" / "app.js").read_text()
    fn = js[js.index("function attachPlayer"):js.index("function destroyHls")]
    assert fn.index("Hls.isSupported()") < fn.index('video.canPlayType("application/vnd.apple.mpegurl")')
    assert 'reportClientEvent("player-path", "hls.js/MSE", "info")' in fn


def test_catalog_cache_paginates_and_filters_without_network():
    from app.storage import catalog_store, provider_cache_key

    config = ProviderConfig(base_url="http://provider.example", username="demo", password="secret", output="ts")
    key = provider_cache_key(config)
    catalog_store.clear()
    catalog_store.replace(
        key,
        [{"category_id": "10", "category_name": "News", "parent_id": 0}],
        [
            {"stream_id": 1, "name": "Alpha News", "category_id": "10", "tv_archive": False},
            {"stream_id": 2, "name": "Beta News", "category_id": "10", "tv_archive": False},
            {"stream_id": 3, "name": "Gamma Sport", "category_id": "20", "tv_archive": False},
        ],
    )

    first, total = catalog_store.channels(key, category_id="10", search="", offset=0, limit=1)
    second, total2 = catalog_store.channels(key, category_id="10", search="", offset=1, limit=1)
    searched, searched_total = catalog_store.channels(key, category_id=None, search="gamma", offset=0, limit=10)

    assert total == total2 == 2
    assert [row["stream_id"] for row in first] == [1]
    assert [row["stream_id"] for row in second] == [2]
    assert searched_total == 1
    assert searched[0]["name"] == "Gamma Sport"


def test_catalog_categories_endpoint_reads_local_cache(monkeypatch):
    from app.main import categories
    from app.storage import catalog_store, provider_cache_key, store

    config = ProviderConfig(base_url="http://provider.example", username="cache-user", password="cache-pass", output="ts")
    store.save(config)
    key = provider_cache_key(config)
    catalog_store.clear()
    catalog_store.replace(
        key,
        [{"category_id": "7", "category_name": "Cached", "parent_id": 0}],
        [],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("provider must not be queried by /api/categories")

    monkeypatch.setattr(XtreamClient, "live_categories", forbidden)
    try:
        result = asyncio.run(categories())
        assert result == [{"category_id": "7", "category_name": "Cached", "parent_id": "0"}]
    finally:
        store.clear()
        catalog_store.clear()


def test_frontend_pages_channels_and_has_independent_scrollbar():
    from pathlib import Path

    root = Path(__file__).parents[1]
    js = (root / "app" / "static" / "app.js").read_text()
    css = (root / "app" / "static" / "styles.css").read_text()
    assert "const CHANNEL_PAGE_SIZE = 150" in js
    assert 'channelList.addEventListener("scroll"' in js
    assert "/api/catalog/refresh" in js
    channel_rule = css[css.index(".channel-list {"):css.index("}", css.index(".channel-list {"))]
    assert "overflow-y: auto" in channel_rule



def test_version_comparison_for_update_checker():
    from app.update import _version_tuple

    assert _version_tuple("0.1.6") == (0, 1, 6)
    assert _version_tuple("v0.1.7") == (0, 1, 7)
    assert _version_tuple("0.1.6-edge+abc") == (0, 1, 6)
    assert _version_tuple("0.1.7") > _version_tuple("0.1.6")


def test_frontend_has_update_badge_and_picture_in_picture():
    from pathlib import Path

    root = Path(__file__).parents[1]
    html = (root / "app" / "static" / "index.html").read_text()
    js = (root / "app" / "static" / "app.js").read_text()
    assert 'id="updateBadge"' in html
    assert 'id="popoutBtn"' in html
    assert '/api/update/status' in js
    assert 'requestPictureInPicture' in js
    assert 'enterpictureinpicture' in js


def test_edge_workflow_bakes_version_commit_and_channel():
    from pathlib import Path

    workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "edge.yml").read_text()
    assert "cat version.txt" in workflow
    assert "APP_CHANNEL=edge" in workflow
    assert "APP_COMMIT=${{ github.sha }}" in workflow
    assert "APP_REPOSITORY=${{ github.repository }}" in workflow


def test_xmltv_parser_filters_channels_and_window(tmp_path):
    from app.epg import parse_xmltv_file

    xml = '''<?xml version="1.0" encoding="UTF-8"?>
    <tv>
      <channel id="bbc1.uk"><display-name>BBC One</display-name></channel>
      <programme start="20260905200000 +0100" stop="20260905210000 +0100" channel="bbc1.uk">
        <title>Evening News</title><desc>Today's headlines.</desc><category>News</category>
      </programme>
      <programme start="20260905210000 +0100" stop="20260905220000 +0100" channel="other.uk">
        <title>Wrong Channel</title>
      </programme>
    </tv>'''
    path = tmp_path / "guide.xml"
    path.write_text(xml)
    items = parse_xmltv_file(path, {"bbc1.uk"}, earliest_ts=0, latest_ts=9999999999)
    assert len(items) == 1
    assert items[0]["title"] == "Evening News"
    assert items[0]["description"] == "Today's headlines."
    assert items[0]["category"] == "News"


def test_catalog_retains_epg_channel_id_and_epg_cache_joins_now():
    from app.storage import catalog_store, epg_store, provider_cache_key
    import time

    config = ProviderConfig(base_url="http://epg-provider.example", username="epg-user", password="epg-pass", output="ts")
    key = provider_cache_key(config)
    catalog_store.clear()
    epg_store.clear()
    catalog_store.replace(
        key,
        [{"category_id": "1", "category_name": "General", "parent_id": 0}],
        [{"stream_id": 42, "name": "Test TV", "category_id": "1", "tv_archive": False, "epg_channel_id": "test.tv"}],
    )
    now = time.time()
    epg_store.replace(
        key,
        [{
            "epg_channel_id": "test.tv",
            "start_ts": now - 600,
            "stop_ts": now + 1200,
            "title": "Current Show",
            "description": "Description",
            "category": "Entertainment",
        }],
    )
    rows, total = catalog_store.channels(key, category_id=None, search="", offset=0, limit=10)
    assert total == 1
    assert rows[0]["epg_channel_id"] == "test.tv"
    current = epg_store.now_for_streams(key, [42], now=now)
    assert current[42]["title"] == "Current Show"
    schedule = epg_store.schedule(key, "test.tv", now=now, limit=10)
    assert schedule[0]["title"] == "Current Show"


def test_epg_frontend_and_refresh_controls_exist():
    from pathlib import Path

    root = Path(__file__).parents[1]
    html = (root / "app" / "static" / "index.html").read_text()
    js = (root / "app" / "static" / "app.js").read_text()
    css = (root / "app" / "static" / "styles.css").read_text()
    assert 'id="epgList"' in html
    assert 'id="epgRefreshBtn"' in html
    assert '/api/epg/channel/' in js
    assert '/api/epg/refresh' in js
    assert '.epg-list' in css and 'overflow-y: auto' in css


def test_epg_refresh_interval_is_configurable():
    from app.config import settings
    assert settings.epg_refresh_interval >= 300
