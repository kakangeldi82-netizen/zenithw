from gevent import monkey
monkey.patch_all()

from flask import Flask, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
import yt_dlp
import os
import uuid
import threading
import time
import subprocess
import secrets
from collections import defaultdict
import gevent
import tempfile

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path='/static')

# Sabit bir fallback yerine rastgele üretilen bir secret key kullanılır;
# env variable verilmezse her başlatmada yeni bir tane üretilir.
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# /convert için maksimum upload boyutu (230 MB)
app.config['MAX_CONTENT_LENGTH'] = 230 * 1024 * 1024

# ── İzin verilen origin'ler ────────────────────────────
# Yerel geliştirmede FLASK_ENV=development veya ALLOW_DEV_CORS=1 verilirse
# localhost origin'lerine de izin verilir.
ALLOWED_ORIGINS = ["https://zenithw.space", "https://www.zenithw.space"]
if os.environ.get("FLASK_ENV") == "development" or os.environ.get("ALLOW_DEV_CORS"):
    ALLOWED_ORIGINS += ["http://localhost:5000", "http://127.0.0.1:3000"]

CORS(app, resources={r"/*": {"origins": ALLOWED_ORIGINS}})
socketio = SocketIO(app, cors_allowed_origins=ALLOWED_ORIGINS, async_mode='gevent')

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── History ───────────────────────────────────────────
# NOT: Geçmiş artık sunucuda DEĞİL, kullanıcının kendi tarayıcısında
# (localStorage) tutuluyor. Böylece her kullanıcı sadece kendi geçmişini
# görür ve sunucu tarafında ortak/karışan bir history.json dosyasına
# gerek kalmıyor. Bu yüzden add_to_history çağrıları artık no-op.

def add_to_history(url, title, platform, fmt, success=True):
    pass

# ── Cookies ───────────────────────────────────────────
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

# Railway environment variable'dan cookies yükle
cookies_env = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES") or os.environ.get("YOUTUBE_COOKIE")
if cookies_env:
    try:
        # Satır sonlarını düzgün yorumla (bazı paneller \n karakterini düz metin olarak kaydeder)
        cookies_content = cookies_env.replace('\\n', '\n').strip()
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(cookies_content)
        print(f"[INIT] cookies.txt Railway environment variable'dan yazıldı ✓ ({len(cookies_content)} bytes)")
    except Exception as e:
        print(f"[INIT] ⚠️ cookies.txt yazılamadı: {e}")
elif os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
    print(f"[INIT] cookies.txt bulundu ✓ ({os.path.getsize(COOKIES_FILE)} bytes)")
else:
    print("[INIT] ⚠️ cookies.txt bulunamadı veya geçersiz - YouTube indirme kısıtlı olabilir")

# ── Rate limiting ─────────────────────────────────────
rate_limit_data = defaultdict(list)
rate_limit_lock = threading.Lock()

def check_rate_limit(ip):
    now = time.time()
    with rate_limit_lock:
        rate_limit_data[ip] = [t for t in rate_limit_data[ip] if now - t < 60]
        if len(rate_limit_data[ip]) >= 10:
            return False
        rate_limit_data[ip].append(now)
        return True

def cleanup_rate_limit_data():
    """Artık istek atmayan IP'lerin boş listelerini sözlükten sil.
    Aksi halde her yeni IP kalıcı olarak dict içinde birikir (hafıza sızıntısı)."""
    now = time.time()
    with rate_limit_lock:
        stale_ips = [
            ip for ip, timestamps in rate_limit_data.items()
            if not timestamps or now - max(timestamps) > 60
        ]
        for ip in stale_ips:
            del rate_limit_data[ip]

def get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

# ── FFmpeg ────────────────────────────────────────────
def find_ffmpeg():
    try:
        result = subprocess.run(['which', 'ffmpeg'], capture_output=True, text=True)
        path = result.stdout.strip()
        if path: return os.path.dirname(path)
    except: pass
    for p in ['/usr/bin', '/usr/local/bin', '/root/.nix-profile/bin', '/nix/store', '/opt/venv/bin']:
        if os.path.exists(os.path.join(p, 'ffmpeg')): return p
    return None

FFMPEG_DIR = find_ffmpeg()
print(f"[INIT] ffmpeg={FFMPEG_DIR}")

def find_aria2():
    try:
        result = subprocess.run(['which', 'aria2c'], capture_output=True, text=True)
        path = result.stdout.strip()
        if path: return path
    except: pass
    for p in ['/usr/bin/aria2c', '/usr/local/bin/aria2c', '/root/.nix-profile/bin/aria2c']:
        if os.path.exists(p): return p
    return None

ARIA2_PATH = find_aria2()
print(f"[INIT] aria2c={ARIA2_PATH or 'yok'}")

# ── Temizlik ──────────────────────────────────────────
def cleanup_old_files():
    try:
        now = time.time()
        for f in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 1800:
                os.remove(fpath)
    except: pass

def periodic_cleanup():
    while True:
        time.sleep(900)
        cleanup_old_files()
        cleanup_rate_limit_data()

threading.Thread(target=periodic_cleanup, daemon=True).start()

# ── İptal ─────────────────────────────────────────────
cancel_events = {}
cancel_events_lock = threading.Lock()

# ── Platform helpers ──────────────────────────────────
def is_youtube(u): return "youtube.com" in u or "youtu.be" in u
def is_tiktok(u): return "tiktok.com" in u
def is_instagram(u): return "instagram.com" in u
def is_youtube_live_url(u): return is_youtube(u) and "/live/" in u

# yt-dlp'nin native extractor'ı olmayan, "generic" extractor'a düşüp
# sahte-başarı (ör. og:image'i video sanıp) dönebilen bilinen platformlar.
# Bunları yt-dlp'ye hiç sormadan erkenden reddediyoruz.
UNSUPPORTED_DOMAINS = (
    "spotify.com", "music.apple.com", "deezer.com", "tidal.com",
    "music.amazon.com", "music.youtube.com",
)

def is_unsupported_domain(u):
    ul = u.lower()
    return any(d in ul for d in UNSUPPORTED_DOMAINS)

# ── Audio-only formatlar ──────────────────────────────
AUDIO_FMTS = {"mp3", "flac", "wav", "ogg", "opus", "m4a"}

# ── Hata mesajları ────────────────────────────────────
def parse_error(error_msg, url):
    es = error_msg.lower()
    if is_youtube(url):
        if "sign in" in es or "login" in es or "bot" in es:
            return "YouTube bot koruması aktif. Birkaç dakika bekleyip tekrar deneyin."
        if "private" in es:
            return "Bu YouTube videosu gizli, indirilemez."
        if "copyright" in es:
            return "Bu video telif hakkı nedeniyle indirilemez."
        if "age" in es:
            return "Bu video yaş kısıtlamalı. Çerez güncellemesi gerekebilir."
        if "unavailable" in es or "not available" in es:
            return "Bu YouTube videosu artık mevcut değil."
        if "live" in es:
            return "Canlı yayınlar desteklenmez."
        if "format" in es:
            return "İstenen format bulunamadı. Farklı kalite deneyin."
        return "YouTube indirme başarısız. Birkaç dakika sonra tekrar deneyin."
    if is_instagram(url):
        if "rate" in es or "429" in es:
            return "instagram_ratelimit"
        if "login" in es:
            return "Bu Instagram içeriği gizli veya giriş gerektiriyor."
        return "Instagram indirme başarısız. Birkaç dakika sonra tekrar deneyin."
    if is_tiktok(url):
        if "private" in es:
            return "Bu TikTok videosu gizli."
        return "TikTok indirme başarısız."
    if "unsupported url" in es:
        return "Bu URL desteklenmiyor. Desteklenen platformları kontrol edin."
    if "no video formats" in es:
        return "Bu içerik için uygun format bulunamadı."
    if "network" in es or "connection" in es:
        return "Bağlantı hatası. İnternet bağlantınızı kontrol edin."
    return error_msg[:200]

# ── Base opts ─────────────────────────────────────────
def get_base_opts(url, use_cookies=True):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if ARIA2_PATH:
        # aria2 çoklu bağlantı ile indirme hızını artırır; yt-dlp'nin
        # dahili indiricisine göre büyük dosyalarda belirgin fark yaratır.
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }
    if use_cookies and os.path.exists(COOKIES_FILE) and not is_instagram(url):
        opts["cookiefile"] = COOKIES_FILE
    if is_youtube(url):
        opts["extractor_args"] = {
            "youtube": {
                # 'ios' bilerek çıkarıldı: iOS client cookie kullanmıyor
                # (OAuth tabanlı), yani cookiefile verilse bile sessizce
                # yok sayılıyor ve cookie güvenilirliği düşüyor.
                #
                # 'android_vr' EKLENDİ: web/mweb client'ları yüksek kaliteli
                # (720p+) DASH formatlarının indirme URL'sini çözmek için
                # YouTube'un nsig (signature) şifresini çözecek bir JS
                # runtime istiyor. Runtime yoksa/başarısız olursa bu
                # formatlar sessizce elenip elde kalan en garanti format
                # (itag 18, 360p muxed) seçiliyor - kalite düşüşünün asıl
                # sebebi buydu. android_vr client'ı nsig gerektirmeden
                # yüksek kaliteli formatlara erişebiliyor, bu yüzden listeye
                # eklendi ve öne alındı.
                "player_client": ["android_vr", "web", "mweb", "android"],
            }
        }
    return opts

def get_opts_list(url, extra=None):
    opts_list = []
    # Çerezli deneme
    o = get_base_opts(url, use_cookies=True)
    if extra: o.update(extra)
    opts_list.append(o)
    # Çerezsiz deneme (fallback)
    o = get_base_opts(url, use_cookies=False)
    if extra: o.update(extra)
    opts_list.append(o)
    return opts_list

# ── Format string builder ─────────────────────────────
def build_format_str(url, quality, fmt, codec):
    if fmt in AUDIO_FMTS:
        return "bestaudio/best"
    q = str(quality)
    best = (q == "9999")
    if is_youtube(url):
        if codec == "av1":
            if best:
                return ("bestvideo[vcodec^=av01]+bestaudio[acodec^=opus]"
                        "/bestvideo[vcodec^=av01]+bestaudio/bestvideo+bestaudio/best")
            return (f"bestvideo[vcodec^=av01][height<={q}]+bestaudio[acodec^=opus]"
                    f"/bestvideo[vcodec^=av01][height<={q}]+bestaudio"
                    f"/bestvideo[height<={q}]+bestaudio/best[height<={q}]/best")
        elif codec == "vp9":
            if best:
                return ("bestvideo[vcodec^=vp9]+bestaudio[acodec^=opus]"
                        "/bestvideo[vcodec^=vp9]+bestaudio/bestvideo+bestaudio/best")
            return (f"bestvideo[vcodec^=vp9][height<={q}]+bestaudio[acodec^=opus]"
                    f"/bestvideo[vcodec^=vp9][height<={q}]+bestaudio"
                    f"/bestvideo[height<={q}]+bestaudio/best[height<={q}]/best")
        else: # h264
            if best:
                return "bestvideo+bestaudio/best"
            return (f"bestvideo[height<={q}]+bestaudio"
                    f"/best[height<={q}]/best")
    # Non-YouTube
    if best:
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    return (f"bestvideo[ext=mp4][height<={q}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={q}]+bestaudio/best[height<={q}]/best")

# ── Routes ────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "ffmpeg": f"OK ({FFMPEG_DIR})" if FFMPEG_DIR else "MISSING",
        "cookies": f"✓ Yüklü ({os.path.getsize(COOKIES_FILE)} bytes)" if os.path.exists(COOKIES_FILE) else "✗ Yok",
        "disk_files": len(os.listdir(DOWNLOAD_DIR)),
    }), 200

@app.route("/robots.txt")
def robots():
    p = os.path.join(FRONTEND_DIR, "robots.txt")
    if os.path.exists(p):
        return send_from_directory(FRONTEND_DIR, 'robots.txt', mimetype='text/plain')
    return "User-agent: *\nAllow: /\n", 200, {'Content-Type': 'text/plain'}

@app.route("/sitemap.xml")
def sitemap():
    p = os.path.join(FRONTEND_DIR, "sitemap.xml")
    if os.path.exists(p):
        return send_from_directory(FRONTEND_DIR, 'sitemap.xml', mimetype='application/xml')
    return "", 404

@app.route("/cancel", methods=["POST"])
def cancel_route():
    data = request.json or {}
    download_id = data.get("download_id", "")
    if download_id:
        with cancel_events_lock:
            ev = cancel_events.get(download_id)
            if ev:
                ev.set()
        return jsonify({"ok": True}), 200
    return jsonify({"error": "download_id gerekli"}), 400

@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    blocked = {"info", "download", "cancel", "health", "convert", "thumbnail", "robots.txt", "sitemap.xml"}
    if path and path not in blocked:
        full = os.path.join(FRONTEND_DIR, path)
        if os.path.exists(full):
            return send_from_directory(FRONTEND_DIR, path)
    return send_from_directory(FRONTEND_DIR, "index.html")

# ── /info ─────────────────────────────────────────────
@app.route("/info", methods=["POST"])
def get_info():
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Çok fazla istek. 1 dakika bekleyin."}), 429
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    if is_youtube_live_url(url):
        return jsonify({"error": "Canlı yayınlar şu anda desteklenmiyor."}), 400
    if is_unsupported_domain(url):
        return jsonify({"error": "Bu platform desteklenmiyor. Desteklenen platformları kontrol edin."}), 400

    # Playlist olabilecek bir URL ise hızlı (flat) çıkarım kullan ve
    # videoyu 50 ile sınırla, yoksa çok büyük playlistler sunucuyu
    # uzun süre kilitleyebilir.
    PLAYLIST_LIMIT = 50
    extra_opts = {
        "extract_flat": "in_playlist",
        "playlistend": PLAYLIST_LIMIT,
    }
    opts_list = get_opts_list(url, extra=extra_opts)
    last_err = None
    for opts in opts_list:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # ── Playlist tespiti ──────────────────────
                if info.get("_type") == "playlist" or "entries" in info:
                    entries = info.get("entries") or []
                    items = []
                    for e in entries:
                        if not e:
                            continue
                        entry_url = e.get("url") or e.get("webpage_url")
                        if not entry_url and e.get("id"):
                            if is_youtube(url):
                                entry_url = f"https://www.youtube.com/watch?v={e['id']}"
                        if not entry_url:
                            continue
                        thumbs = e.get("thumbnails") or []
                        thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
                        items.append({
                            "url": entry_url,
                            "title": e.get("title") or "Video",
                            "duration": e.get("duration") or 0,
                            "thumbnail": thumb,
                        })
                    return jsonify({
                        "is_playlist": True,
                        "playlist_title": info.get("title") or "Playlist",
                        "playlist_count": len(items),
                        "items": items,
                        "platform": info.get("extractor_key", "").lower(),
                    })
                subs = info.get("subtitles") or {}
                auto_subs = info.get("automatic_captions") or {}
                sub_langs = sorted(set(subs.keys()) | set(auto_subs.keys()))
                return jsonify({
                    "is_playlist": False,
                    "title": info.get("title") or "Video",
                    "duration": info.get("duration") or 0,
                    "thumbnail": info.get("thumbnail"),
                    "uploader": info.get("uploader") or info.get("channel") or "",
                    "platform": info.get("extractor_key", "").lower(),
                    "subtitles": sub_langs,
                    "has_manual_subtitles": bool(subs),
                })
        except Exception as e:
            last_err = e
            es = str(e).lower()
            if "login" in es or "private" in es or "cookie" in es:
                break
            continue

    error_msg = str(last_err) if last_err else "Bilinmeyen hata"
    print(f"[INFO ERR] {url[:60]}: {error_msg[:150]}")
    parsed = parse_error(error_msg, url)
    if parsed == "instagram_ratelimit":
        return jsonify({"error": "instagram_ratelimit"}), 400
    return jsonify({"error": parsed}), 400

# ── /download ─────────────────────────────────────────
@app.route("/download", methods=["POST"])
def download():
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Çok fazla istek. 1 dakika bekleyin."}), 429
    cleanup_old_files()
    data = request.json or {}
    url = data.get("url", "").strip()
    quality = str(data.get("quality", "1080"))
    fmt = data.get("format", "mp4").lower()
    codec = data.get("codec", "h264").lower()
    audio_q = str(data.get("audioQ", "256"))
    sid = data.get("sid", "")
    download_id = data.get("download_id") or str(uuid.uuid4())
    add_meta = bool(data.get("metadata", True))

    # ── Altyazı seçenekleri ──
    want_subs = bool(data.get("subtitles", False))
    sub_langs = data.get("sub_langs") or ["en"]
    if isinstance(sub_langs, str):
        sub_langs = [sub_langs]
    # Basit whitelist: sadece dil kodu formatına uyanları kabul et (ör. "en", "tr", "pt-BR")
    sub_langs = [l for l in sub_langs if isinstance(l, str) and 1 <= len(l) <= 10 and all(c.isalnum() or c == '-' for c in l)][:5]
    embed_subs = bool(data.get("embed_subs", True))

    # ── SponsorBlock seçenekleri ──
    want_sponsorblock = bool(data.get("sponsorblock", False))
    ALLOWED_SB_CATEGORIES = {
        "sponsor", "intro", "outro", "selfpromo", "preview",
        "filler", "interaction", "music_offtopic", "poi_highlight",
    }
    sb_categories = data.get("sponsorblock_categories") or ["sponsor"]
    if not isinstance(sb_categories, list):
        sb_categories = ["sponsor"]
    sb_categories = [c for c in sb_categories if c in ALLOWED_SB_CATEGORIES][:9] or ["sponsor"]
    sb_mode = data.get("sponsorblock_mode", "remove")
    if sb_mode not in ("remove", "mark"):
        sb_mode = "remove"

    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    if is_youtube_live_url(url):
        return jsonify({"error": "Canlı yayınlar şu anda desteklenmiyor."}), 400
    if is_unsupported_domain(url):
        return jsonify({"error": "Bu platform desteklenmiyor. Desteklenen platformları kontrol edin."}), 400

    is_audio = fmt in AUDIO_FMTS
    cancel_event = threading.Event()
    with cancel_events_lock:
        cancel_events[download_id] = cancel_event

    filename = str(uuid.uuid4())
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    def progress_hook(d):
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("İptal edildi")
        if d['status'] == 'downloading' and sid:
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            if total > 0:
                pct = max(5, int(downloaded / total * 82))
                socketio.emit('progress', {
                    'percent': pct,
                    'speed': d.get('_speed_str', '').strip(),
                    'eta': d.get('_eta_str', '').strip(),
                    'status': 'downloading'
                }, room=sid)
        elif d['status'] == 'finished' and sid:
            socketio.emit('progress', {'percent': 88, 'status': 'merging'}, room=sid)

    try:
        fmt_str = build_format_str(url, quality, fmt, codec)
        print(f"[DL] q={quality} fmt={fmt} codec={codec} audio={is_audio}")

        if is_audio:
            if not FFMPEG_DIR:
                return jsonify({"error": "Ses dönüşümü için FFmpeg gerekli."}), 400
            codec_map = {
                "mp3": "mp3", "flac": "flac", "wav": "wav",
                "ogg": "vorbis", "opus": "opus", "m4a": "m4a"
            }
            preferred = codec_map.get(fmt, "mp3")
            preferred_q = audio_q if fmt in ("mp3", "ogg", "m4a") else "0"
            postprocessors = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": preferred,
                    "preferredquality": preferred_q
                }
            ]
            if add_meta:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
            if want_sponsorblock:
                postprocessors.append({
                    "key": "SponsorBlock",
                    "categories": sb_categories,
                    "api": "https://sponsor.ajay.app",
                })
                if sb_mode == "remove":
                    postprocessors.append({
                        "key": "ModifyChapters",
                        "remove_sponsor_segments": sb_categories,
                    })
            extra = {
                "format": fmt_str,
                "outtmpl": filepath + ".%(ext)s",
                "progress_hooks": [progress_hook],
                "postprocessors": postprocessors,
            }
        else:
            # Mute mod
            is_mute = data.get("mute", False)
            merge_fmt = "mp4"
            if fmt == "webm": merge_fmt = "webm"
            elif fmt == "mkv": merge_fmt = "mkv"
            elif fmt == "avi": merge_fmt = "avi"
            elif fmt == "mov": merge_fmt = "mov"
            elif codec in ("av1", "vp9") and fmt != "mp4": merge_fmt = "webm"
            # NOT: Video-only format seçicisi (build_mute_format_str) artık
            # burada KULLANILMIYOR. Sebep: YouTube, PO Token olmadan ayrı
            # video-only akışları çoğu client'ta listeden düşürüyor, bu da
            # bazı videolarda "format not available" hatasına yol açıyor.
            # Bunun yerine normal (sesli) format indirilip, indirme
            # tamamlandıktan sonra ffmpeg ile ses izi kesiliyor (aşağıda,
            # full_path belirlendikten sonra). Bu yöntem PO Token'a bağımlı
            # değil ve her zaman çalışır.
            postprocessors = []
            if add_meta:
                postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
            if want_sponsorblock:
                postprocessors.append({
                    "key": "SponsorBlock",
                    "categories": sb_categories,
                    "api": "https://sponsor.ajay.app",
                })
                if sb_mode == "remove":
                    postprocessors.append({
                        "key": "ModifyChapters",
                        "remove_sponsor_segments": sb_categories,
                    })
            extra = {
                "format": fmt_str,
                "outtmpl": filepath + ".%(ext)s",
                "progress_hooks": [progress_hook],
                "merge_output_format": merge_fmt,
            }
            if want_subs and FFMPEG_DIR:
                # Not: Şu an sadece videoya gömülü altyazı destekleniyor.
                # Ayrı .srt indirmek, mevcut tek-dosya seçim mantığıyla
                # (dosya adı prefix eşleşmesi) çakışacağından desteklenmiyor.
                extra["writesubtitles"] = True
                extra["writeautomaticsub"] = True
                extra["subtitleslangs"] = sub_langs
                extra["subtitlesformat"] = "srt/best"
                postprocessors.append({"key": "FFmpegEmbedSubtitle"})
            if postprocessors:
                extra["postprocessors"] = postprocessors

        opts_list = get_opts_list(url, extra=extra)
        success = False
        last_err = None
        for opts in opts_list:
            if cancel_event.is_set():
                break
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                success = True
                break
            except yt_dlp.utils.DownloadCancelled:
                raise
            except Exception as e:
                last_err = e
                es = str(e).lower()
                print(f"[DL FAIL] {es[:100]}")
                if "login" in es or "private" in es or "cookie" in es:
                    break
                continue

        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("İptal edildi")

        if not success:
            raise last_err or Exception("Tüm denemeler başarısız")

        full_path = None
        for f in sorted(os.listdir(DOWNLOAD_DIR)):
            if f.startswith(filename):
                full_path = os.path.join(DOWNLOAD_DIR, f)
                break

        if not full_path:
            return jsonify({"error": "Dosya bulunamadı"}), 500

        # ── Mute mod: ses izini indirme SONRASI ffmpeg ile kes ──
        # PO Token olmadan video-only akışlar güvenilmediği için, normal
        # (sesli) formatı indirip burada -an ile sesi çıkarıyoruz.
        if not is_audio and data.get("mute", False) and FFMPEG_DIR:
            muted_path = full_path + ".muted." + full_path.rsplit('.', 1)[-1]
            try:
                result = subprocess.run(
                    [os.path.join(FFMPEG_DIR, "ffmpeg"), "-y", "-i", full_path,
                     "-c", "copy", "-an", muted_path],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0 and os.path.exists(muted_path):
                    os.remove(full_path)
                    os.rename(muted_path, full_path)
                else:
                    print(f"[MUTE FFMPEG FAIL] {result.stderr[:200]}")
            except Exception as e:
                print(f"[MUTE FFMPEG ERR] {e}")
                try:
                    if os.path.exists(muted_path):
                        os.remove(muted_path)
                except: pass

        if sid:
            socketio.emit('progress', {'percent': 100, 'status': 'done'}, room=sid)

        ext = full_path.rsplit('.', 1)[-1] if '.' in full_path else fmt
        download_name = f"zenithw.{ext}"
        response = send_file(full_path, as_attachment=True, download_name=download_name)
        response.headers['X-Download-Id'] = download_id
        response.headers['Access-Control-Expose-Headers'] = 'X-Download-Id'

        @response.call_on_close
        def cleanup():
            try:
                if full_path and os.path.exists(full_path):
                    os.remove(full_path)
            except: pass
            with cancel_events_lock:
                cancel_events.pop(download_id, None)

        return response

    except yt_dlp.utils.DownloadCancelled:
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(filename):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except: pass
        with cancel_events_lock:
            cancel_events.pop(download_id, None)
        if sid:
            socketio.emit('progress', {'percent': 0, 'status': 'cancelled'}, room=sid)
        return jsonify({"error": "cancelled"}), 409
    except Exception as e:
        error_msg = str(e)
        print(f"[DL ERR] {error_msg[:200]}")
        with cancel_events_lock:
            cancel_events.pop(download_id, None)
        if sid:
            socketio.emit('progress', {'status': 'error', 'message': error_msg[:100]}, room=sid)
        parsed = parse_error(error_msg, url)
        if parsed == "instagram_ratelimit":
            return jsonify({"error": "instagram_ratelimit"}), 400
        return jsonify({"error": parsed}), 400

# ── /thumbnail ─────────────────────────────────────────
@app.route("/thumbnail", methods=["POST"])
def download_thumbnail():
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Çok fazla istek. 1 dakika bekleyin."}), 429
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    if is_youtube_live_url(url):
        return jsonify({"error": "Canlı yayınlar şu anda desteklenmiyor."}), 400
    if is_unsupported_domain(url):
        return jsonify({"error": "Bu platform desteklenmiyor."}), 400
    if not FFMPEG_DIR:
        return jsonify({"error": "FFmpeg gerekli"}), 400

    filename = str(uuid.uuid4())
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    extra = {
        "skip_download": True,
        "writethumbnail": True,
        "outtmpl": filepath + ".%(ext)s",
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    }
    opts_list = get_opts_list(url, extra=extra)
    last_err = None
    for opts in opts_list:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            full_path = None
            for f in sorted(os.listdir(DOWNLOAD_DIR)):
                if f.startswith(filename):
                    full_path = os.path.join(DOWNLOAD_DIR, f)
                    break
            if not full_path:
                continue
            response = send_file(full_path, as_attachment=True, download_name="thumbnail.jpg")

            @response.call_on_close
            def _cleanup_thumb():
                try:
                    if os.path.exists(full_path):
                        os.remove(full_path)
                except: pass

            return response
        except Exception as e:
            last_err = e
            continue

    error_msg = str(last_err) if last_err else "Thumbnail alınamadı"
    print(f"[THUMB ERR] {error_msg[:150]}")
    return jsonify({"error": parse_error(error_msg, url)}), 400

# ── /convert ───────────────────────────────────────────
ALLOWED_CONVERT_FORMATS = {
    "mp3", "flac", "wav", "ogg", "opus", "m4a",
    "mp4", "webm", "mkv", "avi", "mov",
}

@app.route("/convert", methods=["POST"])
def convert_file():
    ip = get_client_ip()
    if not check_rate_limit(ip):
        return jsonify({"error": "Çok fazla istek. 1 dakika bekleyin."}), 429
    if 'file' not in request.files:
        return jsonify({"error": "Dosya gerekli"}), 400
    file = request.files['file']
    target_format = request.form.get('target_format', 'mp3').lower()
    if not file or file.filename == '':
        return jsonify({"error": "Geçersiz dosya"}), 400
    if target_format not in ALLOWED_CONVERT_FORMATS:
        return jsonify({"error": "Desteklenmeyen hedef format"}), 400
    if not FFMPEG_DIR:
        return jsonify({"error": "FFmpeg gerekli"}), 400

    input_path = None
    output_path = None
    try:
        # Create temp files
        suffix = os.path.splitext(file.filename or 'file.tmp')[1] or '.tmp'
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as input_temp:
            input_path = input_temp.name
            file.save(input_path)

        # target_format whitelist'te olduğu için path traversal riski yok,
        # yine de savunma amaçlı basename ile garantiye alıyoruz.
        base_no_ext = os.path.basename(input_path).rsplit('.', 1)[0]
        output_path = os.path.join(os.path.dirname(input_path), base_no_ext + '.' + target_format)

        # Build ffmpeg command
        cmd = [
            os.path.join(FFMPEG_DIR, 'ffmpeg'),
            '-i', input_path,
            '-y'  # Overwrite output
        ]

        # Add format-specific options
        audio_formats = {'mp3', 'flac', 'wav', 'ogg', 'opus', 'm4a'}
        if target_format in audio_formats:
            cmd.extend(['-vn'])  # No video
            if target_format == 'mp3':
                cmd.extend(['-codec:a', 'libmp3lame', '-q:a', '2'])
            elif target_format == 'flac':
                cmd.extend(['-codec:a', 'flac'])
            elif target_format == 'wav':
                cmd.extend(['-codec:a', 'pcm_s16le'])
            elif target_format == 'ogg':
                cmd.extend(['-codec:a', 'libvorbis', '-q:a', '5'])
            elif target_format == 'opus':
                cmd.extend(['-codec:a', 'libopus', '-b:a', '128k'])
            elif target_format == 'm4a':
                cmd.extend(['-codec:a', 'aac', '-b:a', '192k'])
        else:
            # Video formats
            if target_format == 'mp4':
                cmd.extend(['-c:v', 'libx264', '-c:a', 'aac'])
            elif target_format == 'webm':
                cmd.extend(['-c:v', 'libvpx-vp9', '-c:a', 'libopus'])
            elif target_format == 'mkv':
                cmd.extend(['-c:v', 'libx264', '-c:a', 'aac'])
            elif target_format == 'avi':
                cmd.extend(['-c:v', 'libx264', '-c:a', 'mp3'])
            elif target_format == 'mov':
                cmd.extend(['-c:v', 'libx264', '-c:a', 'aac'])

        cmd.append(output_path)

        # Run ffmpeg
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            return jsonify({"error": f"Dönüştürme hatası: {result.stderr[:200]}"}), 400

        # Cleanup input, send output; use call_on_close to delete output after streaming
        try:
            if input_path and os.path.exists(input_path):
                os.unlink(input_path)
        except: pass

        _out = output_path
        response = send_file(_out, as_attachment=True, download_name=f"converted.{target_format}")

        @response.call_on_close
        def _cleanup_conv():
            try:
                if _out and os.path.exists(_out):
                    os.unlink(_out)
            except: pass

        return response

    except Exception as e:
        # Clean up on error
        try:
            if input_path and os.path.exists(input_path):
                os.unlink(input_path)
            if output_path and os.path.exists(output_path):
                os.unlink(output_path)
        except: pass
        error_msg = str(e)
        print(f"[CONV ERR] {error_msg[:200]}")
        return jsonify({"error": error_msg[:200]}), 400

@socketio.on('connect')
def on_connect():
    print(f"+ {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    print(f"- {request.sid}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", debug=False, port=port)