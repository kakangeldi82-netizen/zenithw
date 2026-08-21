
🔗 **Live:** [zenithw.space](https://zenithw.space)

🏷️ **Current release:** `v12.9` — streamlined tool modals, lighter rendering, and a more consistent interface
🏷️ **Current release:** `v13.0` — rebuilt YouTube delivery, bounded temporary storage, native browser handoff, and cacheable frontend assets

---

@@ -16,6 +16,7 @@
- [Getting Started](#getting-started)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Deployment Model](#deployment-model)
- [Security](#security)
- [Release History](#release-history)
- [Legal](#legal)
@@ -29,17 +30,18 @@
- 🎬 **Multi-platform support** — YouTube, TikTok, Instagram, X/Twitter, Reddit, and many other sources (powered by yt-dlp)
- 🎵 **Video or audio** — video formats such as mp4/webm/mkv, audio formats such as mp3/flac/wav/ogg/opus/m4a
- 🔇 **Mute mode** — download video without an audio track
- 📃 **Batch / playlist downloads** — process multiple links in a single request (up to 50 items)
- 📃 **Batch / playlist downloads** — process up to 10 pasted links or inspect playlists containing up to 50 items
- ⏭️ **SponsorBlock integration** — automatically skip or strip sponsor segments, intros, outros, and more
- 🖼️ **Thumbnail downloads** — fetch cover art alongside media or on its own
- 📝 **Subtitle and metadata support** — download available subtitles and embed video metadata
- 🔄 **Converter** — convert existing files between formats (powered by FFmpeg)
- 🎞️ **Remux** — change container format without re-encoding
- 🔄 **Converter** — remux compatible streams first and re-encode only when the requested output requires it
- 🎞️ **Real remux** — change a compatible container with FFmpeg stream copy instead of returning the original file unchanged
- 🌍 **4 languages supported** — Turkish, English, French, German
- 🎨 **Customizable UI** — light/dark themes, accent colors, smooth animations
- 📱 **PWA support** — installable to the home screen and usable like a native app
- 🔒 **No server-side history** — download history is stored locally on-device (localStorage) only
- ⚡ **Real-time progress** — live download status via Socket.IO
- 💾 **Bounded temporary storage** — aggregate spool, minimum-free-disk, prepared-file lifetime, and transfer concurrency limits protect the server
- 🚀 **Native browser handoff** — completed media is exposed through a short-lived, single-use download URL instead of being copied into a large JavaScript Blob

> **Note:** aria2 multi-connection acceleration is currently disabled for security reasons (SSRF protection). It may be re-enabled in the future with proper network isolation.

@@ -63,14 +65,14 @@

```
zenithw/
├── .gitignore                   # Keeps local notes and backend tests out of Git
├── .github/
│   └── dependabot.yml
├── .vscode/
├── backend/
│   ├── downloads/               # Temporary download/processing files (runtime)
│   ├── app.py                   # Flask API — /info, /download, /convert, /thumbnail, /health, /cancel
│   ├── test_security.py         # SSRF, rate-limit, and downloader security regression tests
│   ├── app.py                   # Flask API — metadata, media jobs, native file handoff, health, cancellation
│   ├── requirements.txt
│   ├── requirements-dev.txt     # Optional local/CI tooling; excluded from production installs
│   ├── nixpacks.toml            # Railway build config (includes ffmpeg)
│   ├── Procfile                 # Gunicorn + geventwebsocket worker
│   └── .gitignore
@@ -87,7 +89,9 @@ zenithw/
│   ├── vs-cobalt-tools.html
│   ├── vs-savefrom.html
│   ├── vs-y2mate.html
│   ├── style.css
│   ├── app.[content-hash].js    # Cacheable landing-page application code
│   ├── updates.[content-hash].js
│   ├── style.[content-hash].css
│   ├── version.js               # Single source of truth for version info
│   ├── zenithw.png
│   ├── robots.txt
@@ -137,12 +141,12 @@ For local frontend development, open the files in the `frontend/` directory or s
| `PORT` | No (default `5000`) | Port the server listens on |
| `SECRET_KEY` | **Yes** (production) | Flask secret key. A random value is generated only in development mode |
| `YOUTUBE_COOKIES` | No | Browser-exported `cookies.txt` content used to bypass YouTube bot protection |
| `YOUTUBE_POT_PROVIDER_URL` | No | Private Railway base URL for the YouTube PO Token provider; only private Railway hosts or local loopback are accepted |
| `FLASK_ENV` / `ALLOW_DEV_CORS` | No | Set to `development` or `1` to allow local origins via CORS |
| `ORIGIN_SECRET` | **Yes** when origin lock is enabled | Shared secret injected by Cloudflare in the `X-Origin-Verify` header |
| `ENABLE_ORIGIN_LOCK` | No (default enabled) | Set to `0` only when intentionally disabling the origin-secret check |
| `TRUST_PROXY` | No (default enabled) | Trust Cloudflare/proxy client-IP headers; disable when serving the backend directly |
| `ARIA2_ENABLED` | No | Currently ignored — aria2 is disabled for SSRF protection |
| `SPONSORBLOCK_ENABLED` | No | Set to `1` to enable SponsorBlock processing by default |
| `DOWNLOAD_TIMEOUT_SECONDS` | No (default `600`) | Maximum time allowed for a single download |
| `FFMPEG_TIMEOUT_SECONDS` | No (default `120`) | Maximum runtime for a single FFmpeg conversion or mute operation |
| `MAX_CONCURRENT_DOWNLOADS` | No (default `2`) | Global concurrent download limit |
@@ -153,6 +157,15 @@ For local frontend development, open the files in the `frontend/` directory or s
| `MAX_VIDEO_DURATION_SECONDS` | No (default `5400`) | Maximum allowed video length (90 minutes) |
| `MAX_DOWNLOAD_SIZE_MB` | No (default `1536`) | Maximum allowed file size in MB |
| `MAX_CONVERT_OUTPUT_SIZE_MB` | No (default `1024`) | Maximum converted output size in MB |
| `MAX_SPOOL_SIZE_MB` | No (default `4096`) | Aggregate temporary/prepared-file storage budget in MB |
| `MIN_FREE_DISK_MB` | No (default `512`) | Minimum free disk space preserved while accepting and running jobs |
| `DOWNLOAD_SPOOL_RESERVATION_MB` | No (default `2048`) | Capacity reserved for each accepted download job |
| `MAX_CONCURRENT_TRANSFERS` | No (default `2`) | Maximum simultaneous prepared-file transfers |
| `MAX_CONCURRENT_TRANSFERS_PER_IP` | No (default `2`) | Simultaneous prepared-file transfers allowed per IP |
| `TRANSFER_QUEUE_WAIT_SECONDS` | No (default `120`) | Maximum wait for a native transfer slot |
| `PREPARED_FILE_TTL` | No (default `600`) | Lifetime of an unused prepared download token, in seconds |
| `INFO_CACHE_TTL_SECONDS` | No (default `45`) | Short metadata response-cache lifetime |
| `INFO_CACHE_MAX_SIZE` | No (default `256`) | Maximum number of sanitized metadata responses retained in memory |
| `CONVERSION_RATE_LIMIT_WINDOW` | No (default `600`) | Per-IP conversion quota window in seconds |
| `CONVERSION_RATE_LIMIT_MAX_REQUESTS` | No (default `2`) | Conversions allowed per IP during the conversion quota window |

@@ -163,23 +176,36 @@ For local frontend development, open the files in the `frontend/` directory or s
| Endpoint | Method | Description |
|---|---|---|
| `/info` | POST | Returns video/playlist metadata for a given URL |
| `/download` | POST | Downloads and streams the requested video/audio |
| `/download` | POST | Runs one download/extraction job and returns a short-lived native download URL |
| `/files/<token>` | GET / HEAD | Transfers a prepared file through a single-use, IP-bound token |
| `/thumbnail` | POST | Downloads the cover image for a given URL |
| `/convert` | POST | Converts an uploaded file to the target format |
| `/convert` | POST | Remuxes or converts an uploaded file and returns a native download URL |
| `/cancel` | POST | Cancels an in-progress download |
| `/health` | GET | Reports server, FFmpeg, cookie status, and queue info |
| `/health` | GET | Reports dependencies, queues, disk/transfer budgets, caches, and the single-worker deployment boundary |

All endpoints (except `/health`) are rate-limited to **10 requests per minute per IP**. `/convert` additionally defaults to **2 conversions per 10 minutes per IP**.

---

## Deployment Model

The backend intentionally runs as **one Gunicorn worker and one Railway replica**. Rate limits, semaphores, cancellation events, Socket.IO identifiers, metadata cache entries, prepared-file tokens, and temporary files are currently process-local.

Do not increase `--workers` or add replicas without first moving shared coordination to Redis (or an equivalent shared store), configuring a Socket.IO message queue and sticky routing, and placing prepared files in shared/object storage. `/health` reports `horizontal_scaling_safe: false` and `expected_gunicorn_workers: 1` so deployment checks can detect this boundary.

Vertical scaling is safe when measurements show the current instance needs more CPU, memory, or disk headroom.

---

## Security

- CORS is restricted to `zenithw.space` (and localhost in development)
- Cloudflare origin lock requires a timing-safe `X-Origin-Verify` shared-secret match in production
- Uploaded files are sanitized and restricted to an allow-list of extensions
- `SECRET_KEY` is required in production; no insecure hardcoded fallback
- Downloaded/converted files are automatically deleted shortly after processing
- Prepared files use short-lived, single-use, IP-bound tokens and separate transfer concurrency limits
- Aggregate spool reservations and a minimum-free-disk watermark prevent unbounded temporary-file growth
- All file path inputs are validated against path traversal attacks
- `ffmpeg` subprocesses run with fixed argument lists; user input is never passed to a shell
- SSRF protection blocks private/reserved destinations during URL validation, high-level connections, and low-level socket connections
@@ -194,18 +220,11 @@ All endpoints (except `/health`) are rate-limited to **10 requests per minute pe

Found a vulnerability? Please report it to [info@zenithw.space](mailto:info@zenithw.space).

Security regression tests cover private-network socket blocking, unsafe media protocols, downloader configuration, and independent rate-limit quotas:

```bash
cd backend
pytest -q
```

---

## Release History

The full bilingual changelog is available at [zenithw.space/updates.html](https://zenithw.space/updates.html). The v11 series covers the security hardening work from socket-level SSRF protection in v11.0 through the documented and regression-tested v11.7 release. The v12 series follows with comparison and legal-page redesigns, complete theme profiles, settings and performance improvements, and streamlined convert, remux, and support tools through v12.9.
The full bilingual changelog is available at [zenithw.space/updates.html](https://zenithw.space/updates.html). The v11 series covers the security-hardening work, and v12 focuses on presentation, themes, settings, and rendering improvements. v13.0 rebuilds YouTube delivery around the PO Token provider and modern EJS/Deno support, adds bounded spool/native-transfer behavior, corrects bulk success accounting and mute planning, and introduces real remux-first media processing.

---
