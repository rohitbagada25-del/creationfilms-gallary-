"""
Telegram-backed Photo Gallery for Photographers
------------------------------------------------
Telegram channel = free unlimited-ish photo storage (backend).
This Flask app = the gallery website your clients actually see (frontend).

HOW IT WORKS
1. You create a private Telegram channel and add a bot as admin.
2. You upload each client's photos through the /admin panel here.
   This app sends them to your Telegram channel via the Bot API and
   remembers which Telegram file_id belongs to which client.
3. Your client visits yourdomain.com/gallery/<client-slug>, enters the
   password you set, and sees a proper photo gallery. When an image is
   requested, this server fetches the bytes from Telegram and streams
   them to the browser -- your bot token is never exposed to the client.

SETUP (one time)
1. Open Telegram, message @BotFather, send /newbot, follow the steps.
   Copy the token it gives you (looks like 123456789:AAExxxxxxxxxxxxx).
2. Create a new Telegram channel (Public or Private, either works).
   Add your bot as an Administrator of that channel.
3. Send any message in the channel, then visit this URL in your browser
   (replace TOKEN):
   https://api.telegram.org/botTOKEN/getUpdates
   Find "chat":{"id": -1001234567890, ...}  <-- that's your CHANNEL_ID.
4. Copy .env.example to .env and fill in BOT_TOKEN, CHANNEL_ID, ADMIN_KEY.
5. pip install -r requirements.txt
6. python server.py
7. Open http://localhost:5000/admin to upload client photos.
   Open http://localhost:5000/gallery/<slug> to view a gallery.

DEPLOYING FOR REAL CLIENTS (free)
Render.com or Railway.app both have free tiers that can run a small
Flask app like this 24/7. Push this folder to a GitHub repo, connect
it on Render/Railway, add the same environment variables from .env,
and it gives you a public URL you can share with clients.
"""

import os
import io
import base64
import json
import re
import uuid
import time
import threading
import secrets
from collections import OrderedDict
from functools import wraps
from pathlib import Path

import requests
from PIL import Image, ImageOps
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_from_directory, send_file, Response, stream_with_context
)
from markupsafe import Markup, escape
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

# Optional Cloudflare media delivery. When configured, media bytes are served
# by the Worker instead of passing through Render.
MEDIA_WORKER_URL = os.environ.get("MEDIA_WORKER_URL", "").rstrip("/")
MEDIA_MEDIA_SECRET = os.environ.get("MEDIA_MEDIA_SECRET", "")


DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "galleries.json"
DATA_DIR.mkdir(exist_ok=True)

# In-memory caches (cleared on restart, rebuilt automatically as needed):
# - FILE_PATH_CACHE: Telegram file_id -> file_path, skips a redundant API call
#   on repeat views of the same photo.
# - THUMB_CACHE: file_id -> resized JPEG bytes, so the grid/lightbox never
#   re-downloads and re-resizes the same full-resolution original twice.
#   Capped at THUMB_CACHE_MAX entries (oldest-viewed evicted first) so a big
#   wedding gallery (500-1500+ photos) can't grow this without limit and run
#   the free-tier instance out of RAM -- once full, older thumbnails just get
#   regenerated on next view instead of staying cached, which is a cheap
#   trade-off (a fraction of a second) compared to crashing.
FILE_PATH_CACHE = {}
THUMB_CACHE = OrderedDict()
THUMB_CACHE_MAX = 300
THUMB_CACHE_LOCK = threading.Lock()

THUMB_DIR = DATA_DIR / "thumbs"
PREVIEW_DIR = DATA_DIR / "previews"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_TMP_DIR = DATA_DIR / "upload_tmp"
UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1 MB resumable chunks
DIRECT_UPLOAD_MAX = 50 * 1024 * 1024  # Telegram Bot API sendDocument limit
DB_LOCK = threading.RLock()
# Image pipeline:
# - 1280px thumbnail for the gallery grid
# - 2000px compressed JPEG preview for the lightbox
# - Telegram document remains the untouched original for downloads
THUMB_SIZE = (1280, 1280)
THUMB_QUALITY = 72
PREVIEW_SIZE = (2000, 2000)
PREVIEW_QUALITY = 72


def thumb_cache_get(key):
    with THUMB_CACHE_LOCK:
        if key not in THUMB_CACHE:
            return None
        THUMB_CACHE.move_to_end(key)  # mark as recently used
        return THUMB_CACHE[key]


def thumb_cache_set(key, value):
    with THUMB_CACHE_LOCK:
        THUMB_CACHE[key] = value
        THUMB_CACHE.move_to_end(key)
        while len(THUMB_CACHE) > THUMB_CACHE_MAX:
            THUMB_CACHE.popitem(last=False)  # drop the least-recently-viewed thumbnail


# Render's free tier has very little RAM. Decoding a full-resolution photo
# (e.g. a 24MP JPEG can need 100MB+ just to hold the decoded pixels) is fine
# one at a time, but the gallery grid loads many images in parallel -- doing
# several of those decodes simultaneously was crashing the app (503s). This
# semaphore caps it to 2 at a time; everything else just queues briefly.
THUMB_SEMAPHORE = threading.Semaphore(2)

# The project folder may be called "templates" or "Templates" depending on how it was uploaded
# (Linux hosts are case-sensitive), so accept either.
_BASE_DIR = Path(__file__).parent
_TEMPLATE_DIR = "Templates" if (_BASE_DIR / "Templates").is_dir() and not (_BASE_DIR / "templates").is_dir() else "templates"
app = Flask(__name__, template_folder=_TEMPLATE_DIR)
app.secret_key = SECRET_KEY

# ---------------------------------------------------------------------
# Tiny JSON "database", backed up to Telegram itself.
#
# Render's free plan wipes local files whenever the app restarts/sleeps,
# so the local galleries.json alone is not reliable. Every save also
# uploads the full JSON as a document to the Telegram channel and pins
# it there; on startup, if the local file is missing, we pull the latest
# backup back down from the pinned message. This keeps everything free
# and inside Telegram -- no extra service/account needed.
# ---------------------------------------------------------------------

def load_db():
    if DATA_FILE.exists():
        with open(DATA_FILE, "r") as f:
            return json.load(f)

    # Local file missing (fresh restart) -- try restoring from Telegram.
    if BOT_TOKEN and CHANNEL_ID:
        try:
            restored = tg_restore_db()
            if restored is not None:
                with open(DATA_FILE, "w") as f:
                    json.dump(restored, f, indent=2)
                return restored
        except Exception as e:
            print(f"WARNING: could not restore backup from Telegram: {e}")

    return {"galleries": {}, "website": {}, "portfolio_items": []}


_DB_RO_CACHE = {"sig": None, "data": None}
_DB_RO_LOCK = threading.Lock()

def load_db_ro():
    """Fast READ-ONLY database access for hot routes (photo thumbnails, previews, gallery API).

    The database file now also holds large base64 images (hero, intro photos, logo, testimonials),
    so parsing it on every one of the dozens of thumbnail requests a gallery makes was slow.
    This parses the file only when it actually changed on disk. Callers must NOT modify or save
    the returned object -- routes that write must keep using load_db()."""
    try:
        st = DATA_FILE.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        return load_db()
    cached = _DB_RO_CACHE
    if cached["sig"] == sig and cached["data"] is not None:
        return cached["data"]
    with _DB_RO_LOCK:
        if _DB_RO_CACHE["sig"] == sig and _DB_RO_CACHE["data"] is not None:
            return _DB_RO_CACHE["data"]
        data = load_db()
        _DB_RO_CACHE["data"] = data
        _DB_RO_CACHE["sig"] = sig
        return data


BACKUP_LOCK = threading.Lock()
BACKUP_TIMER = None
BACKUP_PENDING = None
BACKUP_DELAY_SECONDS = 8

def save_db(db, backup=True):
    global BACKUP_TIMER, BACKUP_PENDING
    with DB_LOCK:
        with open(DATA_FILE, "w") as f:
            json.dump(db, f, indent=2)
    if backup and BOT_TOKEN and CHANNEL_ID:
        # Coalesce rapid saves (especially a large photo batch) into one
        # backup. The previous version spawned one Telegram upload per save,
        # so parallel photo registrations could create a large amount of
        # service-initiated Render bandwidth.
        snapshot = json.loads(json.dumps(db))
        with BACKUP_LOCK:
            BACKUP_PENDING = snapshot
            if BACKUP_TIMER is None or not BACKUP_TIMER.is_alive():
                BACKUP_TIMER = threading.Timer(BACKUP_DELAY_SECONDS, _run_pending_backup)
                BACKUP_TIMER.daemon = True
                BACKUP_TIMER.start()

def _run_pending_backup():
    global BACKUP_TIMER, BACKUP_PENDING
    with BACKUP_LOCK:
        snapshot = BACKUP_PENDING
        BACKUP_PENDING = None
        BACKUP_TIMER = None
    if snapshot is None:
        return
    _safe_tg_backup(snapshot)

def _safe_tg_backup(db):
    try:
        tg_backup_db(db)
    except Exception as e:
        print(f"WARNING: Telegram backup failed: {e}")


def slugify(name):
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


# ---------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------

def tg_backup_db(db):
    """Upload the database backup without sending it directly from Render.

    When the Cloudflare Worker is configured, Render sends only the small JSON
    backup to the Worker; the Worker performs the actual Telegram upload. This
    keeps large photo traffic off Render and also prevents backup uploads from
    becoming a noticeable source of service-initiated bandwidth.
    """
    content = json.dumps(db, indent=2).encode("utf-8")

    if MEDIA_WORKER_URL and MEDIA_MEDIA_SECRET:
        r = requests.post(
            f"{MEDIA_WORKER_URL}/backup",
            data=content,
            headers={
                "Content-Type": "application/json",
                "X-Media-Backup-Secret": MEDIA_MEDIA_SECRET,
            },
            timeout=60,
        )
        r.raise_for_status()
        return

    # Local/development fallback when Cloudflare is not configured.
    files = {"document": ("galleries_backup.json", io.BytesIO(content), "application/json")}
    data = {"chat_id": CHANNEL_ID, "caption": "gallery-db-backup (auto)"}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=60)
    r.raise_for_status()
    message_id = r.json()["result"]["message_id"]
    try:
        requests.post(
            f"{TG_API}/pinChatMessage",
            data={"chat_id": CHANNEL_ID, "message_id": message_id, "disable_notification": True},
            timeout=30,
        )
    except Exception:
        pass


def tg_restore_db():
    """Fetch the pinned backup document from the channel, if any."""
    r = requests.get(f"{TG_API}/getChat", params={"chat_id": CHANNEL_ID}, timeout=30)
    r.raise_for_status()
    result = r.json().get("result", {})
    pinned = result.get("pinned_message")
    if not pinned or "document" not in pinned:
        return None
    file_id = pinned["document"]["file_id"]
    file_path = tg_get_file_path(file_id)
    file_url = f"{TG_FILE_API}/{file_path}"
    r2 = requests.get(file_url, timeout=30)
    r2.raise_for_status()
    return r2.json()

def tg_send_photo(file_storage, caption=""):
    """Upload one photo to the Telegram channel, return (file_id, message_id).

    Sent as a *document* rather than via sendPhoto: Telegram's sendPhoto
    endpoint always compresses/resizes images (max ~1280px), while
    sendDocument stores the exact original bytes, so client downloads stay
    full resolution.
    """
    files = {"document": (file_storage.filename, file_storage.stream, file_storage.mimetype)}
    data = {"chat_id": CHANNEL_ID, "caption": caption}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=120)
    r.raise_for_status()
    result = r.json()["result"]
    doc = result["document"]
    return doc["file_id"], result["message_id"]



def tg_send_video(file_storage, caption=""):
    """Upload one video to the Telegram channel and return (file_id, message_id)."""
    files = {"video": (file_storage.filename, file_storage.stream, file_storage.mimetype or "video/mp4")}
    data = {"chat_id": CHANNEL_ID, "caption": caption, "supports_streaming": True}
    r = requests.post(f"{TG_API}/sendVideo", data=data, files=files, timeout=300)
    r.raise_for_status()
    result = r.json()["result"]
    video = result["video"]
    return video["file_id"], result["message_id"]


def tg_get_file_path(file_id):
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    return r.json()["result"]["file_path"]


def thumb_disk_path(slug, photo_id):
    folder = THUMB_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{photo_id}.jpg"


def preview_disk_path(slug, photo_id):
    folder = PREVIEW_DIR / slug
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{photo_id}.jpg"


def make_image_variants(file_storage):
    """Create both browsing thumbnail and lightbox preview during upload."""
    try:
        file_storage.stream.seek(0)
        with Image.open(file_storage.stream) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                bg = Image.new("RGB", img.size, "white")
                if "A" in img.getbands():
                    bg.paste(img, mask=img.getchannel("A"))
                else:
                    bg.paste(img)
                img = bg
            else:
                img = img.convert("RGB")

            preview = img.copy()
            preview.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
            pbuf = io.BytesIO()
            preview.save(pbuf, format="JPEG", quality=PREVIEW_QUALITY, optimize=True, progressive=True)

            thumb = img.copy()
            thumb.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            tbuf = io.BytesIO()
            thumb.save(tbuf, format="JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
            return tbuf.getvalue(), pbuf.getvalue()
    finally:
        try:
            file_storage.stream.seek(0)
        except Exception:
            pass


def ensure_photo_assets(slug, photo):
    """Ensure Telegram-backed thumbnail and preview exist for a photo.

    Existing galleries created by older versions may not have asset file_ids.
    In that case, repair them once from the untouched Telegram original and
    persist the new file_ids in the database. Future requests use Telegram
    assets directly and never regenerate on every view.
    """
    thumb_id = photo.get("thumb_file_id")
    preview_id = photo.get("preview_file_id")
    if thumb_id and preview_id:
        return thumb_id, preview_id

    with DB_LOCK:
        # Reload the latest DB in case another request repaired this photo.
        db = load_db()
        gallery = db["galleries"].get(slug)
        if not gallery:
            raise ValueError("Gallery not found")
        current = next((p for p in gallery.get("photos", []) if p["id"] == photo["id"]), None)
        if not current:
            raise ValueError("Photo not found")
        photo = current
        thumb_id = photo.get("thumb_file_id")
        preview_id = photo.get("preview_file_id")
        if thumb_id and preview_id:
            return thumb_id, preview_id

        original = tg_download_bytes(photo["file_id"])
        source = FileStorage(stream=io.BytesIO(original), filename=photo.get("filename") or "photo.jpg", content_type="image/jpeg")
        thumb_bytes, preview_bytes = make_image_variants(source)

        if not thumb_id:
            thumb_id, _ = tg_send_binary(thumb_bytes, f'{photo["id"]}_thumb.jpg', caption=f'gallery-asset thumb {slug}')
            photo["thumb_file_id"] = thumb_id
        if not preview_id:
            preview_id, _ = tg_send_binary(preview_bytes, f'{photo["id"]}_preview.jpg', caption=f'gallery-asset preview {slug}')
            photo["preview_file_id"] = preview_id

        save_disk_thumb(slug, photo["id"], thumb_bytes)
        save_disk_preview(slug, photo["id"], preview_bytes)
        save_db(db)
        return thumb_id, preview_id


def save_disk_thumb(slug, photo_id, thumb_bytes):
    path = thumb_disk_path(slug, photo_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(thumb_bytes)
    tmp.replace(path)
    return path


def save_disk_preview(slug, photo_id, preview_bytes):
    path = preview_disk_path(slug, photo_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(preview_bytes)
    tmp.replace(path)
    return path


def tg_send_binary(content, filename, caption=""):
    """Store generated gallery assets in Telegram as documents.

    Keeping thumbnail/preview file_ids in the JSON database means the assets
    survive Render/free-host restarts and do not depend on ephemeral disk.
    """
    files = {"document": (filename, io.BytesIO(content), "image/jpeg")}
    data = {"chat_id": CHANNEL_ID, "caption": caption}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=120)
    r.raise_for_status()
    result = r.json()["result"]
    return result["document"]["file_id"], result["message_id"]


def tg_send_thumbnail(thumb_bytes, filename, caption=""):
    return tg_send_binary(thumb_bytes, filename, caption)


def tg_download_bytes(file_id):
    """Download a Telegram file into memory, used only for asset repair."""
    file_path = FILE_PATH_CACHE.get(file_id)
    if not file_path:
        file_path = tg_get_file_path(file_id)
        FILE_PATH_CACHE[file_id] = file_path
    r = requests.get(f"{TG_FILE_API}/{file_path}", timeout=120)
    r.raise_for_status()
    return r.content


# ---------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if session.get("is_admin"):
            return fn(*args, **kwargs)
        if request.headers.get("X-Admin-Key") == ADMIN_KEY:
            return fn(*args, **kwargs)
        return redirect(url_for("admin_login"))
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("key") == ADMIN_KEY:
            session["is_admin"] = True
            return redirect(url_for("admin_home"))
        error = "Wrong key"
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


# ---------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_home():
    db = load_db()
    galleries = db["galleries"]
    return render_template("admin_home.html", galleries=galleries, branding=db.get("branding", {}))


@app.route("/admin/website", methods=["GET", "POST"])
@admin_required
def admin_website():
    db = load_db()
    website = db.get("website", {})
    error = None
    if request.method == "POST":
        updated = dict(website)  # keep anything not on this form
        hero_file = request.files.get("hero_image")
        if hero_file and hero_file.filename:
            try:
                raw = hero_file.read(15 * 1024 * 1024 + 1)
                if len(raw) > 15 * 1024 * 1024:
                    raise ValueError("Hero image must be 15 MB or smaller.")
                im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
                im = im.convert("RGB")
                im.thumbnail((2200, 2200), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                im.save(out, format="JPEG", quality=82, optimize=True, progressive=True)
                # Stored inside the database so it survives Render restarts (local files do not).
                updated["hero_image_b64"] = base64.b64encode(out.getvalue()).decode("ascii")
                updated["hero_image_v"] = int(time.time())
                updated["hero_image_url"] = ""
            except Exception as exc:
                error = "Hero image problem: " + str(exc)
        elif request.form.get("remove_hero_image") == "1":
            updated["hero_image_b64"] = ""
            updated["hero_image_url"] = ""
        elif request.form.get("hero_image_url", "").strip():
            updated["hero_image_url"] = request.form.get("hero_image_url", "").strip()[:1000]
            updated["hero_image_b64"] = ""

        for n in (1, 2):
            f = request.files.get(f"intro_photo{n}")
            if f and f.filename:
                try:
                    updated[f"intro_photo{n}_b64"] = _site_photo_b64(f, max_px=1400, quality=80, limit_mb=15)
                    updated[f"intro_photo{n}_v"] = int(time.time())
                except Exception as exc:
                    error = f"Intro photo {n} problem: {exc}"
            elif request.form.get(f"remove_intro_photo{n}") == "1":
                updated[f"intro_photo{n}_b64"] = ""

        mode = request.form.get("hero_mode", "photo")
        updated.update({
            "home_eyebrow": request.form.get("home_eyebrow", "").strip()[:120],
            "home_title": request.form.get("home_title", "").strip()[:180],
            "home_description": request.form.get("home_description", "").strip()[:500],
            "hero_mode": mode if mode in ("photo", "film", "reel", "highlight") else "photo",
            "hero_video_key": request.form.get("hero_video_key", "").strip()[:120],
            "home_show_intro": request.form.get("home_show_intro") == "1",
            "home_intro_title": request.form.get("home_intro_title", "").strip()[:160],
            "home_intro_text": request.form.get("home_intro_text", "").strip()[:1500],
            "home_show_films": request.form.get("home_show_films") == "1",
            "home_show_reels": request.form.get("home_show_reels") == "1",
            "home_show_highlights": request.form.get("home_show_highlights") == "1",
            "home_show_collections": request.form.get("home_show_collections") == "1",
            "home_show_instagram": request.form.get("home_show_instagram") == "1",
            "home_show_mosaic": request.form.get("home_show_mosaic") == "1",
            "home_show_testimonials": request.form.get("home_show_testimonials") == "1",
            "about_title": request.form.get("about_title", "").strip()[:180],
            "about_text": request.form.get("about_text", "").strip()[:1500],
            "portfolio_intro": request.form.get("portfolio_intro", "").strip()[:500],
        })
        db["website"] = updated
        save_db(db)
        if not error:
            return redirect(url_for("admin_website", saved="1"))
        website = updated
    return render_template("admin_website.html", website=website, videos=all_youtube_videos(db),
                           saved=request.args.get("saved") == "1", error=error)


PORTFOLIO_PER_PAGE = 40


@app.route("/admin/mini/<slug>/<photo_id>")
@admin_required
def admin_mini_thumb(slug, photo_id):
    """Small (360px) cached preview for the Portfolio Manager, so a page of photos loads quickly."""
    db = load_db_ro()
    gallery = db.get("galleries", {}).get(slug)
    photo = next((p for p in (gallery or {}).get("photos", []) if str(p.get("id")) == str(photo_id)), None)
    if not photo:
        return "Not found", 404
    folder = DATA_DIR / "mini" / slug
    path = folder / f"{photo_id}.jpg"
    headers = {"Cache-Control": "private, max-age=2592000"}
    try:
        if path.exists():
            return Response(path.read_bytes(), content_type="image/jpeg", headers=headers)
        data = _photo_thumb_bytes(slug, photo)
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((360, 360), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=70, optimize=True)
        folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(out.getvalue())
        return Response(out.getvalue(), content_type="image/jpeg", headers=headers)
    except Exception as e:
        print(f"Mini thumb error {slug}/{photo_id}: {e}")
        return "Unavailable", 503


@app.route("/admin/portfolio", methods=["GET", "POST"])
@admin_required
def admin_portfolio():
    db = load_db()
    items = db.setdefault("portfolio_items", [])
    galleries = db.get("galleries", {})
    if request.method == "POST":
        selected = set(request.form.getlist("portfolio_item"))
        scope = set(request.form.getlist("scope"))  # the items that were on screen; only these are changed
        # Store only media references that still exist in a gallery. Public rendering
        # never exposes arbitrary file IDs supplied by a browser.
        valid = {}
        for slug, gallery in galleries.items():
            for photo in gallery.get("photos", []):
                key = f"photo:{slug}:{photo.get('id','')}"
                valid[key] = {"key": key, "type": "photo", "slug": slug, "media_id": str(photo.get("id", "")),
                              "title": photo.get("filename") or gallery.get("name", "Wedding Story")}
            for video in gallery.get("videos", []):
                if video.get("source") == "youtube":
                    key = f"video:{slug}:{video.get('id','')}"
                    valid[key] = {"key": key, "type": "video", "slug": slug, "media_id": str(video.get("id", "")),
                                  "title": video.get("filename") or video.get("title") or "Wedding Film"}
        old_by_key = {i.get("key"): i for i in items}
        new_items = []
        done = set()
        for old in items:  # keep existing order (it controls Home ordering); drop what was unchecked on screen
            key = old.get("key")
            if key in scope and key not in selected:
                continue
            if key in scope and key in valid:
                new_items.append(_portfolio_item_from_form(valid[key], key))
                done.add(key)
            elif key in valid:
                new_items.append(old)
                done.add(key)
        for key in request.form.getlist("portfolio_item"):
            if key in valid and key in scope and key not in done:
                new_items.append(_portfolio_item_from_form(valid[key], key))
                done.add(key)
        db["portfolio_items"] = new_items
        save_db(db)
        return redirect(url_for("admin_portfolio", saved="1", gallery=request.form.get("gallery", ""),
                                page=request.form.get("page", "1"), only=request.form.get("only", "")))

    slug = request.args.get("gallery", "")
    mosaic_total = len([i for i in items if i.get("type") == "photo" and i.get("mosaic")])
    chosen_count = {}
    for i in items:
        chosen_count[i.get("slug")] = chosen_count.get(i.get("slug"), 0) + 1
    if slug not in galleries:
        listing = [{"slug": k, "name": g.get("name", k), "photos": len(g.get("photos", [])),
                    "videos": len([v for v in g.get("videos", []) if v.get("source") == "youtube"]),
                    "selected": chosen_count.get(k, 0)} for k, g in galleries.items()]
        return render_template("admin_portfolio.html", listing=listing, gallery=None, mosaic_total=mosaic_total, saved=request.args.get("saved") == "1")

    gallery = galleries[slug]
    by_key = {i.get("key"): i for i in items}
    only = request.args.get("only") == "1"
    media = []
    for video in gallery.get("videos", []):
        if video.get("source") == "youtube":
            key = f"video:{slug}:{video.get('id','')}"
            it = by_key.get(key, {})
            media.append({"key": key, "type": "video", "title": video.get("filename") or video.get("title") or "Film",
                          "selected": key in by_key, "category": normalize_video_category(it.get("category", "Films")),
                          "format": it.get("format", "auto"), "detected": video.get("orientation", ""),
                          "youtube_id": video.get("youtube_id")})
    photos = gallery.get("photos", [])
    if only:
        photos = [p for p in photos if f"photo:{slug}:{p.get('id','')}" in by_key]
    total = len(photos)
    pages = max(1, -(-total // PORTFOLIO_PER_PAGE))
    try:
        page = min(max(int(request.args.get("page", "1")), 1), pages)
    except ValueError:
        page = 1
    for photo in photos[(page - 1) * PORTFOLIO_PER_PAGE: page * PORTFOLIO_PER_PAGE]:
        key = f"photo:{slug}:{photo.get('id','')}"
        it = by_key.get(key, {})
        media.append({"key": key, "type": "photo", "media_id": str(photo.get("id", "")),
                      "title": photo.get("filename") or photo.get("name") or "Photo", "selected": key in by_key,
                      "category": it.get("category", "Wedding"), "event_date": it.get("event_date", ""),
                      "role": it.get("role", "auto"), "tile": bool(it.get("tile")),
                      "mosaic": bool(it.get("mosaic")), "mosaic_pos": it.get("mosaic_pos", "")})
    if page > 1:  # videos are managed on page 1 only
        media = [m for m in media if m["type"] == "photo"]
    return render_template("admin_portfolio.html", gallery={"slug": slug, "name": gallery.get("name", slug)}, media=media,
                           page=page, pages=pages, total=total, only=only, mosaic_total=mosaic_total,
                           photo_categories=PHOTO_CATEGORIES, video_categories=VIDEO_CATEGORIES,
                           saved=request.args.get("saved") == "1")


def _portfolio_item_from_form(base, key):
    item = dict(base)
    default_cat = "Films" if item["type"] == "video" else "Wedding"
    item["category"] = request.form.get("category_" + key, default_cat)[:40]
    item["event_date"] = request.form.get("date_" + key, "").strip()[:40]
    if item["type"] == "photo":
        role = request.form.get("role_" + key, "auto")
        if role in ("cover", "small"):
            item["role"] = role
        if request.form.get("tile_" + key) == "1":
            item["tile"] = True
        if request.form.get("mosaic_" + key) == "1":
            item["mosaic"] = True
            pos = request.form.get("mpos_" + key, "").strip()
            if pos.isdigit() and 1 <= int(pos) <= 99:
                item["mosaic_pos"] = int(pos)
    else:
        shape = request.form.get("format_" + key, "auto")
        if shape in ("vertical", "horizontal"):
            item["format"] = shape
    return item


@app.route("/admin/branding", methods=["GET", "POST"])
@admin_required
def admin_branding():
    db = load_db()
    branding = db.get("branding", {})
    if request.method == "POST":
        branding = {
            "brand_name": request.form.get("brand_name", "Creation Films").strip()[:100],
            "tagline": request.form.get("tagline", "Visual Storytellers").strip()[:160],
            "instagram_handle": request.form.get("instagram_handle", "").strip().lstrip("@")[:80],
            "instagram_url": request.form.get("instagram_url", "").strip()[:300],
            "whatsapp": request.form.get("whatsapp", "").strip()[:40],
            "email": request.form.get("email", "").strip()[:160],
            "location": request.form.get("location", "").strip()[:120],
            "logo_url": db.get("branding", {}).get("logo_url", ""),
        }
        logo_file = request.files.get("logo_file")
        if logo_file and logo_file.filename:
            # Decode and re-encode raster images to avoid unsafe uploads; PNG alpha is preserved.
            try:
                raw = logo_file.read(5 * 1024 * 1024 + 1)
                if len(raw) > 5 * 1024 * 1024:
                    raise ValueError("Logo must be 5 MB or smaller.")
                from PIL import Image
                import io as _io
                im = Image.open(_io.BytesIO(raw))
                if im.format not in ("PNG", "WEBP", "JPEG"):
                    raise ValueError("Please upload a PNG, WEBP, or JPG image.")
                im.load()
                if im.width > 3000 or im.height > 3000:
                    raise ValueError("Logo dimensions must be 3000 × 3000 px or smaller.")
                # Keep transparency when present; convert JPG to PNG for one consistent output.
                if im.mode not in ("RGBA", "LA") and "transparency" not in im.info:
                    im = im.convert("RGBA")
                else:
                    im = im.convert("RGBA")
                # Store the optimized logo inside the branding record itself. This avoids
                # broken logo URLs when Render's ephemeral local filesystem is recycled.
                import io as _logo_io, base64 as _logo_base64
                out = _logo_io.BytesIO()
                im.save(out, format="PNG", optimize=True)
                branding["logo_url"] = "data:image/png;base64," + _logo_base64.b64encode(out.getvalue()).decode("ascii")
            except Exception as exc:
                return render_template("admin_branding.html", branding=branding, saved=False, error=str(exc)), 400
        if request.form.get("remove_logo") == "1":
            old_url = branding.get("logo_url", "")
            if old_url.startswith("/static/uploads/brand-logo-"):
                old_path = Path(app.static_folder) / old_url.split("/static/", 1)[1]
                if old_path.exists() and old_path.is_file():
                    old_path.unlink()
            branding["logo_url"] = ""
        db["branding"] = branding
        save_db(db)
        return redirect(url_for("admin_branding", saved="1"))
    return render_template("admin_branding.html", branding=branding, saved=request.args.get("saved") == "1", error=None)


@app.route("/admin/gallery/new", methods=["POST"])
@admin_required
def admin_new_gallery():
    db = load_db()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "").strip()
    slug = slugify(name)
    if not name or not slug:
        return redirect(url_for("admin_home"))
    if slug not in db["galleries"]:
        db["galleries"][slug] = {
            "name": name,
            "password": password,
            "created": int(time.time()),
            "photos": [],  # each: {id, file_id, message_id, favorite}
            "folders": [],
            "videos": [],  # each: {id, file_id, message_id, filename, mimetype}
            "public_visible": True,
            "cover_photo_id": "",
        }
        save_db(db)
    return redirect(url_for("admin_gallery", slug=slug))


@app.route("/admin/gallery/<slug>/public-settings", methods=["POST"])
@admin_required
def admin_gallery_public_settings(slug):
    db = load_db()
    gallery = db.get("galleries", {}).get(slug)
    if not gallery:
        return "Gallery not found", 404
    gallery["public_visible"] = request.form.get("public_visible") == "1"
    cover_id = request.form.get("cover_photo_id", "").strip()
    valid_ids = {str(p.get("id", "")) for p in gallery.get("photos", [])}
    gallery["cover_photo_id"] = cover_id if cover_id in valid_ids else ""
    save_db(db)
    return redirect(url_for("admin_home", updated=slug))


@app.route("/admin/gallery/<slug>")
@admin_required
def admin_gallery(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Gallery not found", 404
    gallery.setdefault("videos", [])
    return render_template("admin_gallery.html", slug=slug, gallery=gallery, all_galleries=[{"slug": k, "name": v.get("name", k)} for k, v in db["galleries"].items()])


@app.route("/admin/gallery/<slug>/password", methods=["POST"])
@admin_required
def admin_change_password(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    new_password = request.form.get("password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    if not new_password:
        return jsonify({"error": "Password cannot be empty"}), 400
    if new_password != confirm_password:
        return jsonify({"error": "Passwords do not match"}), 400

    gallery["password"] = new_password
    save_db(db)
    return jsonify({"ok": True, "password": new_password})



def _upload_tmp_path(upload_key):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", upload_key or "")
    return UPLOAD_TMP_DIR / f"{safe}.part"


def _media_url(file_id, kind, ttl=86400, filename=None):
    """Create a short-lived HMAC-signed Cloudflare Worker media URL."""
    if not MEDIA_WORKER_URL or not MEDIA_MEDIA_SECRET or not file_id:
        return None
    import base64
    import hashlib
    import hmac
    from urllib.parse import quote

    exp = int(time.time()) + int(ttl)
    message = f"{kind}|{file_id}|{exp}".encode("utf-8")
    sig = hmac.new(
        MEDIA_MEDIA_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    encoded = base64.urlsafe_b64encode(file_id.encode("utf-8")).decode("ascii").rstrip("=")
    url = f"{MEDIA_WORKER_URL}/media/{kind}/{encoded}?exp={exp}&sig={sig}"
    if filename and kind == "raw":
        url += f"&filename={quote(filename)}"
    return url


def _upload_ticket(slug, kind, total_size, ttl=600):
    """Create a short-lived ticket for the Cloudflare direct-upload Worker."""
    import base64
    import hashlib
    import hmac

    if not MEDIA_WORKER_URL or not MEDIA_MEDIA_SECRET:
        raise ValueError("Cloudflare media Worker is not configured")
    if kind not in ("original", "thumb", "preview"):
        raise ValueError("Invalid upload kind")
    if int(total_size) <= 0 or int(total_size) > DIRECT_UPLOAD_MAX:
        raise ValueError("File is too large for direct Telegram upload (max 50 MB)")

    exp = int(time.time()) + int(ttl)
    payload = {
        "slug": slug,
        "kind": kind,
        "exp": exp,
        "max_size": DIRECT_UPLOAD_MAX,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(MEDIA_MEDIA_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{sig}"


@app.route("/admin/gallery/<slug>/upload-tickets", methods=["POST"])
@admin_required
def admin_upload_tickets(slug):
    db = load_db()
    if slug not in db["galleries"]:
        return jsonify({"error": "Gallery not found"}), 404
    try:
        total_size = int(request.form.get("total_size", "0"))
    except ValueError:
        total_size = 0
    if total_size <= 0:
        return jsonify({"error": "Invalid file size"}), 400
    if total_size > DIRECT_UPLOAD_MAX:
        return jsonify({"error": "File is larger than Telegram's 50 MB Bot API limit"}), 413

    try:
        tickets = {
            kind: _upload_ticket(slug, kind, total_size)
            for kind in ("original", "thumb", "preview")
        }
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "upload_url": f"{MEDIA_WORKER_URL}/upload", "tickets": tickets})


@app.route("/admin/gallery/<slug>/upload-register", methods=["POST"])
@admin_required
def admin_upload_register(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    upload_key = request.form.get("upload_key", "").strip()
    filename = request.form.get("filename", "").strip()
    category = request.form.get("category", "").strip()
    try:
        total_size = int(request.form.get("total_size", "0"))
    except ValueError:
        total_size = 0
    file_id = request.form.get("file_id", "").strip()
    thumb_file_id = request.form.get("thumb_file_id", "").strip()
    preview_file_id = request.form.get("preview_file_id", "").strip()
    try:
        message_id = int(request.form.get("message_id", "0"))
    except ValueError:
        message_id = 0
    if not upload_key or not filename or total_size <= 0 or not file_id or not thumb_file_id or not preview_file_id:
        return jsonify({"error": "Invalid upload registration data"}), 400
    if total_size > DIRECT_UPLOAD_MAX:
        return jsonify({"error": "File is larger than Telegram's 50 MB Bot API limit"}), 413

    existing = next((p for p in gallery.get("photos", []) if p.get("upload_key") == upload_key), None)
    if existing:
        return jsonify({"complete": True, "photo": _photo_meta(slug, existing)})

    photo_id = uuid.uuid4().hex[:12]
    photo = {
        "id": photo_id,
        "file_id": file_id,
        "message_id": message_id,
        "thumb_file_id": thumb_file_id,
        "preview_file_id": preview_file_id,
        "favorite": False,
        "filename": secure_filename(filename) or "photo.jpg",
        "category": category,
        "upload_key": upload_key,
        "file_size": total_size,
    }
    with DB_LOCK:
        current_db = load_db()
        current_gallery = current_db["galleries"].get(slug)
        if not current_gallery:
            return jsonify({"error": "Gallery not found"}), 404
        existing = next((p for p in current_gallery.get("photos", []) if p.get("upload_key") == upload_key), None)
        if existing:
            return jsonify({"complete": True, "photo": _photo_meta(slug, existing)})
        current_gallery.setdefault("photos", []).append(photo)
        save_db(current_db)
    return jsonify({"complete": True, "photo": _photo_meta(slug, photo)})


def _photo_meta(slug, photo):
    thumb = _media_url(photo.get("thumb_file_id"), "thumb")
    preview = _media_url(photo.get("preview_file_id"), "preview")
    # Telegram Bot API getFile can download files only up to 20 MB. Keep larger
    # originals on the Render fallback route; thumbnails/previews still use Worker.
    file_size = int(photo.get("file_size") or 0)
    raw = _media_url(photo.get("file_id"), "raw", ttl=3600, filename=photo.get("filename")) if file_size and file_size <= 20 * 1024 * 1024 else None
    return {
        "id": photo["id"],
        "thumb_url": thumb or url_for("photo_thumb", slug=slug, photo_id=photo["id"]),
        "preview_url": preview or url_for("photo_preview", slug=slug, photo_id=photo["id"]),
        "download_url": raw or url_for("photo_download", slug=slug, photo_id=photo["id"]),
        "category": photo.get("category", ""),
    }

def _finalize_resumable_photo(slug, upload_key, filename, category, total_size):
    """Turn a fully received temp file into one gallery photo.

    The upload_key makes this idempotent: if the browser retries after the
    server already finished the Telegram upload, it gets the same photo back.
    """
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        raise ValueError("Gallery not found")

    existing = next((p for p in gallery.get("photos", [])
                     if p.get("upload_key") == upload_key), None)
    if existing:
        return existing

    path = _upload_tmp_path(upload_key)
    if not path.exists():
        raise ValueError("Upload data not found")
    if path.stat().st_size != int(total_size):
        raise ValueError("Upload is incomplete")

    photo_id = uuid.uuid4().hex[:12]
    finalized_ok = False
    try:
        with open(path, "rb") as fh:
            f = FileStorage(
                stream=fh,
                filename=secure_filename(filename) or "photo.jpg",
                content_type="image/jpeg",
            )
            thumb_bytes, preview_bytes = make_image_variants(f)
            f.stream.seek(0)
            file_id, message_id = tg_send_photo(f, caption=gallery["name"])
            thumb_file_id, _ = tg_send_binary(thumb_bytes, f"{photo_id}_thumb.jpg", caption=f"gallery-asset thumb {slug}")
            preview_file_id, _ = tg_send_binary(preview_bytes, f"{photo_id}_preview.jpg", caption=f"gallery-asset preview {slug}")

        save_disk_thumb(slug, photo_id, thumb_bytes)
        save_disk_preview(slug, photo_id, preview_bytes)

        photo = {
            "id": photo_id,
            "file_id": file_id,
            "message_id": message_id,
            "thumb_file_id": thumb_file_id,
            "preview_file_id": preview_file_id,
            "favorite": False,
            "filename": secure_filename(filename) or "photo.jpg",
            "category": category,
            "upload_key": upload_key,
        }
        with DB_LOCK:
            current_db = load_db()
            current_gallery = current_db["galleries"].get(slug)
            if not current_gallery:
                raise ValueError("Gallery not found")
            existing = next((p for p in current_gallery.get("photos", [])
                             if p.get("upload_key") == upload_key), None)
            if existing:
                return existing
            current_gallery.setdefault("photos", []).append(photo)
            save_db(current_db)
        finalized_ok = True
        return photo
    finally:
        # IMPORTANT: keep the .part file when Telegram/processing fails.
        # That lets the next retry continue from the already received bytes
        # instead of forcing the browser to upload the whole original again.
        if finalized_ok:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@app.route("/admin/gallery/<slug>/upload-status/<upload_key>")
@admin_required
def admin_upload_status(slug, upload_key):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    existing = next((p for p in gallery.get("photos", [])
                     if p.get("upload_key") == upload_key), None)
    if existing:
        return jsonify({
            "complete": True,
            "received": 0,
            "total_size": 0,
            "photo": _photo_meta(slug, existing),
        })

    path = _upload_tmp_path(upload_key)
    received = path.stat().st_size if path.exists() else 0
    return jsonify({"complete": False, "received": received})


@app.route("/admin/gallery/<slug>/upload-chunk", methods=["POST"])
@admin_required
def admin_upload_chunk(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    upload_key = request.form.get("upload_key", "").strip()
    filename = request.form.get("filename", "").strip()
    category = request.form.get("category", "").strip()
    try:
        offset = int(request.form.get("offset", "0"))
        total_size = int(request.form.get("total_size", "0"))
    except ValueError:
        return jsonify({"error": "Invalid upload position"}), 400

    chunk = request.files.get("chunk")
    if not upload_key or not filename or total_size <= 0 or offset < 0 or offset > total_size:
        return jsonify({"error": "Invalid upload data"}), 400

    # If a previous request completed Telegram upload but its response was
    # lost, return the already-created photo rather than duplicating it.
    existing = next((p for p in gallery.get("photos", [])
                     if p.get("upload_key") == upload_key), None)
    if existing:
        return jsonify({"complete": True, "received": total_size,
                        "photo": _photo_meta(slug, existing)})

    path = _upload_tmp_path(upload_key)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize writes for the same upload. If the client retries a chunk
    # whose response was lost, do not append the same bytes twice.
    with DB_LOCK:
        current_size = path.stat().st_size if path.exists() else 0
        chunk_bytes = chunk.read() if chunk else b""
        if current_size > offset:
            # The server already received this chunk (or more). Trust the
            # actual on-disk offset and let the browser continue from there.
            received = current_size
        elif current_size < offset:
            return jsonify({"error": "Upload offset mismatch", "received": current_size}), 409
        else:
            with open(path, "ab") as fh:
                fh.write(chunk_bytes)
            received = current_size + len(chunk_bytes)

    if received > total_size:
        return jsonify({"error": "Upload exceeds original file size"}), 400

    if received == total_size:
        try:
            photo = _finalize_resumable_photo(
                slug, upload_key, filename, category, total_size
            )
            return jsonify({
                "complete": True,
                "received": received,
                "photo": _photo_meta(slug, photo),
            })
        except Exception as e:
            # Keep the complete .part file so a retry can finalize it without
            # asking the browser to upload the bytes again.
            return jsonify({
                "error": str(e),
                "complete": False,
                "received": received,
                "ready_to_finalize": True,
            }), 500

    return jsonify({"complete": False, "received": received})


@app.route("/admin/gallery/<slug>/upload-finalize", methods=["POST"])
@admin_required
def admin_upload_finalize(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    upload_key = request.form.get("upload_key", "").strip()
    filename = request.form.get("filename", "").strip()
    category = request.form.get("category", "").strip()
    try:
        total_size = int(request.form.get("total_size", "0"))
    except ValueError:
        total_size = 0
    if not upload_key or not filename or total_size <= 0:
        return jsonify({"error": "Invalid finalize data"}), 400

    existing = next((p for p in gallery.get("photos", []) if p.get("upload_key") == upload_key), None)
    if existing:
        return jsonify({"complete": True, "photo": _photo_meta(slug, existing)})

    path = _upload_tmp_path(upload_key)
    received = path.stat().st_size if path.exists() else 0
    if received != total_size:
        return jsonify({"error": "Upload is incomplete", "received": received, "total_size": total_size}), 409

    try:
        photo = _finalize_resumable_photo(slug, upload_key, filename, category, total_size)
        return jsonify({"complete": True, "received": total_size, "photo": _photo_meta(slug, photo)})
    except Exception as e:
        # Keep the complete temp file. A later retry can finalize it without
        # sending the original bytes again.
        return jsonify({"error": str(e), "complete": False, "received": received, "ready_to_finalize": True}), 500


@app.route("/admin/gallery/<slug>/upload", methods=["POST"])
@admin_required
def admin_upload(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    category = request.form.get("category", "").strip()
    uploaded = request.files.getlist("photos")
    added, photos_meta = [], []

    # Each browser-side file gets a stable upload_key. If the network drops
    # after Telegram accepted the file but before the browser received the
    # response, retrying the same file returns the existing photo instead of
    # creating a duplicate.
    upload_key = request.form.get("upload_key", "").strip()

    for f in uploaded:
        if not f or not f.filename:
            continue

        # Idempotency: safely return an already-completed upload.
        if upload_key:
            existing = next(
                (p for p in gallery.get("photos", []) if p.get("upload_key") == upload_key),
                None,
            )
            if existing:
                added.append(existing["id"])
                photos_meta.append({
                    "id": existing["id"],
                    "thumb_url": url_for("photo_thumb", slug=slug, photo_id=existing["id"]),
                    "preview_url": url_for("photo_preview", slug=slug, photo_id=existing["id"]),
                    "category": existing.get("category", ""),
                })
                continue

        photo_id = uuid.uuid4().hex[:12]
        try:
            thumb_bytes, preview_bytes = make_image_variants(f)
            save_disk_thumb(slug, photo_id, thumb_bytes)
            save_disk_preview(slug, photo_id, preview_bytes)
            file_id, message_id = tg_send_photo(f, caption=gallery["name"])
            thumb_file_id, _ = tg_send_binary(thumb_bytes, f"{photo_id}_thumb.jpg", caption=f"gallery-asset thumb {slug}")
            preview_file_id, _ = tg_send_binary(preview_bytes, f"{photo_id}_preview.jpg", caption=f"gallery-asset preview {slug}")

            photo = {
                "id": photo_id, "file_id": file_id, "message_id": message_id,
                "thumb_file_id": thumb_file_id, "preview_file_id": preview_file_id,
                "favorite": False, "filename": secure_filename(f.filename),
                "category": category,
                "upload_key": upload_key,
            }
            with DB_LOCK:
                current_db = load_db()
                current_gallery = current_db["galleries"].get(slug)
                if not current_gallery:
                    return jsonify({"error": "Gallery not found", "added": added}), 404
                # Re-check under the lock in case a retry raced with the first request.
                if upload_key:
                    existing = next(
                        (p for p in current_gallery.get("photos", []) if p.get("upload_key") == upload_key),
                        None,
                    )
                    if existing:
                        added.append(existing["id"])
                        photos_meta.append({
                            "id": existing["id"],
                            "thumb_url": url_for("photo_thumb", slug=slug, photo_id=existing["id"]),
                            "preview_url": url_for("photo_preview", slug=slug, photo_id=existing["id"]),
                            "category": existing.get("category", ""),
                        })
                        continue
                current_gallery.setdefault("photos", []).append(photo)
                save_db(current_db)

            added.append(photo_id)
            photos_meta.append({
                "id": photo_id,
                "thumb_url": url_for("photo_thumb", slug=slug, photo_id=photo_id),
                "preview_url": url_for("photo_preview", slug=slug, photo_id=photo_id),
                "category": category,
            })
        except Exception as e:
            return jsonify({"error": str(e), "added": added}), 500

    return jsonify({"added": added, "total": len(load_db()["galleries"][slug]["photos"]), "photos_meta": photos_meta})


@app.route("/admin/gallery/<slug>/video-upload", methods=["POST"])
@admin_required
def admin_upload_video(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    gallery.setdefault("videos", [])
    uploaded = request.files.getlist("videos")
    added = []
    videos_meta = []

    for f in uploaded:
        if not f or not f.filename:
            continue
        if not (f.mimetype or "").startswith("video/"):
            continue
        try:
            file_id, message_id = tg_send_video(f, caption=gallery["name"])
        except Exception as e:
            return jsonify({"error": str(e), "added": added}), 500

        video = {
            "id": uuid.uuid4().hex[:12],
            "file_id": file_id,
            "message_id": message_id,
            "filename": secure_filename(f.filename) or "video.mp4",
            "mimetype": f.mimetype or "video/mp4",
        }
        gallery["videos"].append(video)
        added.append(video["id"])
        videos_meta.append({
            "id": video["id"],
            "video_url": url_for("video_proxy", slug=slug, video_id=video["id"]),
            "filename": video["filename"],
        })

    save_db(db)
    return jsonify({
        "added": added,
        "total": len(gallery["videos"]),
        "videos_meta": videos_meta
    })


@app.route("/admin/gallery/<slug>/youtube-video", methods=["POST"])
@admin_required
def admin_add_youtube_video(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    raw_url = (request.form.get("youtube_url") or "").strip()
    title = (request.form.get("title") or "Wedding Film").strip()[:120] or "Wedding Film"
    from urllib.parse import urlparse, parse_qs
    try:
        parsed = urlparse(raw_url if "://" in raw_url else "https://" + raw_url)
        host = (parsed.hostname or "").lower()
        video_id = None
        is_short = parsed.path.startswith("/shorts/")
        if host in ("youtu.be", "www.youtu.be"):
            video_id = parsed.path.strip("/").split("/")[0]
        elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [None])[0]
            elif parsed.path.startswith(("/embed/", "/shorts/", "/live/")):
                video_id = parsed.path.split("/")[2]
        if not video_id or not __import__("re").fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError("Please enter a valid YouTube video link.")
    except Exception:
        return jsonify({"error": "Please enter a valid YouTube video link."}), 400

    gallery.setdefault("videos", [])
    video = {"id": uuid.uuid4().hex[:12], "source": "youtube", "youtube_id": video_id, "filename": title}
    orientation = "vertical" if is_short else detect_youtube_orientation(video_id)
    if orientation:
        video["orientation"] = orientation
    gallery["videos"].append(video)
    save_db(db)
    return jsonify({"ok": True, "video": {"id": video["id"], "source": "youtube", "youtube_id": video_id, "title": title}})


@app.route("/admin/gallery/<slug>/video/<video_id>/delete", methods=["POST"])
@admin_required
def admin_delete_video(slug, video_id):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    gallery.setdefault("videos", [])
    gallery["videos"] = [v for v in gallery["videos"] if v["id"] != video_id]
    save_db(db)
    return jsonify({"ok": True})


@app.route("/admin/gallery/<slug>/rename", methods=["POST"])
@admin_required
def admin_rename_gallery(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Gallery name cannot be empty"}), 400
    gallery["name"] = name
    save_db(db)
    return jsonify({"ok": True, "name": name})


@app.route("/admin/gallery/<slug>/photos/delete", methods=["POST"])
@admin_required
def admin_delete_photos(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    ids = request.get_json(silent=True) or {}
    ids = set(ids.get("ids", []))
    if not ids:
        return jsonify({"error": "No photos selected"}), 400
    before = len(gallery["photos"])
    gallery["photos"] = [p for p in gallery["photos"] if p["id"] not in ids]
    for _key in ("cover_photo_id", "public_cover_photo_id"):
        if gallery.get(_key) in ids:
            gallery[_key] = None
    save_db(db)
    return jsonify({"ok": True, "removed": before - len(gallery["photos"]), "ids": list(ids)})


@app.route("/admin/gallery/<slug>/photo/<photo_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(slug, photo_id):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    gallery["photos"] = [p for p in gallery["photos"] if p["id"] != photo_id]
    for _key in ("cover_photo_id", "public_cover_photo_id"):
        if str(gallery.get(_key)) == str(photo_id):
            gallery[_key] = None
    save_db(db)
    return jsonify({"ok": True})


@app.route("/admin/gallery/<slug>/photo/<photo_id>/set-cover", methods=["POST"])
@admin_required
def admin_set_cover(slug, photo_id):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    if not any(p["id"] == photo_id for p in gallery["photos"]):
        return jsonify({"error": "Photo not found"}), 404
    gallery["cover_photo_id"] = photo_id
    save_db(db)
    return jsonify({"ok": True, "cover_photo_id": photo_id})


@app.route("/admin/gallery/<slug>/photo/<photo_id>/set-public-cover", methods=["POST"])
@admin_required
def admin_set_public_cover(slug, photo_id):
    """Choose the separate cover image shown on the public Client Gallery cards."""
    db = load_db()
    gallery = db.get("galleries", {}).get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    if not any(str(p.get("id")) == str(photo_id) for p in gallery.get("photos", [])):
        return jsonify({"error": "Photo not found"}), 404
    gallery["public_cover_photo_id"] = str(photo_id)
    save_db(db)
    return jsonify({"ok": True, "public_cover_photo_id": str(photo_id)})


@app.route("/admin/gallery/<slug>/delete", methods=["POST"])
@admin_required
def admin_delete_gallery(slug):
    db = load_db()
    db["galleries"].pop(slug, None)
    save_db(db)
    return redirect(url_for("admin_home"))


# ---------------------------------------------------------------------
# Collection/folder management (categories are stored on photo records)
# ---------------------------------------------------------------------

@app.route("/admin/gallery/<slug>/folders", methods=["GET", "POST"])
@admin_required
def admin_folders(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    gallery.setdefault("folders", [])
    if request.method == "GET":
        names = set(str(x).strip() for x in gallery.get("folders", []) if str(x).strip())
        names.update(str(p.get("category", "")).strip() for p in gallery.get("photos", []) if str(p.get("category", "")).strip())
        return jsonify({"folders": sorted(names, key=str.casefold)})
    name = (request.form.get("name") or "").strip()
    if not name or len(name) > 80:
        return jsonify({"error": "Folder name must be 1–80 characters"}), 400
    names = {str(x).casefold() for x in gallery.get("folders", [])}
    names.update(str(p.get("category", "")).strip().casefold() for p in gallery.get("photos", []))
    if name.casefold() in names:
        return jsonify({"error": "That folder already exists"}), 409
    gallery["folders"].append(name)
    save_db(db)
    return jsonify({"ok": True, "folder": name})

@app.route("/admin/gallery/<slug>/folders/rename", methods=["POST"])
@admin_required
def admin_rename_folder(slug):
    db = load_db(); gallery = db["galleries"].get(slug)
    if not gallery: return jsonify({"error": "Gallery not found"}), 404
    old = (request.form.get("old_name") or "").strip(); new = (request.form.get("new_name") or "").strip()
    if not old or not new or len(new) > 80: return jsonify({"error": "Enter valid old and new folder names"}), 400
    names = {str(x).casefold() for x in gallery.get("folders", [])}
    names.update(str(p.get("category", "")).strip().casefold() for p in gallery.get("photos", []) if str(p.get("category", "")).strip())
    if new.casefold() != old.casefold() and new.casefold() in names: return jsonify({"error": "That folder already exists"}), 409
    gallery["folders"] = [new if str(x).casefold() == old.casefold() else x for x in gallery.get("folders", [])]
    for p in gallery.get("photos", []):
        if str(p.get("category", "")).strip().casefold() == old.casefold(): p["category"] = new
    save_db(db); return jsonify({"ok": True, "folder": new})

@app.route("/admin/gallery/<slug>/folders/delete", methods=["POST"])
@admin_required
def admin_delete_folder(slug):
    db = load_db(); gallery = db["galleries"].get(slug)
    if not gallery: return jsonify({"error": "Gallery not found"}), 404
    name = (request.form.get("name") or "").strip()
    action = (request.form.get("action") or "keep").strip()
    if action not in ("keep", "uncategorize"): return jsonify({"error": "Photos can only be kept or moved to Uncategorized"}), 400
    gallery["folders"] = [x for x in gallery.get("folders", []) if str(x).casefold() != name.casefold()]
    if action == "uncategorize":
        for p in gallery.get("photos", []):
            if str(p.get("category", "")).strip().casefold() == name.casefold(): p["category"] = ""
    save_db(db); return jsonify({"ok": True})

@app.route("/admin/gallery/<slug>/photos/move", methods=["POST"])
@admin_required
def admin_move_photos(slug):
    db = load_db()
    source = db["galleries"].get(slug)
    if not source:
        return jsonify({"error": "Source album not found"}), 404
    payload = request.get_json(silent=True) or {}
    ids = set(str(x) for x in payload.get("ids", []))
    destination = str(payload.get("folder", "")).strip()
    target_slug = str(payload.get("destination_gallery") or slug)
    target = db["galleries"].get(target_slug)
    if not target:
        return jsonify({"error": "Destination album not found"}), 404
    if not ids:
        return jsonify({"error": "Select at least one photo"}), 400

    if target_slug == slug:
        known = set(str(x).casefold() for x in target.get("folders", []))
        known.update(str(p.get("category", "")).strip().casefold() for p in target.get("photos", []) if str(p.get("category", "")).strip())
        if destination and destination.casefold() not in known:
            return jsonify({"error": "Folder does not exist; create it first"}), 400
        moved = 0
        for photo in source.get("photos", []):
            if str(photo.get("id")) in ids:
                photo["category"] = destination
                moved += 1
    else:
        moving = [photo for photo in source.get("photos", []) if str(photo.get("id")) in ids]
        if not moving:
            return jsonify({"error": "Selected photos were not found"}), 404
        target.setdefault("photos", [])
        existing_ids = {str(photo.get("id")) for photo in target["photos"]}
        for photo in moving:
            if str(photo.get("id")) not in existing_ids:
                photo = dict(photo)
                photo["category"] = ""
                target["photos"].append(photo)
        source["photos"] = [photo for photo in source.get("photos", []) if str(photo.get("id")) not in ids]
        moved = len(moving)
        # Clear stale per-album disk caches; assets can be regenerated from stored Telegram IDs.
        for photo in moving:
            for cache_fn in (thumb_disk_path, preview_disk_path):
                try:
                    cache_fn(slug, photo.get("id")).unlink(missing_ok=True)
                except Exception:
                    pass
    save_db(db)
    return jsonify({"ok": True, "moved": moved, "destination_gallery": target_slug})

# ---------------------------------------------------------------------
# Public Creation Films website (kept separate from gallery/admin systems)
# ---------------------------------------------------------------------

PHOTO_CATEGORIES = ["Wedding", "Pre-Wedding", "Engagement", "Maternity", "Newborn & Baby", "Events"]
VIDEO_CATEGORIES = ["Films", "Reels", "Highlights"]
SERVICE_CARDS = [
    ("Wedding", "Weddings", "01 · Photography"),
    ("Pre-Wedding", "Pre-Weddings", "02 · Togetherness"),
    ("Newborn & Baby", "Little Ones", "03 · Beginnings"),
    ("Events", "Celebrations", "04 · Events"),
]
CATEGORY_TITLES = {"Wedding": "Weddings", "Pre-Wedding": "Pre-Weddings", "Engagement": "Engagements", "Maternity": "Maternity",
                   "Newborn & Baby": "Newborn & Baby", "Events": "Events"}


def cat_slug(cat):
    return re.sub(r"-+", "-", "".join(c.lower() if c.isalnum() else "-" for c in cat)).strip("-")


GENERIC_VIDEO_TITLES = {"", "wedding film", "film", "video", "reel", "highlight"}


def normalize_video_category(cat):
    """Map any stored video category onto Films / Reels / Highlights (old 'Reels & Highlights' counts as Reels)."""
    c = (cat or "").lower()
    if "highlight" in c:
        return "Highlights"
    if "reel" in c or "short" in c:
        return "Reels"
    return "Films"


@app.template_filter("hero_title")
def hero_title_filter(text):
    """Italicise part of the headline. Use *words* for control, otherwise the last two words are italic."""
    text = (text or "").strip()
    if "*" in text:
        parts = re.split(r"\*(.+?)\*", str(escape(text)))
        return Markup("".join(f"<em>{p}</em>" if i % 2 else p for i, p in enumerate(parts)))
    words = text.split()
    if len(words) >= 3:
        return Markup(f"{escape(' '.join(words[:-2]))} <em>{escape(' '.join(words[-2:]))}</em>")
    return escape(text)


def _find_video(galleries, slug, video_id):
    gallery = galleries.get(slug) or {}
    return next((v for v in gallery.get("videos", []) if str(v.get("id")) == str(video_id) and v.get("youtube_id")), None)


def all_youtube_videos(db):
    """Every YouTube video across galleries, for the admin pickers."""
    out = []
    for slug, g in db.get("galleries", {}).items():
        for v in g.get("videos", []):
            if v.get("youtube_id"):
                out.append({"key": f"{slug}:{v.get('id')}", "slug": slug, "gallery_name": g.get("name", slug),
                            "title": v.get("filename") or v.get("title") or "Film", "youtube_id": v["youtube_id"]})
    return out


def gallery_cover_info(slug, g):
    """Resolve the cover for a client-gallery card. Always returns something usable.

    Order: public card cover, hero cover, first photo, first YouTube film thumbnail, then a
    pastel monogram placeholder (the template shows that when `url` is empty or fails to load).
    """
    photos = g.get("photos", []) or []
    ids = [str(p.get("id")) for p in photos]
    chosen = None
    for key in ("public_cover_photo_id", "cover_photo_id"):
        cid = g.get(key)
        if cid and str(cid) in ids:
            chosen = str(cid)
            break
    if not chosen and ids:
        chosen = ids[0]
    yt = next((v.get("youtube_id") for v in g.get("videos", []) if v.get("youtube_id")), None)
    yt_url = f"https://img.youtube.com/vi/{yt}/maxresdefault.jpg" if yt else None
    yt_fb = f"https://img.youtube.com/vi/{yt}/hqdefault.jpg" if yt else None
    if chosen:
        return {"url": url_for("client_cover", slug=slug, v=chosen), "fb": yt_fb}
    return {"url": yt_url, "fb": yt_fb}


_ORIENT_FAILED = {}  # youtube_id -> time of last failed lookup (avoids retrying on every page view)


def detect_youtube_orientation(yt_id):
    """Return "vertical", "horizontal" or None, using YouTube's public oEmbed endpoint (no API key)."""
    if time.time() - _ORIENT_FAILED.get(yt_id, 0) < 600:
        return None
    try:
        r = requests.get("https://www.youtube.com/oembed", timeout=3,
                         params={"url": f"https://www.youtube.com/watch?v={yt_id}", "format": "json"})
        if r.ok:
            d = r.json()
            w, h = int(d.get("width") or 0), int(d.get("height") or 0)
            if w and h:
                return "vertical" if h > w else "horizontal"
    except Exception as e:
        print(f"oEmbed lookup failed for {yt_id}: {e}")
    _ORIENT_FAILED[yt_id] = time.time()
    return None


def fill_missing_orientations(db, wanted):
    """Detect and store the shape of videos that do not have one yet. `wanted` = [(slug, video_id), ...]."""
    galleries = db.get("galleries", {})
    todo = []
    for slug, vid in wanted:
        v = _find_video(galleries, slug, vid)
        if v and v.get("orientation") not in ("vertical", "horizontal"):
            todo.append(v)
    if not todo:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda v: detect_youtube_orientation(v["youtube_id"]), todo))
    changed = False
    for v, o in zip(todo, results):
        if o:
            v["orientation"] = o
            changed = True
    if changed:
        save_db(db)


def _site_photo_b64(file_storage, max_px=900, quality=76, limit_mb=12):
    """Resize an uploaded photo and return it as base64 JPEG (stored inside the database)."""
    raw = file_storage.read(limit_mb * 1024 * 1024 + 1)
    if len(raw) > limit_mb * 1024 * 1024:
        raise ValueError(f"Photo must be {limit_mb} MB or smaller.")
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return base64.b64encode(out.getvalue()).decode("ascii")


def public_testimonials(db):
    """Testimonials ready for the template (paragraphs split, photo URL built)."""
    out = []
    for t in sorted(db.get("testimonials", []), key=lambda x: (x.get("order", 0), x.get("created", 0))):
        paras = [p.strip() for p in re.split(r"\n\s*\n", t.get("text", "") or "") if p.strip()]
        out.append({
            "id": t["id"], "names": t.get("names", ""), "title": t.get("title", ""), "paras": paras,
            "signature": t.get("signature", ""),
            "photo": url_for("site_testimonial_photo", tid=t["id"], v=t.get("photo_v", 0)) if t.get("photo_b64") else None,
        })
    return out


def build_public_context(db):
    galleries = db.get("galleries", {})
    items = db.get("portfolio_items", [])
    website = db.get("website", {})

    def thumb(it):
        return url_for("portfolio_media_thumb", slug=it["slug"], photo_id=it["media_id"])

    pf_photos = [i for i in items if i.get("type") == "photo"]

    # Home "Iconic" grid: photos ticked in Portfolio Manager (ordered by their position number,
    # then by portfolio order). If nothing is ticked yet, fall back to the first 11 photos.
    picked = [(i.get("mosaic_pos") or 999, n, i) for n, i in enumerate(pf_photos) if i.get("mosaic")]
    picked.sort(key=lambda t: (t[0], t[1]))
    mosaic_photos = [t[2] for t in picked[:11]] if picked else pf_photos[:11]

    # ---- videos, split into Films / Reels / Highlights ----
    # The real shape of the video decides where it goes: vertical = Reels, and a horizontal
    # video that was tagged "Reel" is shown under Highlights instead.
    wanted = [(i.get("slug"), i.get("media_id")) for i in items if i.get("type") == "video" and i.get("format") not in ("vertical", "horizontal")]
    hero_parts = (website.get("hero_video_key") or "").split(":", 1)
    if website.get("hero_mode") in ("film", "reel", "highlight") and len(hero_parts) == 2:
        wanted.append((hero_parts[0], hero_parts[1]))
    fill_missing_orientations(db, wanted)

    videos = {"Films": [], "Reels": [], "Highlights": []}
    for it in items:
        if it.get("type") != "video":
            continue
        g = galleries.get(it.get("slug"), {})
        v = _find_video(galleries, it.get("slug"), it.get("media_id"))
        if not v:
            continue
        kind = normalize_video_category(it.get("category"))
        orient = it.get("format") if it.get("format") in ("vertical", "horizontal") else v.get("orientation")
        vertical = orient == "vertical"
        if vertical:
            kind = "Reels"
        elif kind == "Reels":
            kind = "Highlights"
        raw_title = (it.get("title") or v.get("filename") or "").strip()
        videos[kind].append({
            "yt": v["youtube_id"],
            "name": g.get("name") or raw_title or "Film",
            "sub": "" if raw_title.lower() in GENERIC_VIDEO_TITLES or raw_title == g.get("name") else raw_title,
            "kind": kind,
            "vertical": vertical,
            "thumb": f"https://img.youtube.com/vi/{v['youtube_id']}/maxresdefault.jpg",
        })

    # ---- hero: photo, film, reel or highlight ----
    hero = {"mode": "photo", "yt": None, "photo": None, "fb": None, "soft": False, "vertical": False}
    if website.get("hero_mode") in ("film", "reel", "highlight"):
        parts = (website.get("hero_video_key") or "").split(":", 1)
        v = _find_video(galleries, parts[0], parts[1]) if len(parts) == 2 else None
        if v:
            orient = v.get("orientation")
            hero.update(mode=website["hero_mode"], yt=v["youtube_id"],
                        vertical=(orient == "vertical") or (orient is None and website["hero_mode"] == "reel"))
    if website.get("hero_image_b64"):
        hero["photo"] = url_for("site_hero_image", v=website.get("hero_image_v", 0))
    elif website.get("hero_image_url"):
        hero["photo"] = website["hero_image_url"]
    elif pf_photos and not hero["yt"]:
        hero["photo"] = thumb(pf_photos[0])
    if hero["yt"] and not hero["photo"]:
        hero["photo"] = f"https://img.youtube.com/vi/{hero['yt']}/maxresdefault.jpg"
        hero["fb"] = f"https://img.youtube.com/vi/{hero['yt']}/hqdefault.jpg"
        hero["soft"] = hero["vertical"]

    # ---- client gallery cards ----
    cards = []
    for slug, g in galleries.items():
        if not g.get("public_visible", True):
            continue
        cover = gallery_cover_info(slug, g)
        name = g.get("name") or slug.replace("-", " ").title()
        cards.append({"slug": slug, "name": name, "initial": (name.strip()[:1] or "C").upper(),
                      "photos": len(g.get("photos", [])), "films": len(g.get("videos", [])),
                      "cover": cover["url"], "fb": cover["fb"]})

    # ---- photo albums: one album per (category, gallery) ----
    # Admin can mark a photo as the album "cover" (big photo), as a "small" photo (shown beside the
    # cover on Home), and/or as the category "tile" photo (the Collections tile on Home).
    albums_by_cat = OrderedDict()
    for it in pf_photos:
        cat = it.get("category", "Wedding")
        albums_by_cat.setdefault(cat, OrderedDict()).setdefault(it["slug"], []).append(it)
    albums = []
    tile_by_cat = {}
    for cat, groups in albums_by_cat.items():
        for slug, group in groups.items():
            g = galleries.get(slug, {})
            cover = next((x for x in group if x.get("role") == "cover"), group[0])
            rest = [x for x in group if x is not cover]
            smalls = [x for x in rest if x.get("role") == "small"] + [x for x in rest if x.get("role") != "small"]
            for x in group:
                if x.get("tile") and cat not in tile_by_cat:
                    tile_by_cat[cat] = x
            albums.append({
                "cat": cat, "cat_slug": cat_slug(cat), "slug": slug,
                "name": g.get("name") or group[0].get("title", "Celebration"),
                "date": next((x.get("event_date") for x in group if x.get("event_date")), ""),
                "count": len(group), "cover": thumb(cover), "smalls": [thumb(x) for x in smalls[:2]],
                "photos": [thumb(x) for x in group],
                "url": url_for("public_portfolio_album", cat_slug_=cat_slug(cat), album_slug=slug),
            })

    # ---- recent celebrations (Home) ----
    wedding_albums = [al for al in albums if al["cat"] == "Wedding"] or albums
    stories = [{"name": al["name"], "date": al["date"], "category": al["cat"], "lead": al["cover"],
                "smalls": al["smalls"], "url": al["url"]} for al in wedding_albums[:3]]
    if not stories:  # nothing chosen in Portfolio Manager yet: fall back to the newest client galleries
        for c in [c for c in cards if c["cover"]][:3]:
            stories.append({"name": c["name"], "date": "", "category": "Wedding", "lead": c["cover"], "smalls": [],
                            "fb": c["fb"], "url": url_for("public_client_galleries")})

    # ---- portfolio categories (overview page + Home collection tiles) ----
    categories = []
    for cat in PHOTO_CATEGORIES + [c for c in albums_by_cat if c not in PHOTO_CATEGORIES]:
        cat_albums = [al for al in albums if al["cat"] == cat]
        if not cat_albums:
            continue
        categories.append({"cat": cat, "slug": cat_slug(cat), "title": CATEGORY_TITLES.get(cat, cat), "kind": "photo",
                           "count": len(cat_albums), "unit": "album", "img": thumb(tile_by_cat[cat]) if cat in tile_by_cat else cat_albums[0]["cover"]})
    for kind in VIDEO_CATEGORIES:
        if videos[kind]:
            categories.append({"cat": kind, "slug": cat_slug(kind), "title": kind, "kind": "video", "count": len(videos[kind]),
                               "unit": "video", "img": videos[kind][0]["thumb"]})
    by_cat = {c["cat"]: c for c in categories}

    services = []
    for cat, title, eyebrow in SERVICE_CARDS:
        c = by_cat.get(cat)
        services.append({"cat": cat, "title": title, "eyebrow": eyebrow, "img": c["img"] if c else None,
                         "url": url_for("public_portfolio_category", cat_slug_=c["slug"]) if c else url_for("public_portfolio")})

    show = lambda key: website.get(key, True) is not False
    intro_photos = [url_for("site_intro_photo", n=n, v=website.get(f"intro_photo{n}_v", 0))
                    if website.get(f"intro_photo{n}_b64") else None for n in (1, 2)]
    fallback = [thumb(i) for i in (pf_photos[-2:] if len(pf_photos) >= 13 else pf_photos[:2])]
    for k in range(2):
        if not intro_photos[k] and k < len(fallback):
            intro_photos[k] = fallback[k]
    return {
        "intro_photos": intro_photos,
        "hero": hero, "stories": stories, "services": services, "cards": cards,
        "categories": categories, "albums": albums, "videos": videos,
        "films": videos["Films"], "reels": videos["Reels"], "highlights": videos["Highlights"],
        "mosaic": [thumb(i) for i in mosaic_photos],
        "gram": [thumb(i) for i in (pf_photos[11:23] if len(pf_photos) >= 18 else pf_photos[:12])],
        "testimonials": public_testimonials(db),
        "story_photo": thumb(pf_photos[0]) if pf_photos else None,
        "show": {"celebrations": show("home_show_celebrations"), "films": show("home_show_films"),
                 "reels": show("home_show_reels"), "highlights": show("home_show_highlights"),
                 "collections": show("home_show_collections"), "instagram": show("home_show_instagram"),
                 "mosaic": show("home_show_mosaic"), "intro": show("home_show_intro"), "testimonials": show("home_show_testimonials")},
    }


def render_public(page, pick=None, **extra):
    """Render a public page. `pick(ctx)` may return extra template values, or None to redirect to /portfolio."""
    db = load_db()
    ctx = build_public_context(db)
    if pick:
        found = pick(ctx)
        if found is None:
            return redirect(url_for("public_portfolio"))
        extra.update(found)
    return render_template("public_site.html", page=page, branding=db.get("branding", {}),
                           website=db.get("website", {}), ctx=ctx, **extra)


@app.route("/")
def public_home():
    return render_public("home")

@app.route("/portfolio")
def public_portfolio():
    legacy = request.args.get("cat")
    if legacy:
        return redirect(url_for("public_portfolio_category", cat_slug_=cat_slug(legacy)))
    return render_public("portfolio")

@app.route("/portfolio/<cat_slug_>")
def public_portfolio_category(cat_slug_):
    return render_public("portfolio-cat", pick=lambda ctx: (
        {"current": next(c for c in ctx["categories"] if c["slug"] == cat_slug_)}
        if any(c["slug"] == cat_slug_ for c in ctx["categories"]) else None))

@app.route("/portfolio/<cat_slug_>/<album_slug>")
def public_portfolio_album(cat_slug_, album_slug):
    def pick(ctx):
        album = next((a for a in ctx["albums"] if a["cat_slug"] == cat_slug_ and a["slug"] == album_slug), None)
        cat = next((c for c in ctx["categories"] if c["slug"] == cat_slug_), None)
        return {"current": cat, "album": album} if album and cat else None
    return render_public("portfolio-album", pick=pick)

@app.route("/about")
def public_about():
    return render_public("about")

@app.route("/contact")
def public_contact():
    return render_public("contact")


@app.route("/site-media/intro/<int:n>")
def site_intro_photo(n):
    b64 = load_db_ro().get("website", {}).get(f"intro_photo{n}_b64") if n in (1, 2) else None
    if not b64:
        return "Not found", 404
    return Response(base64.b64decode(b64), content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/testimonials")
def public_testimonials_page():
    return render_public("testimonials")


@app.route("/site-media/testimonial/<tid>")
def site_testimonial_photo(tid):
    t = next((x for x in load_db_ro().get("testimonials", []) if x.get("id") == tid), None)
    if not t or not t.get("photo_b64"):
        return "Not found", 404
    return Response(base64.b64decode(t["photo_b64"]), content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/admin/testimonials", methods=["GET", "POST"])
@admin_required
def admin_testimonials():
    db = load_db()
    items = db.setdefault("testimonials", [])
    error = None
    if request.method == "POST":
        action = request.form.get("action", "")
        tid = request.form.get("tid", "")
        if action == "delete":
            db["testimonials"] = [t for t in items if t.get("id") != tid]
        elif action in ("add", "save"):
            names = request.form.get("names", "").strip()[:120]
            if not names:
                error = "Please enter the couple's names."
            else:
                if action == "add":
                    t = {"id": uuid.uuid4().hex[:12], "created": int(time.time())}
                    items.append(t)
                else:
                    t = next((x for x in items if x.get("id") == tid), None)
                if t is None:
                    error = "That testimonial no longer exists."
                else:
                    try:
                        order = int(request.form.get("order", "0") or 0)
                    except ValueError:
                        order = 0
                    t.update(names=names, title=request.form.get("title", "").strip()[:160],
                             text=request.form.get("text", "").strip()[:4000],
                             signature=request.form.get("signature", "").strip()[:80], order=order)
                    photo = request.files.get("photo")
                    if photo and photo.filename:
                        try:
                            t["photo_b64"] = _site_photo_b64(photo)
                            t["photo_v"] = int(time.time())
                        except Exception as exc:
                            error = "Photo problem: " + str(exc)
                    elif request.form.get("remove_photo") == "1":
                        t["photo_b64"] = ""
        db["testimonials"] = db.get("testimonials", items)
        save_db(db)
        if not error:
            return redirect(url_for("admin_testimonials", saved="1"))
    return render_template("admin_testimonials.html", items=db.get("testimonials", []),
                           saved=request.args.get("saved") == "1", error=error)


@app.route("/site-media/hero")
def site_hero_image():
    b64 = load_db_ro().get("website", {}).get("hero_image_b64")
    if not b64:
        return "Not found", 404
    return Response(base64.b64decode(b64), content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/portfolio-media/<slug>/<photo_id>/thumb")
def portfolio_media_thumb(slug, photo_id):
    db = load_db_ro()
    allowed = any(i.get("type") == "photo" and i.get("slug") == slug and str(i.get("media_id")) == str(photo_id) for i in db.get("portfolio_items", []))
    gallery = db.get("galleries", {}).get(slug)
    if not allowed or not gallery:
        return "Not found", 404
    photo = next((p for p in gallery.get("photos", []) if str(p.get("id")) == str(photo_id)), None)
    if not photo:
        return "Not found", 404
    try:
        data = _photo_thumb_bytes(slug, photo)
        return Response(data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        print(f"Portfolio thumbnail error: {e}")
        return "Thumbnail unavailable", 503


def _photo_thumb_bytes(slug, photo):
    """Thumbnail bytes for a photo, as reliably as possible.

    Disk cache first, then the stored Telegram thumbnail, then the stored preview, and only as a
    last resort rebuild both from the Telegram original. Each step is tried even if the one before
    it fails, which is what keeps cover photos from silently disappearing.
    """
    photo_id = photo["id"]
    disk_path = thumb_disk_path(slug, photo_id)
    if disk_path.exists():
        return disk_path.read_bytes()
    last_error = None
    for key in ("thumb_file_id", "preview_file_id"):
        file_id = photo.get(key)
        if not file_id:
            continue
        try:
            data = tg_download_bytes(file_id)
            save_disk_thumb(slug, photo_id, data)
            return data
        except Exception as e:  # try the next source
            last_error = e
            FILE_PATH_CACHE.pop(file_id, None)
    try:
        thumb_id, _ = ensure_photo_assets(slug, photo)
        if disk_path.exists():
            return disk_path.read_bytes()
        data = tg_download_bytes(thumb_id)
        save_disk_thumb(slug, photo_id, data)
        return data
    except Exception as e:
        raise last_error or e


@app.route("/client-cover/<slug>")
def client_cover(slug):
    """Public cover image for a client-gallery card (never exposes any other photo)."""
    db = load_db_ro()
    gallery = db.get("galleries", {}).get(slug)
    if not gallery or not gallery.get("public_visible", True):
        return "Not found", 404
    photos = gallery.get("photos", []) or []
    by_id = {str(p.get("id")): p for p in photos}
    order = []
    for key in ("public_cover_photo_id", "cover_photo_id"):
        cid = gallery.get(key)
        if cid and str(cid) in by_id and by_id[str(cid)] not in order:
            order.append(by_id[str(cid)])
    for p in photos[:4]:
        if p not in order:
            order.append(p)
    for photo in order[:4]:
        try:
            data = _photo_thumb_bytes(slug, photo)
            return Response(data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})
        except Exception as e:
            print(f"Client cover error for {slug}/{photo.get('id')}: {e}")
    return "Cover unavailable", 404


@app.route("/client-galleries", methods=["GET", "POST"])
def public_client_galleries():
    error = None
    if request.method == "POST":
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", request.form.get("gallery_code", "").strip())
        if slug and slug in load_db().get("galleries", {}):
            return redirect(url_for("client_gallery", slug=slug))
        error = "We couldn't find that gallery code. Please check it and try again."
    return render_public("client-galleries", error=error)

# ---------------------------------------------------------------------
# Client-facing gallery
# ---------------------------------------------------------------------

@app.route("/gallery/<slug>", methods=["GET", "POST"])
def client_gallery(slug):
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Gallery not found", 404

    session_key = f"unlocked_{slug}"
    error = None

    if request.method == "POST":
        if request.form.get("password") == gallery["password"]:
            session[session_key] = True
        else:
            error = "Wrong password"

    if not session.get(session_key):
        return render_template("gallery_login.html", gallery=gallery, error=error, cover_url=gallery_cover_info(slug, gallery)["url"] if gallery.get("public_visible", True) else None)

    return render_template("gallery.html", slug=slug, gallery=gallery, branding=db.get("branding", {}))


@app.route("/api/gallery/<slug>/photos")
def api_gallery_photos(slug):
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "not found"}), 404
    if not session.get(f"unlocked_{slug}"):
        return jsonify({"error": "locked"}), 403
    photos = []
    for p in gallery["photos"]:
        meta = _photo_meta(slug, p)
        meta["favorite"] = p.get("favorite", False)
        photos.append(meta)
    videos = [
        {
            "id": v["id"],
            "filename": v.get("filename", "video.mp4"),
            "source": v.get("source", "upload"),
            "youtube_id": v.get("youtube_id"),
            "video_url": ("https://www.youtube-nocookie.com/embed/" + v["youtube_id"] if v.get("source") == "youtube" else url_for("video_proxy", slug=slug, video_id=v["id"])),
            "download_url": (None if v.get("source") == "youtube" else url_for("video_download", slug=slug, video_id=v["id"])),
        }
        for v in gallery.get("videos", [])
    ]
    return jsonify({
        "name": gallery["name"],
        "photos": photos,
        "folders": sorted(set([str(x).strip() for x in gallery.get("folders", []) if str(x).strip()] + [str(p.get("category", "")).strip() for p in gallery.get("photos", []) if str(p.get("category", "")).strip()]), key=str.casefold),
        "videos": videos,
        "cover_photo_id": gallery.get("cover_photo_id"),
    })


@app.route("/api/gallery/<slug>/photo/<photo_id>/favorite", methods=["POST"])
def api_toggle_favorite(slug, photo_id):
    if not session.get(f"unlocked_{slug}"):
        return jsonify({"error": "locked"}), 403
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "not found"}), 404
    for p in gallery["photos"]:
        if p["id"] == photo_id:
            p["favorite"] = not p.get("favorite", False)
            save_db(db)
            return jsonify({"favorite": p["favorite"]})
    return jsonify({"error": "photo not found"}), 404


@app.route("/gallery/<slug>/photo/<photo_id>/raw")
def photo_proxy(slug, photo_id):
    """Streams the actual image bytes from Telegram, keeping the bot token secret."""
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    is_public_cover = gallery.get("public_visible", True) and (str(gallery.get("cover_photo_id")) == str(photo_id) or str(gallery.get("public_cover_photo_id")) == str(photo_id) or (not gallery.get("cover_photo_id") and not gallery.get("public_cover_photo_id") and bool(gallery.get("photos")) and str(gallery["photos"][0].get("id")) == str(photo_id)))
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin") and not is_public_cover:
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404

    file_id = photo["file_id"]
    file_path = FILE_PATH_CACHE.get(file_id)
    if not file_path:
        file_path = tg_get_file_path(file_id)
        FILE_PATH_CACHE[file_id] = file_path

    tg_url = f"{TG_FILE_API}/{file_path}"
    r = requests.get(tg_url, stream=True, timeout=60)

    return Response(
        stream_with_context(r.iter_content(chunk_size=8192)),
        content_type=r.headers.get("Content-Type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=604800"},
    )


@app.route("/gallery/<slug>/photo/<photo_id>/preview")
def photo_preview(slug, photo_id):
    """Serve the persistent 2000px preview, from local disk cache when possible."""
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404
    try:
        # Avoid a Telegram round-trip on every single view: if this render
        # already saved a local copy (and the host's disk survived since),
        # serve that directly instead of re-fetching from Telegram.
        disk_path = preview_disk_path(slug, photo_id)
        if disk_path.exists():
            return Response(disk_path.read_bytes(), content_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800, immutable"})
        _, preview_id = ensure_photo_assets(slug, photo)
        data = tg_download_bytes(preview_id)
        save_disk_preview(slug, photo_id, data)
        return Response(data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800, immutable"})
    except Exception as e:
        print(f"Preview error for {slug}/{photo_id}: {e}")
        return "Preview unavailable", 503


@app.route("/gallery/<slug>/photo/<photo_id>/thumb")
def photo_thumb(slug, photo_id):
    """Serve the persistent 1280px thumbnail, from local disk cache when possible."""
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    is_public_cover = gallery.get("public_visible", True) and (str(gallery.get("cover_photo_id")) == str(photo_id) or str(gallery.get("public_cover_photo_id")) == str(photo_id) or (not gallery.get("cover_photo_id") and not gallery.get("public_cover_photo_id") and bool(gallery.get("photos")) and str(gallery["photos"][0].get("id")) == str(photo_id)))
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin") and not is_public_cover:
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404
    try:
        disk_path = thumb_disk_path(slug, photo_id)
        if disk_path.exists():
            return Response(disk_path.read_bytes(), content_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800, immutable"})
        thumb_id, _ = ensure_photo_assets(slug, photo)
        data = tg_download_bytes(thumb_id)
        save_disk_thumb(slug, photo_id, data)
        return Response(data, content_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800, immutable"})
    except Exception as e:
        print(f"Thumbnail error for {slug}/{photo_id}: {e}")
        return "Thumbnail unavailable", 503


@app.route("/gallery/<slug>/video/<video_id>/raw")
def video_proxy(slug, video_id):
    """Streams a Telegram-stored video to the client without exposing the bot token."""
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403

    video = next((v for v in gallery.get("videos", []) if v["id"] == video_id), None)
    if not video:
        return "Not found", 404

    file_path = tg_get_file_path(video["file_id"])
    tg_url = f"{TG_FILE_API}/{file_path}"

    headers = {}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]

    r = requests.get(tg_url, headers=headers, stream=True, timeout=120)
    response_headers = {}
    for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if key in r.headers:
            response_headers[key] = r.headers[key]

    response_headers["Accept-Ranges"] = "bytes"

    return Response(
        stream_with_context(r.iter_content(chunk_size=1024 * 256)),
        status=r.status_code,
        headers=response_headers,
        content_type=r.headers.get("Content-Type", video.get("mimetype", "video/mp4")),
    )


@app.route("/gallery/<slug>/video/<video_id>/download")
def video_download(slug, video_id):
    resp = video_proxy(slug, video_id)
    if isinstance(resp, Response):
        video = next(
            (v for v in load_db_ro()["galleries"][slug].get("videos", []) if v["id"] == video_id),
            None,
        )
        filename = (video or {}).get("filename") or f"{video_id}.mp4"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/gallery/<slug>/photo/<photo_id>/download")
def photo_download(slug, photo_id):
    # When Cloudflare media delivery is configured, gallery JS uses the signed
    # Worker URL directly. Keep this route as a safe fallback for old clients
    # and for installations without the Worker configured.
    db = load_db_ro()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404
    return photo_proxy(slug, photo_id)


if __name__ == "__main__":
    if not BOT_TOKEN or not CHANNEL_ID:
        print("WARNING: BOT_TOKEN / CHANNEL_ID not set. Copy .env.example to .env and fill it in.")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
   
