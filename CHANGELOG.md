# Changelog

Release Please maintains this file from Conventional Commit messages.

## Unreleased (v0.1.4)

- Prefer hls.js/MSE over Chromium's misleading native HLS capability report.
- Keep native HLS as a fallback for browsers with genuine native HLS support.
- Increase the default live HLS playlist from 6 to 12 segments and retain more expired segments to reduce recovery-time 404s.
- Log the selected browser playback path (`hls.js/MSE` or `native-HLS fallback`).

- Stop silently swallowing browser `video.play()` failures; show an explicit “press Play” state when autoplay is blocked.
- Report browser-side hls.js and media-element failures back into the Docker logs without exposing provider credentials.
- Add `GET /api/session/<id>/diagnostics` with FFmpeg process state, HLS file health, segment counts and redacted FFmpeg stderr.
- Follow the canonical hls.js MediaSource attachment lifecycle before loading the HLS playlist.
- Automatically retry Auto-mode remux sessions in forced H.264/AAC transcode mode when the browser reports a fatal media/decode error.
- Surface player buffering, playing, stalled, network recovery and fatal HLS states in the UI.

## v0.1.2

- Make channel startup single-flight so duplicate clicks/requests reuse one FFmpeg session instead of racing and evicting each other.
- Disable channel buttons while a stream is opening and explicitly release the previous session before changing channels.
- Detect source codecs directly from FFmpeg input metadata instead of waiting for a complete HLS segment and running a local ffprobe pass.
- Learn the provider route that successfully played and try that route first on subsequent channel changes.
- Cache Player API authentication/server information for the playback hot path.
- Move expensive `get.php` M3U scanning to a last-resort fallback instead of doing it on every channel click.
- Add detailed startup timing logs for candidate preparation, codec detection, FFmpeg startup and the complete playback request.
- Reduce the default provider reconnect delay from 2.0 seconds to 0.5 seconds.

## v0.1.1

- Fix live playback for one-connection Xtream accounts by avoiding a separate upstream `ffprobe` connection.
- Resolve provider-advertised stream protocol/ports and both common live URL layouts.
- Add M3U-based exact stream URL resolution and an HTTP/80 fallback for providers that separate HTTPS API traffic from live transport.
- Redact/suppress credential-bearing provider URLs from normal application logs.
- Rename the project to **Xtream Online**.
