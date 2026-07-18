from gevent import monkey
monkey.patch_all()

import logging

from flask import Flask, request, jsonify, send_file, send_from_directory, g
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

# ── Logging ─────────────────────────────────────────────
# print yerine logging kullanıyoruz: zaman damgası, seviye (INFO/WARNING/
# ERROR) ve gevent altında eşzamanlı isteklerde daha düzenli/okunabilir
# çıktı sağlıyor. LOG_LEVEL env variable ile prod'da WARNING'e çekilebilir,
# geliştirmede varsayılan INFO bırakılabilir.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("zenithw")

# socketio/engineio kendi iç bağlantı loglarını (client id'leri, polling
# GET/POST istekleri) INFO seviyesinde basıyor; bunlar bizim asıl uygulama
# loglarımızı gürültüye boğuyor. WARNING'e çekip sadece gerçek sorunları
# görüyoruz.
logging.getLogger("engineio").setLevel(logging.WARNING)
logging.getLogger("socketio").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

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
def add_to_history(url, title, platform, fmt, success=True):
    pass

# ── Cookies ───────────────────────────────────────────
COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

cookies_env = os.environ.get("YOUTUBE_COOKIES") or os.environ.get("COOKIES") or os.environ.get("YOUTUBE_COOKIE")
if cookies_env:
    try:
        cookies_content = cookies_env.replace('\\n', '\n').strip()
        # Dosyayı önce oluştur/aç, sonra izinlerini kısıtla (0600: sadece sahibi okuyup yazabilir)
        fd = os.open(COOKIES_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(cookies_content)
        os.chmod(COOKIES_FILE, 0o600)
        logger.info(f"[INIT] cookies.txt Railway environment variable'dan yazıldı ✓ ({len(cookies_content)} bytes)")
    except Exception as e:
        logger.warning(f"[INIT] ⚠️ cookies.txt yazılamadı: {e}")
elif os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
    try:
        os.chmod(COOKIES_FILE, 0o600)
    except Exception:
        pass
    logger.info(f"[INIT] cookies.txt bulundu ✓ ({os.path.getsize(COOKIES_FILE)} bytes)")
else:
    logger.warning("[INIT] ⚠️ cookies.txt bulunamadı veya geçersiz - YouTube indirme kısıtlı olabilir")

# ── Rate limiting ─────────────────────────────────────
rate_limit_data = defaultdict(list)
rate_limit_lock = threading.Lock()

# ── Client IP tespiti (Cloudflare + Railway zinciri) ───
# Zincir: Client -> Cloudflare -> Railway -> bu uygulama.
# X-Forwarded-For header'ının İLK değerine güvenmek YANLIŞTIR: bu değer
# genellikle client'ın kendi gönderdiği (dolayısıyla sahtelenebilir)
# değerdir; gerçek/güvenilir IP genelde listenin SONUNA doğru eklenir.
# Bunun yerine Cloudflare'in kendi ürettiği ve client tarafından asla
# override edilemeyen CF-Connecting-IP header'ı önceliklendirilir.
# Bu header sadece Cloudflare'in kendisi tarafından set edilir; ancak bu
# korumanın anlamlı olması için Railway'in DOĞRUDAN (Cloudflare'i bypass
# ederek) gelen trafiği reddetmesi/filtrelemesi gerekir - aksi halde biri
# Railway'in verdiği *.up.railway.app adresine doğrudan istek atıp bu
# header'ı serbestçe sahteleyebilir. Bunu Railway tarafında Cloudflare IP
# aralıklarına kısıtlayarak veya Cloudflare "Authenticated Origin Pulls"
# ile sağlayın.
#
# TRUST_PROXY=0 verilirse hem CF-Connecting-IP hem X-Forwarded-For yok
# sayılır ve sadece gerçek soket adresi (request.remote_addr) kullanılır.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "1") != "0"

def check_rate_limit(ip):
    now = time.time()
    with rate_limit_lock:
        rate_limit_data[ip] = [t for t in rate_limit_data[ip] if now - t < 60]
        if len(rate_limit_data[ip]) >= 10:
            return False
        rate_limit_data[ip].append(now)
        return True

def cleanup_rate_limit_data():
    now = time.time()
    with rate_limit_lock:
        stale_ips = [
            ip for ip, timestamps in rate_limit_data.items()
            if not timestamps or now - max(timestamps) > 60
        ]
        for ip in stale_ips:
            del rate_limit_data[ip]

def get_client_ip():
    if TRUST_PROXY:
        # 1. Öncelik: Cloudflare'in kendi header'ı. Cloudflare -> Railway
        # bağlantısında bu header Cloudflare tarafından set edilir ve
        # client bunu değiştiremez (Cloudflare kendi değeriyle ezer).
        cf_ip = request.headers.get('CF-Connecting-IP')
        if cf_ip:
            candidate = cf_ip.strip()
            if candidate:
                return candidate
        # 2. Fallback: Cloudflare üzerinden gelmeyen (örn. yerel/dev veya
        # Cloudflare'siz farklı bir kurulum) istekler için X-Forwarded-For.
        # NOT: Railway origin'i Cloudflare'e kısıtlanmadıysa bu header
        # istemci tarafından sahtelenebilir; bkz. yukarıdaki yorum.
        xff = request.headers.get('X-Forwarded-For')
        if xff:
            candidate = xff.split(',')[0].strip()
            if candidate:
                return candidate
    return request.remote_addr or "unknown"

# ── Aynı IP'den eşzamanlı istek sınırı ─────────────────
# Dakikalık rate limit (10/dk) tek başına yetmiyor: biri aynı IP'den 10+
# isteği aynı anda (t=0'da) patlatırsa hepsi limitten geçer ve sunucuyu
# aynı anda meşgul eder. Bu hook tüm route'lara (statik dosyalar hariç
# değil, hepsine) uygulanır ve bir IP'nin o an açık olan istek sayısını
# MAX_CONCURRENT_PER_IP ile sınırlar.
MAX_CONCURRENT_PER_IP = int(os.environ.get("MAX_CONCURRENT_PER_IP", 5))
concurrent_ip_lock = threading.Lock()
concurrent_ip_counts = defaultdict(int)

@app.before_request
def _limit_concurrent_requests_per_ip():
    ip = get_client_ip()
    with concurrent_ip_lock:
        if concurrent_ip_counts[ip] >= MAX_CONCURRENT_PER_IP:
            return jsonify({"error": "Aynı anda çok fazla istek gönderiyorsunuz. Lütfen bekleyin."}), 429
        concurrent_ip_counts[ip] += 1
    g._concurrent_ip = ip

@app.teardown_request
def _release_concurrent_request_per_ip(exc=None):
    ip = getattr(g, "_concurrent_ip", None)
    if ip is not None:
        with concurrent_ip_lock:
            if concurrent_ip_counts[ip] > 0:
                concurrent_ip_counts[ip] -= 1
            if concurrent_ip_counts[ip] == 0:
                del concurrent_ip_counts[ip]

# ── Eşzamanlı indirme/ffmpeg sınırı ────────────────────
# /download tek bir istekte hem yt-dlp indirmesini hem de (varsa) ffmpeg
# postprocess adımlarını yapıyor, yani "aktif indirme" ve "aktif ffmpeg"
# burada fiilen aynı şey. Tek bir global semaphore ile CPU/bant genişliği
# tüketimini sınırlıyoruz; slot boşalana kadar bekleyen istekler kuyrukta
# tutulur ve sid varsa ilerleme olarak "queued" durumu gönderilir.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
download_slots = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
queue_lock = threading.Lock()
queue_waiting = 0
active_downloads_count = 0

def _release_download_slot():
    """download_slots.release() ile birlikte aktif indirme sayacını da
    tutarlı şekilde azaltır. /health endpoint'i bu sayacı okuyor."""
    global active_downloads_count
    download_slots.release()
    with queue_lock:
        active_downloads_count = max(0, active_downloads_count - 1)

def acquire_download_slot(sid, cancel_event):
    """Slot boşalana kadar bekler; iptal edilirse DownloadCancelled fırlatır.
    Slot alınca True döner (çağıran taraf finally içinde release etmeli)."""
    global queue_waiting, active_downloads_count
    with queue_lock:
        queue_waiting += 1
    try:
        while True:
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled("İptal edildi")
            if download_slots.acquire(blocking=True, timeout=1):
                with queue_lock:
                    active_downloads_count += 1
                return True
            if sid:
                with queue_lock:
                    ahead = max(0, queue_waiting - 1)
                socketio.emit('progress', {
                    'status': 'queued',
                    'message': f"Sunucu yoğun, sırada bekleniyor... ({ahead} kişi önde)"
                }, room=sid)
    finally:
        with queue_lock:
            queue_waiting -= 1

# ── Maksimum video süresi ──────────────────────────────
MAX_VIDEO_DURATION_SECONDS = int(os.environ.get("MAX_VIDEO_DURATION_SECONDS", 90 * 60))

# ── Maksimum indirme boyutu ─────────────────────────────
# Site kalitesi konusunda taviz vermiyoruz (quality parametresine
# dokunulmuyor); bunun yerine 1.5GB'ı aşan indirmeler iptal ediliyor.
# Böylece son derece uzun/yüksek bitrate'li dosyalar disk ve bant
# genişliğini tüketemiyor, ama normal kaliteli videolar etkilenmiyor.
MAX_DOWNLOAD_SIZE_BYTES = int(os.environ.get("MAX_DOWNLOAD_SIZE_MB", 1536)) * 1024 * 1024

# ── İndirme zaman aşımı ─────────────────────────────────
# yt-dlp'nin ydl.download() çağrısı, ayrı video+ses akışlarını birleştirmek
# için kendi içinde ffmpeg'i çağırabiliyor (FFmpegMerger postprocessor).
# Bu adımın kendi subprocess.run çağrımızdaki gibi bir timeout'u YOK - eğer
# bu adım (nadir de olsa, örn. bozuk bir fragment veya Railway'in kısıtlı
# CPU'sunda beklenmedik bir takılma yüzünden) donarsa, tüm istek süresiz
# askıda kalır. Bunu önlemek için tüm download denemesini bir üst
# zaman aşımıyla sarıyoruz.
DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", 600))

def probe_duration(url):
    """Gerçek indirmeye başlamadan önce hafif bir extract_info ile süreyi
    öğrenir. Süre bilinmiyorsa (bazı platformlarda olabiliyor) None döner
    ve indirmeye izin verilir (fail-open) — amaç sadece bariz uzun
    videoları (canlı yayın kayıtları, uzun podcastler vb.) baştan elemek."""
    try:
        opts_list = get_opts_list(url, extra={"skip_download": True, "quiet": True})
        for opts in opts_list:
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info.get("_type") == "playlist" or "entries" in info:
                        return None  # playlist: tekil videolar zaten ayrı ayrı /download ile geliyor
                    return info.get("duration") or None
            except Exception:
                continue
    except Exception:
        pass
    return None

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
logger.info(f"[INIT] ffmpeg={FFMPEG_DIR}")

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
logger.info(f"[INIT] aria2c={ARIA2_PATH or 'yok'}")

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

UNSUPPORTED_DOMAINS = (
    "spotify.com", "music.apple.com", "deezer.com", "tidal.com",
    "music.amazon.com", "music.youtube.com",
)

def is_unsupported_domain(u):
    ul = u.lower()
    return any(d in ul for d in UNSUPPORTED_DOMAINS)

# ── SSRF Koruması ──────────────────────────────────────
# yt-dlp'ye vereceğimiz URL'nin şeması http/https ile sınırlı olmalı ve
# çözülen host iç ağa (private/loopback/link-local/metadata endpoint)
# işaret etmemeli. Bu kontrol hem hostname hem de (varsa) doğrudan IP
# girişleri için DNS çözümlemesi yapılarak uygulanır (DNS rebinding'e
# karşı da bir miktar koruma sağlar; yt-dlp'nin kendi bağlantısı ayrı bir
# an'da tekrar resolve edeceği için %100 garanti değildir, ama basit
# SSRF denemelerinin büyük çoğunluğunu engeller).
import ipaddress
import socket
from urllib.parse import urlparse

def _is_private_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # parse edilemeyen şey güvenli sayılmaz
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or
        ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )

def is_safe_url(u):
    try:
        parsed = urlparse(u)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    # Bariz metadata/local isimler
    lowered = hostname.lower()
    if lowered in ("localhost", "metadata", "metadata.google.internal"):
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        # Çözülemiyorsa reddet; yt-dlp zaten başaramayacak
        return False
    for info in infos:
        addr = info[4][0]
        if _is_private_ip(addr):
            return False
    return True

# ── SSRF: redirect / DNS-rebinding koruması (soket seviyesi) ──────────
# is_safe_url() sadece kullanıcının verdiği URL'nin İLK anındaki DNS
# çözümlemesini kontrol eder. Ama yt-dlp indirme sırasında HTTP
# redirect'leri (3xx) takip eder ve o an tekrar DNS sorgusu yapar; yani
# "güvenli" görünen bir URL, sunucu tarafında 127.0.0.1 veya
# 169.254.169.254 (cloud metadata) gibi bir adrese yönlendirilebilir ve
# yukarıdaki tek seferlik kontrol bunu YAKALAYAMAZ.
#
# Bunu kapatmak için: yt-dlp (ve requests/urllib) alt seviyede
# socket.create_connection() kullanıyor. Bu fonksiyonu process genelinde
# monkeypatch'leyip HER gerçek TCP bağlantısında hedef IP'yi kontrol
# ediyoruz. Böylece redirect sonrası gerçekten bağlanılan adres de
# doğrulanmış olur (DNS rebinding'e karşı da koruma sağlar, çünkü kontrol
# "bağlanma anında" yapılıyor, DNS lookup ile bağlanma arasında değil).
#
# ÖNEMLİ SINIRLAMA: Bu guard sadece Python sürecinin kendi yaptığı
# bağlantıları kapsar. Eğer ARIA2_PATH mevcutsa ve external_downloader
# olarak aria2c kullanılıyorsa, aria2c AYRI BİR PROSES olduğu için bu
# monkeypatch onu kapsamaz. Tam koruma için ya aria2c'yi devre dışı
# bırakın (ARIA2_PATH'i kullanmayın) ya da altyapı seviyesinde (Railway/
# container) egress firewall ile private IP aralıklarına (RFC1918,
# 169.254.0.0/16, ::1 vb.) giden trafiği engelleyin.
_orig_create_connection = socket.create_connection

def _guarded_create_connection(address, *args, **kwargs):
    host = address[0]
    try:
        ip = ipaddress.ip_address(host)
        if _is_private_ip(str(ip)):
            raise PermissionError(f"SSRF koruması: {host} adresine bağlantı engellendi")
    except ValueError:
        # host bir hostname (nadiren burada IP değil de isim gelebilir,
        # örn. bazı kütüphaneler resolve etmeden çağırabilir) - yine de
        # çözüp kontrol edelim.
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            infos = []
        for info in infos:
            if _is_private_ip(info[4][0]):
                raise PermissionError(f"SSRF koruması: {host} adresine bağlantı engellendi")
    return _orig_create_connection(address, *args, **kwargs)

socket.create_connection = _guarded_create_connection

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
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M"]
        }
    if use_cookies and os.path.exists(COOKIES_FILE) and not is_instagram(url):
        opts["cookiefile"] = COOKIES_FILE
    if is_youtube(url):
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web", "mweb", "android"],
            }
        }
    return opts

def get_opts_list(url, extra=None):
    opts_list = []
    o = get_base_opts(url, use_cookies=True)
    if extra: o.update(extra)
    opts_list.append(o)
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
        else:
            if best:
                return "bestvideo+bestaudio/best"
            return (f"bestvideo[height<={q}]+bestaudio"
                    f"/best[height<={q}]/best")
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
        "active_downloads": active_downloads_count,
        "max_concurrent_downloads": MAX_CONCURRENT_DOWNLOADS,
        "queue_waiting": queue_waiting,
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
        # send_from_directory zaten safe_join ile path traversal'a karşı korumalı,
        # ama biz yine de normalize edilmiş yolun FRONTEND_DIR dışına çıkmadığını
        # açıkça doğruluyoruz (savunma katmanı).
        full = os.path.normpath(os.path.join(FRONTEND_DIR, path))
        if full.startswith(os.path.abspath(FRONTEND_DIR)) and os.path.exists(full) and os.path.isfile(full):
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
    if not is_safe_url(url):
        return jsonify({"error": "Geçersiz veya izin verilmeyen URL."}), 400
    if is_youtube_live_url(url):
        return jsonify({"error": "Canlı yayınlar şu anda desteklenmiyor."}), 400
    if is_unsupported_domain(url):
        return jsonify({"error": "Bu platform desteklenmiyor. Desteklenen platformları kontrol edin."}), 400

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
    logger.error(f"[INFO ERR] {url[:60]}: {error_msg[:150]}")
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

    want_subs = bool(data.get("subtitles", False))
    sub_langs = data.get("sub_langs") or ["en"]
    if isinstance(sub_langs, str):
        sub_langs = [sub_langs]
    sub_langs = [l for l in sub_langs if isinstance(l, str) and 1 <= len(l) <= 10 and all(c.isalnum() or c == '-' for c in l)][:5]
    embed_subs = bool(data.get("embed_subs", True))

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

    # Whitelist doğrulamaları: format, codec ve quality kullanıcıdan geliyor
    # ve bunlar build_format_str ile bir yt-dlp format string'ine ekleniyor.
    # Beklenmeyen değerler yt-dlp'ye enjekte edilmeden önce reddedilmeli.
    ALLOWED_FORMATS = {"mp4", "webm", "mkv", "avi", "mov"} | AUDIO_FMTS
    ALLOWED_CODECS = {"h264", "av1", "vp9"}
    if fmt not in ALLOWED_FORMATS:
        return jsonify({"error": "Desteklenmeyen format"}), 400
    if codec not in ALLOWED_CODECS:
        return jsonify({"error": "Desteklenmeyen codec"}), 400
    if not quality.isdigit() or not (1 <= len(quality) <= 4):
        return jsonify({"error": "Geçersiz kalite değeri"}), 400
    if not audio_q.isdigit():
        audio_q = "256"

    if not url:
        return jsonify({"error": "URL gerekli"}), 400
    if not is_safe_url(url):
        return jsonify({"error": "Geçersiz veya izin verilmeyen URL."}), 400
    if is_youtube_live_url(url):
        return jsonify({"error": "Canlı yayınlar şu anda desteklenmiyor."}), 400
    if is_unsupported_domain(url):
        return jsonify({"error": "Bu platform desteklenmiyor. Desteklenen platformları kontrol edin."}), 400

    is_audio = fmt in AUDIO_FMTS
    cancel_event = threading.Event()
    size_exceeded = {"flag": False}
    with cancel_events_lock:
        cancel_events[download_id] = cancel_event

    # Maksimum video süresi kontrolü (gerçek indirmeye/slot kuyruğuna
    # girmeden önce yapılır ki uzun videolar başkalarının sırasını tutmasın).
    duration = probe_duration(url)
    if duration and duration > MAX_VIDEO_DURATION_SECONDS:
        with cancel_events_lock:
            cancel_events.pop(download_id, None)
        max_min = MAX_VIDEO_DURATION_SECONDS // 60
        return jsonify({"error": f"Video çok uzun (maksimum {max_min} dakika)."}), 400

    filename = str(uuid.uuid4())
    filepath = os.path.join(DOWNLOAD_DIR, filename)

    def progress_hook(d):
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("İptal edildi")
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            # Boyut limiti: ya toplam boyut baştan biliniyorsa (total) ya da
            # indirilen miktar limiti şimdiden geçtiyse indirmeyi iptal et.
            if (total and total > MAX_DOWNLOAD_SIZE_BYTES) or downloaded > MAX_DOWNLOAD_SIZE_BYTES:
                size_exceeded["flag"] = True
                cancel_event.set()
                if sid:
                    socketio.emit('progress', {
                        'status': 'error',
                        'message': f"Dosya boyutu limiti aşıldı (maksimum {MAX_DOWNLOAD_SIZE_BYTES // (1024*1024)} MB)."
                    }, room=sid)
                raise yt_dlp.utils.DownloadCancelled("Boyut limiti aşıldı")
            if total > 0 and sid:
                pct = max(5, int(downloaded / total * 82))
                socketio.emit('progress', {
                    'percent': pct,
                    'speed': d.get('_speed_str', '').strip(),
                    'eta': d.get('_eta_str', '').strip(),
                    'status': 'downloading'
                }, room=sid)
        elif d['status'] == 'finished' and sid:
            socketio.emit('progress', {'percent': 88, 'status': 'merging'}, room=sid)

    # Aktif indirme/ffmpeg sayısı sınırına ulaşıldıysa burada kuyrukta
    # bekler; slot alınamadan iptal edilirse DownloadCancelled fırlatılır
    # ve aşağıdaki except bloğu bunu normal şekilde yakalar.
    slot_acquired = False
    try:
        acquire_download_slot(sid, cancel_event)
        slot_acquired = True
        fmt_str = build_format_str(url, quality, fmt, codec)
        logger.info(f"[DL] q={quality} fmt={fmt} codec={codec} audio={is_audio}")

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
            is_mute = data.get("mute", False)
            merge_fmt = "mp4"
            if fmt == "webm": merge_fmt = "webm"
            elif fmt == "mkv": merge_fmt = "mkv"
            elif fmt == "avi": merge_fmt = "avi"
            elif fmt == "mov": merge_fmt = "mov"
            elif codec in ("av1", "vp9") and fmt != "mp4": merge_fmt = "webm"
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
        timed_out = False
        logger.info(f"[DL] indirme başlıyor (timeout={DOWNLOAD_TIMEOUT_SECONDS}s)")
        for opts in opts_list:
            if cancel_event.is_set():
                break
            try:
                with gevent.Timeout(DOWNLOAD_TIMEOUT_SECONDS, TimeoutError(f"{DOWNLOAD_TIMEOUT_SECONDS}s içinde tamamlanmadı")):
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                success = True
                break
            except yt_dlp.utils.DownloadCancelled:
                raise
            except TimeoutError as e:
                timed_out = True
                last_err = e
                logger.error(f"[DL TIMEOUT] {DOWNLOAD_TIMEOUT_SECONDS}s aşıldı, muhtemelen yt-dlp'nin içindeki ffmpeg merge adımı takıldı")
                break
            except Exception as e:
                last_err = e
                es = str(e).lower()
                logger.error(f"[DL FAIL] {es[:100]}")
                if "login" in es or "private" in es or "cookie" in es:
                    break
                continue

        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled("İptal edildi")

        if timed_out:
            with cancel_events_lock:
                cancel_events.pop(download_id, None)
            if slot_acquired:
                _release_download_slot()
                slot_acquired = False
            # Yarım kalmış geçici dosyaları temizle
            for f in os.listdir(DOWNLOAD_DIR):
                if f.startswith(filename):
                    try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                    except: pass
            if sid:
                socketio.emit('progress', {'status': 'error', 'message': 'İndirme zaman aşımına uğradı, lütfen tekrar deneyin.'}, room=sid)
            return jsonify({"error": "İndirme zaman aşımına uğradı, lütfen tekrar deneyin."}), 504

        if not success:
            raise last_err or Exception("Tüm denemeler başarısız")

        logger.info("[DL] indirme tamamlandı, dosya işleniyor...")

        full_path = None
        for f in sorted(os.listdir(DOWNLOAD_DIR)):
            if f.startswith(filename):
                full_path = os.path.join(DOWNLOAD_DIR, f)
                break

        if not full_path:
            return jsonify({"error": "Dosya bulunamadı"}), 500

        if not is_audio and data.get("mute", False) and FFMPEG_DIR:
            logger.info("[DL] mute (sessizleştirme) adımı başlıyor")
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
                    logger.error(f"[MUTE FFMPEG FAIL] {result.stderr[:200]}")
            except Exception as e:
                logger.error(f"[MUTE FFMPEG ERR] {e}")
                try:
                    if os.path.exists(muted_path):
                        os.remove(muted_path)
                except: pass

        # Son güvenlik kontrolü: merge/mute sonrası dosya boyutu (ör. ses+video
        # birleşiminde şişme olabilir) limiti aşmışsa dosyayı sil ve reddet.
        try:
            final_size = os.path.getsize(full_path)
        except OSError:
            final_size = 0
        if final_size > MAX_DOWNLOAD_SIZE_BYTES:
            try:
                os.remove(full_path)
            except: pass
            with cancel_events_lock:
                cancel_events.pop(download_id, None)
            if slot_acquired:
                _release_download_slot()
                slot_acquired = False
            if sid:
                socketio.emit('progress', {'status': 'error', 'message': 'Dosya boyutu limiti aşıldı.'}, room=sid)
            max_mb = MAX_DOWNLOAD_SIZE_BYTES // (1024 * 1024)
            return jsonify({"error": f"Dosya boyutu limiti aşıldı (maksimum {max_mb} MB)."}), 400

        if sid:
            socketio.emit('progress', {'percent': 100, 'status': 'done'}, room=sid)

        # Ağır iş (indirme + ffmpeg) bitti; slotu burada bırakıyoruz ki
        # dosya kullanıcıya stream edilirken sıradaki iş beklemesin.
        if slot_acquired:
            _release_download_slot()
            slot_acquired = False

        ext = full_path.rsplit('.', 1)[-1] if '.' in full_path else fmt
        download_name = f"zenithw.{ext}"
        logger.info(f"[DL] yanıt gönderiliyor: {download_name} ({os.path.getsize(full_path)} bytes)")
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
        if slot_acquired:
            _release_download_slot()
            slot_acquired = False
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(filename):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except: pass
        with cancel_events_lock:
            cancel_events.pop(download_id, None)
        if size_exceeded["flag"]:
            max_mb = MAX_DOWNLOAD_SIZE_BYTES // (1024 * 1024)
            if sid:
                socketio.emit('progress', {'status': 'error', 'message': f"Dosya boyutu limiti aşıldı (maksimum {max_mb} MB)."}, room=sid)
            return jsonify({"error": f"Dosya boyutu limiti aşıldı (maksimum {max_mb} MB)."}), 400
        if sid:
            socketio.emit('progress', {'percent': 0, 'status': 'cancelled'}, room=sid)
        return jsonify({"error": "cancelled"}), 409
    except Exception as e:
        if slot_acquired:
            _release_download_slot()
            slot_acquired = False
        error_msg = str(e)
        logger.error(f"[DL ERR] {error_msg[:200]}")
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
    if not is_safe_url(url):
        return jsonify({"error": "Geçersiz veya izin verilmeyen URL."}), 400
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
    logger.error(f"[THUMB ERR] {error_msg[:150]}")
    return jsonify({"error": parse_error(error_msg, url)}), 400

# ── /convert ───────────────────────────────────────────
ALLOWED_CONVERT_FORMATS = {
    "mp3", "flac", "wav", "ogg", "opus", "m4a",
    "mp4", "webm", "mkv", "avi", "mov",
}

# target_format zaten whitelist ile kontrol ediliyor ama input dosyasının
# suffix'i kullanıcının gönderdiği orijinal filename'den türetiliyordu.
# Bunun yerine input dosyası için de sabit/whitelisted bir uzantı seti
# kullanıyoruz; gerçek dosya türünü ffmpeg zaten kendi içerik analiziyle
# tespit eder, uzantıya güvenmemize gerek yok.
ALLOWED_INPUT_EXTS = {
    ".mp3", ".flac", ".wav", ".ogg", ".opus", ".m4a", ".aac",
    ".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v", ".flv", ".wmv", ".3gp",
}

def safe_input_suffix(original_filename):
    ext = os.path.splitext(original_filename or "")[1].lower()
    # Sadece harf/rakam/nokta içeren, whitelist'teki bir uzantıya izin ver.
    if ext in ALLOWED_INPUT_EXTS and all(c.isalnum() or c == '.' for c in ext):
        return ext
    return ".bin"

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
        # Kullanıcının gönderdiği filename'e güvenmek yerine whitelist'ten
        # doğrulanmış bir suffix kullanıyoruz (komut/uzantı enjeksiyonuna karşı).
        suffix = safe_input_suffix(file.filename)
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=DOWNLOAD_DIR) as input_temp:
            input_path = input_temp.name
            file.save(input_path)

        base_no_ext = os.path.basename(input_path).rsplit('.', 1)[0]
        # target_format whitelist'te olduğu için ekstra güvenli; yine de
        # basename ile garantiye alıyoruz.
        output_path = os.path.join(os.path.dirname(input_path), base_no_ext + '.' + target_format)

        cmd = [
            os.path.join(FFMPEG_DIR, 'ffmpeg'),
            '-i', input_path,
            '-y'
        ]

        audio_formats = {'mp3', 'flac', 'wav', 'ogg', 'opus', 'm4a'}
        if target_format in audio_formats:
            cmd.extend(['-vn'])
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

        # timeout eklendi: kötü amaçlı/bozuk bir dosya ffmpeg'i sonsuz
        # döngüye sokup worker'ı tıkayabilir (DoS riski).
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            # stderr içeriği kullanıcıya döndürülmüyor artık; sunucu
            # tarafında loglanıp kullanıcıya genel bir mesaj veriliyor
            # (dosya yolu / sistem bilgisi sızıntısını önlemek için).
            logger.error(f"[CONV FFMPEG ERR] {result.stderr[:300]}")
            return jsonify({"error": "Dönüştürme başarısız oldu. Dosya formatını kontrol edin."}), 400

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

    except subprocess.TimeoutExpired:
        logger.error("[CONV ERR] ffmpeg timeout")
        return jsonify({"error": "Dönüştürme zaman aşımına uğradı."}), 400
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[CONV ERR] {error_msg[:200]}")
        # Kullanıcıya genel mesaj; iç hata detayı sadece logda.
        return jsonify({"error": "Dönüştürme sırasında bir hata oluştu."}), 400
    finally:
        try:
            if input_path and os.path.exists(input_path):
                os.unlink(input_path)
            if output_path and os.path.exists(output_path):
                # Not: Başarılı senaryoda dosya send_file ile stream edilip
                # call_on_close içinde silinir; burada tekrar silmeye
                # çalışmak zaten var olmayan dosya için sessizce geçilir.
                pass
        except: pass

@socketio.on('connect')
def on_connect():
    logger.info(f"+ {request.sid}")

@socketio.on('disconnect')
def on_disconnect():
    logger.info(f"- {request.sid}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    if not os.environ.get("SECRET_KEY"):
        logger.warning("[UYARI] SECRET_KEY env variable set edilmemiş; her restart'ta yeni bir tane üretiliyor. "
              "Üretimde Railway'de SECRET_KEY environment variable olarak sabit bir değer tanımlayın.")
    socketio.run(app, host="0.0.0.0", debug=False, port=port)