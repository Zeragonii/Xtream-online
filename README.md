# Xtream Web

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

## Architecture

```text
Browser
  │
  ├── /api/categories
  ├── /api/channels
  └── /hls/<session>/index.m3u8
          │
          ▼
     Xtream Web
      ├─ FastAPI
      ├─ SQLite config
      └─ FFmpeg session manager
              │
              ▼
       Xtream provider
```

The browser never receives the upstream `/live/<username>/<password>/<stream-id>` URL.

## Playback modes

**Auto** runs `ffprobe` first. H.264 + AAC is remuxed with `-c copy`; other detected video/audio combinations are transcoded to H.264 + AAC for a conservative browser-compatible baseline. If probing fails, Auto chooses transcode rather than exposing the raw source.

**Remux only** forces stream copy. This is the lowest CPU option and is ideal for H.264/AAC sources.

**Transcode** forces H.264/AAC encoding using FFmpeg. v0.1 defaults to software `libx264`; hardware-accelerated presets can be added later.

All playback is emitted as a short rolling HLS playlist. FFmpeg's HLS muxer is configured to delete old segments as playback advances, so live sessions do not grow indefinitely.

## Quick start with Docker Compose

Edit `docker-compose.yml` and change:

```yaml
image: ghcr.io/YOUR_GITHUB_USERNAME/xtream-web:latest
```

Then:

```bash
docker compose up -d
```

Open:

```text
http://<docker-host-ip>:8080
```

Enter the provider server URL, username and password on the setup page. The credentials are stored in `/data/xtream-web.db` inside the persistent volume.

### Portainer

The included `docker-compose.yml` can be pasted directly into a Portainer Stack after changing the GHCR image path.

## Environment configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_ACTIVE_STREAMS` | `1` | Maximum simultaneous FFmpeg/upstream sessions |
| `SESSION_IDLE_TIMEOUT` | `35` | Seconds without HLS requests before a session is killed |
| `SESSION_START_TIMEOUT` | `15` | Seconds allowed for FFmpeg to produce its first HLS playlist |
| `XTREAM_TIMEOUT` | `15` | Provider/API and FFmpeg HTTP timeout |
| `FFPROBE_TIMEOUT` | `12` | Codec probe timeout |
| `FFMPEG_MODE` | `auto` | UI/default mode: `auto`, `copy`, or `transcode` |
| `FFMPEG_VIDEO_ENCODER` | `libx264` | Video encoder used in transcode mode |
| `FFMPEG_VIDEO_PRESET` | `veryfast` | FFmpeg video preset |
| `FFMPEG_AUDIO_ENCODER` | `aac` | Audio encoder used in transcode mode |
| `HLS_TIME` | `2` | Target HLS segment length in seconds |
| `HLS_LIST_SIZE` | `6` | Number of HLS segments retained in the live playlist |

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

starting a second channel stops the existing FFmpeg process before starting the new one. This is useful for providers/accounts limited to one concurrent stream.

The browser repeatedly requests the live HLS playlist while playing. Those requests update the session's last-access time. Once they stop for longer than `SESSION_IDLE_TIMEOUT`, the backend terminates FFmpeg and removes its temporary HLS files.

The UI also explicitly stops the session when the Stop button is pressed and makes a best-effort stop request when the browser tab closes.

## Build locally

The Docker build vendors hls.js into the image, so no third-party JavaScript CDN is required at runtime.

```bash
docker build -t xtream-web:dev .
docker run --rm -p 8080:8080 -v xtream-web-data:/data xtream-web:dev
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

A non-Docker local run also needs `ffmpeg`, `ffprobe`, and `app/static/vendor/hls.min.js`. Docker is the supported v0.1 development/runtime path because the image installs FFmpeg and vendors hls.js automatically.

## Provider API calls used in v0.1

Xtream Web currently uses the standard Player API operations:

```text
/player_api.php?username=...&password=...
/player_api.php?...&action=get_live_categories
/player_api.php?...&action=get_live_streams
/player_api.php?...&action=get_live_streams&category_id=...
```

The live input URL is constructed server-side from the configured provider and stream ID.

## Security notes

- Provider credentials never appear in the browser's playback URL.
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
