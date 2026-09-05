# Changelog

Release Please maintains this file from Conventional Commit messages.

## Unreleased

- Fix live playback for one-connection Xtream accounts by probing local HLS output instead of opening a separate upstream `ffprobe` connection.
- Resolve provider-advertised stream protocol/ports and both common live URL layouts.
- Add M3U-based exact stream URL resolution and an HTTP/80 fallback for providers that separate HTTPS API traffic from live transport.
- Redact/suppress credential-bearing provider URLs from normal application logs.
- Rename the project to **Xtream Online**.
