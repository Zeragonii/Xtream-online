# Xtream Online

A lightweight, LAN-first Xtream Codes web player. The provider credentials and upstream stream URLs stay on the server; the browser receives a clean channel/category API and a local HLS stream produced by FFmpeg.

> Use this only with IPTV services and content you are authorised to access.

## v0.1 scope

- Xtream provider setup/login
- Live categories
- Live channels
- Server-side Xtream credentials
- FFmpeg-managed stream sessions
- MPEG-TS or HLS upstream input
- HLS output for browser playback
- Automatic remux vs transcode decision
- Manual `Auto` / `Remux only` / `Transcode` override
- hls.js browser player
- Configurable maximum concurrent upstream streams
- Idle-session cleanup
- Docker image
- GitHub Actions CI
- GHCR `edge` builds from `main`
- Release Please semantic versioning + GitHub releases
- Stable GHCR image tags when a release is created

EPG, favourites, logos, VOD and series are intentionally deferred to later releases.

### v0.1.6 version/update + Picture-in-Picture

- Shows the running semantic version in the header.
- Background update indicator checks the public GitHub repository; edge images compare their build commit with `main`, stable images compare against the latest GitHub release.
- `Pop out` uses the browser Picture-in-Picture API to keep video floating above other windows.
- `UPDATE_CHECK_ENABLED` and `UPDATE_CHECK_INTERVAL` control update checks.

### v0.1.5 catalogue/UI improvements

- Categories and channels are cached persistently in SQLite.
- Provider catalogue refreshes run in the background; page navigation never waits on Xtream API calls.
- The cache refreshes every 15 minutes by default and can be refreshed manually from the UI.
- Channel/category filtering and search are served from SQLite.
- The channel list is paged 150 rows at a time and loads additional rows on scroll.
- Categories and Channels each have their own independent scrollbar.

### v0.1.2 playback-start improvements

- Provider `get.php` is no longer downloaded/scanned on normal channel changes.
- Successful stream URL layout is learned and reused first for later channels.
- Player API/server metadata is cached for 10 minutes by default.
- Auto mode reads codecs directly from FFmpeg stderr instead of waiting for an HLS segment.
- Duplicate channel-start requests are serialized and reuse an existing matching session.
- The web UI locks channel buttons while startup is in progress.

## Architecture

```text
Browser
  │
  ├── /api/categories ─┐
  ├── /api/channels ───┼──► SQLite catalogue cache
  └── /hls/<session>   │
          │             │
          ▼             │
     Xtream Online      │
      ├─ FastAPI        │
      ├─ background catalogue refresher ───► Xtream Player API
      ├─ SQLite config/cache
      └─ FFmpeg session manager ────────────► Xtream live stream
```

The browser never receives the upstream `/live/<username>/<password>/<stream-id>` URL.

## Playback modes

**Auto** opens the upstream in remux mode and reads the source codec declaration directly from FFmpeg's input metadata. H.264 + AAC stays on `-c copy`; incompatible video/audio combinations are immediately restarted as H.264 + AAC transcoding. Codec detection no longer waits for a complete HLS segment and never opens a separate upstream probe connection.

**Remux only** forces stream copy. This is the lowest CPU option and is ideal for H.264/AAC sources.

**Transcode** forces H.264/AAC encoding using FFmpeg. v0.1 defaults to software `libx264`; hardware-accelerated presets can be added later.

All playback is emitted as a short rolling HLS playlist. FFmpeg's HLS muxer is configured to delete old segments as playback advances, so live sessions do not grow indefinitely.

## Quick start with Docker Compose

Edit `docker-compose.yml` and change:

```yaml
image: ghcr.io/zeragonii/xtream-online:edge
```

Then:

```bash
docker compose up -d
```

Open:

```text
http://<docker-host-ip>:8080
```

Enter the provider server URL, username and password on the setup page. The credentials are stored in `/data/xtream-web.db` inside the persistent volume. The legacy filename is intentionally retained so existing v0.1 deployments upgrade without losing configuration.

### Portainer

The included `docker-compose.yml` can be pasted directly into a Portainer Stack after changing the GHCR image path.

## Environment configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_ACTIVE_STREAMS` | `1` | Maximum simultaneous FFmpeg/upstream sessions |
| `SESSION_IDLE_TIMEOUT` | `35` | Seconds without HLS requests before a session is killed |
| `SESSION_START_TIMEOUT` | `12` | Seconds allowed for FFmpeg to produce its first HLS playlist |
| `XTREAM_TIMEOUT` | `12` | Provider/API and FFmpeg HTTP timeout |
| `CODEC_PROBE_TIMEOUT` | `4` | Maximum seconds to wait for FFmpeg input codec metadata |
| `FFMPEG_MODE` | `auto` | UI/default mode: `auto`, `copy`, or `transcode` |
| `FFMPEG_VIDEO_ENCODER` | `libx264` | Video encoder used in transcode mode |
| `FFMPEG_VIDEO_PRESET` | `veryfast` | FFmpeg video preset |
| `FFMPEG_AUDIO_ENCODER` | `aac` | Audio encoder used in transcode mode |
| `PROVIDER_RELEASE_DELAY` | `0.5` | Seconds to wait before reconnecting when Auto switches from remux to transcode |
| `PROVIDER_CACHE_TTL` | `600` | Seconds to cache Player API auth/server metadata for playback |
| `CATALOG_REFRESH_INTERVAL` | `900` | Seconds between background category/channel catalogue refreshes |
| `M3U_TIMEOUT` | `6` | Timeout for last-resort `get.php` stream discovery |
| `XTREAM_STREAM_BASE_URL` | unset | Optional manual streaming base override for unusual providers |
| `HLS_TIME` | `2` | Target HLS segment length in seconds |
| `HLS_LIST_SIZE` | `12` | Number of HLS segments retained in the live playlist |

Provider credentials can also be supplied entirely through container environment variables:

```yaml
environment:
  XTREAM_URL: http://provider.example:8080
  XTREAM_USERNAME: username
  XTREAM_PASSWORD: password
  XTREAM_OUTPUT: ts
```

When all three required Xtream variables are present, the web UI cannot overwrite them.

## LAN-only deployment

The application contains no user authentication in v0.1 because it is designed for trusted local networks. Do **not** publish port 8080 to the internet as-is.

If the Docker host has multiple interfaces and you want to bind only to its LAN address, use for example:

```yaml
ports:
  - "192.168.1.202:8080:8080"
```

Replace that address with the actual LAN IP of your Docker host.

## Session behaviour

With the default:

```text
MAX_ACTIVE_STREAMS=1
```

starting a second channel stops the existing FFmpeg process before starting the new one. Stream startup is single-flight: duplicate requests for the same channel wait for and reuse the same session rather than opening competing provider connections. This is useful for providers/accounts limited to one concurrent stream.

The browser repeatedly requests the live HLS playlist while playing. Those requests update the session's last-access time. Once they stop for longer than `SESSION_IDLE_TIMEOUT`, the backend terminates FFmpeg and removes its temporary HLS files.

The UI also explicitly stops the session when the Stop button is pressed and makes a best-effort stop request when the browser tab closes.

## Build locally

The Docker build vendors hls.js into the image, so no third-party JavaScript CDN is required at runtime.

```bash
docker build -t xtream-online:dev .
docker run --rm -p 8080:8080 -v xtream-online-data:/data xtream-online:dev
```

## GitHub / GHCR workflow

### Main branch

Every push to `main` runs tests and publishes:

```text
ghcr.io/<owner>/<repo>:edge
```

GitHub's repository `GITHUB_TOKEN` is used to publish the package.

### Releases

The repo uses Release Please and Conventional Commits. Examples:

```text
fix: terminate ffmpeg when the playlist becomes idle
feat: add EPG timeline
feat!: replace the provider configuration schema
```

After changes land on `main`, Release Please maintains a release PR. Merging that release PR creates the semantic Git tag and GitHub Release. The same workflow then publishes stable container tags such as:

```text
ghcr.io/<owner>/<repo>:0.1.0
ghcr.io/<owner>/<repo>:0.1
ghcr.io/<owner>/<repo>:latest
```

The repository is bootstrapped so the first `feat:` release is intended to become **v0.1.0**.

In **Settings → Actions → General**, allow GitHub Actions to create pull requests if your repository settings require it. Release Please can use the built-in token, but if you want workflows to run automatically on Release Please-created PRs, add a repository secret named `RELEASE_PLEASE_TOKEN` containing a suitable GitHub token; the workflow will prefer it when present.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload --port 8080
```

A non-Docker local run also needs `ffmpeg` and `app/static/vendor/hls.min.js`. Docker is the supported v0.1 development/runtime path because the image installs FFmpeg and vendors hls.js automatically.

## Provider catalogue cache

The browser-facing category and channel APIs do **not** query the Xtream provider directly. A background service fetches `get_live_categories` and `get_live_streams`, then atomically replaces the persistent SQLite catalogue cache. The UI reads that cache immediately, including after container restarts. Manual refresh starts a background sync and leaves the current cached catalogue usable while it runs.

The channel API is paged (`offset`, `limit`) and supports server-side cached search (`q`) plus `category_id`, so the browser never needs to create tens of thousands of channel buttons at once.

## Provider API calls used in v0.1

Xtream Online currently uses the standard Player API operations:

```text
/player_api.php?username=...&password=...
/player_api.php?...&action=get_live_categories
/player_api.php?...&action=get_live_streams
/player_api.php?...&action=get_live_streams
```

For playback, Xtream Online reads and caches `server_info`, honours the provider's `allowed_output_formats`, and tries both common live URL layouts (`/live/<user>/<pass>/<id>` and `/<user>/<pass>/<id>`). The first successful route is learned in memory and tried first on later channel changes. If normal Xtream routes all fail, `get.php` M3U resolution is used only as a last-resort fallback. If an HTTPS API endpoint is fronted separately from the live transport, an HTTP/80 fallback is included automatically. All resolution remains server-side.

## Browser playback diagnostics

v0.1.3 reports browser-side playback state back to the application logs. This closes a gap where FFmpeg could be healthy and HLS segments could be served successfully while a browser autoplay, MSE or decoder failure remained invisible.

Useful Docker log events include:

```text
Browser event session=abcd1234 stream=11930 event=video-play-rejected detail=NotAllowedError: ...
Browser event session=abcd1234 stream=11930 event=hls-fatal detail=type=mediaError details=bufferAppendError ...
Browser event session=abcd1234 stream=11930 event=video-playing detail=readyState=4
```

A live session can also be inspected locally with:

```text
GET /api/session/<session-id>/diagnostics
```

The diagnostics response contains process state, source codecs, playlist/segment health and the tail of redacted FFmpeg stderr. It never returns the upstream provider URL or credentials.

If Auto mode successfully remuxes a source but hls.js reports a fatal browser media/decode error, the UI makes one automatic browser-safe retry in forced H.264/AAC transcode mode. Autoplay-policy failures are not transcoding failures: when `video.play()` is blocked, the UI tells the user to press the native Play control.

## Security notes

- Provider credentials never appear in the browser's playback URL.
- INFO logging for `httpx`/`httpcore` is suppressed so Player API query strings containing credentials are not written to ordinary container logs.
- Provider credentials stored through the UI are currently plaintext inside the SQLite database. Protect the `/data` volume accordingly.
- FFmpeg stderr is redacted before being surfaced by the application so the complete upstream URL is not returned to the browser.
- v0.1 is intended for a trusted LAN and does not implement application user accounts.

## v0.2 candidates

- XMLTV / Xtream EPG
- Current/next programme data
- Channel logos proxied through the backend
- Favourites
- Recently watched
- Better stream health/reconnect telemetry
- Optional hardware transcoding profiles


### Browser playback path (v0.1.4)

Xtream Online prefers **hls.js + MediaSource Extensions** on Chromium, Edge and Firefox. Native HLS is used only as a fallback for browsers with a genuine native HLS implementation (notably Safari). This avoids Chromium builds that advertise HLS via `canPlayType()` but fail to parse MPEG-TS HLS in the platform media pipeline.

The Docker logs now include a `player-path` browser event so the selected playback engine is visible during troubleshooting.
