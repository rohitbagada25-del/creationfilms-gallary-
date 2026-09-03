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
import json
import uuid
import time
import hashlib
import threading
from functools import wraps
from pathlib import Path

from PIL import Image

import requests
from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_from_directory, Response, stream_with_context
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
ADMIN_KEY = os.environ.get("ADMIN_KEY", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TG_FILE_API = f"https://api.telegram.org/file/bot{BOT_TOKEN}"

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "galleries.json"
DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------
# SPEED LAYER
# Everything below exists to stop the site from talking to Telegram on
# every single image request. Three caches:
#   1. HTTP session      -> reuses TLS connections to Telegram
#   2. _path_cache       -> getFile results (valid ~1h), so each image
#                           needs ONE request instead of two
#   3. CACHE_DIR on disk -> generated thumbnails, served straight from
#                           local disk with long-lived browser caching
# ---------------------------------------------------------------------

HTTP = requests.Session()
HTTP.mount(
    "https://",
    requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=2),
)

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/tmp/gallery-cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

THUMB_MAX = 1000          # longest edge of grid/preview images, in px
THUMB_QUALITY = 72
PATH_TTL = 45 * 60        # Telegram file paths stay valid ~1h

_db_lock = threading.Lock()
_db_cache = {"data": None, "mtime": None}
_path_cache = {}
_backup_timer = None
_backup_lock = threading.Lock()

app = Flask(__name__)
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

def _db_mtime():
    return DATA_FILE.stat().st_mtime if DATA_FILE.exists() else None


def load_db():
    """Cached in memory. Before this, every photo request re-read and
    re-parsed galleries.json from disk."""
    mtime = _db_mtime()
    with _db_lock:
        if _db_cache["data"] is not None and _db_cache["mtime"] == mtime:
            return _db_cache["data"]
    db = _load_db_from_disk()
    with _db_lock:
        _db_cache["data"] = db
        _db_cache["mtime"] = _db_mtime()
    return db


def _load_db_from_disk():
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

    return {"galleries": {}}


def save_db(db):
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=2)
    with _db_lock:
        _db_cache["data"] = db
        _db_cache["mtime"] = _db_mtime()
    if BOT_TOKEN and CHANNEL_ID:
        _schedule_backup(db)


def _schedule_backup(db):
    """Backup to Telegram in the background, debounced by 5s.

    Previously this ran inline, so a simple 'favorite' click waited for a
    full JSON upload to Telegram before the browser got a response.
    """
    global _backup_timer
    snapshot = json.loads(json.dumps(db))

    def run():
        try:
            tg_backup_db(snapshot)
        except Exception as e:
            print(f"WARNING: Telegram backup failed: {e}")

    with _backup_lock:
        if _backup_timer is not None:
            _backup_timer.cancel()
        _backup_timer = threading.Timer(5.0, run)
        _backup_timer.daemon = True
        _backup_timer.start()


def slugify(name):
    return "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")


# ---------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------

def tg_backup_db(db):
    """Upload the current galleries.json to the channel and pin it."""
    content = json.dumps(db, indent=2).encode("utf-8")
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
        pass  # pinning needs "pin messages" admin right; backup itself still succeeded


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
    """Cached getFile lookup -- saves one Telegram round-trip per image."""
    hit = _path_cache.get(file_id)
    now = time.time()
    if hit and hit[1] > now:
        return hit[0]
    r = HTTP.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    file_path = r.json()["result"]["file_path"]
    _path_cache[file_id] = (file_path, now + PATH_TTL)
    return file_path


def tg_download_bytes(file_id):
    r = HTTP.get(f"{TG_FILE_API}/{tg_get_file_path(file_id)}", timeout=120)
    r.raise_for_status()
    return r.content


def tg_send_bytes(filename, raw, mimetype="application/octet-stream", caption=""):
    files = {"document": (filename, io.BytesIO(raw), mimetype)}
    r = HTTP.post(
        f"{TG_API}/sendDocument",
        data={"chat_id": CHANNEL_ID, "caption": caption},
        files=files,
        timeout=180,
    )
    r.raise_for_status()
    result = r.json()["result"]
    return result["document"]["file_id"], result["message_id"]


def make_thumb_bytes(raw):
    """Downscale to a web-sized progressive JPEG (typically 60-150 KB
    instead of the 4-12 MB original)."""
    im = Image.open(io.BytesIO(raw))
    try:
        im = im.convert("RGB")
    except Exception:
        pass
    im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True, progressive=True)
    return buf.getvalue()


def thumb_cache_path(photo_id):
    return CACHE_DIR / f"thumb-{photo_id}.jpg"


def get_thumb(photo):
    """Disk-cached thumbnail. Generated lazily the first time an old
    photo is viewed, so existing galleries speed up too."""
    path = thumb_cache_path(photo["id"])
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()

    thumb_file_id = photo.get("thumb_file_id")
    if thumb_file_id:
        data = tg_download_bytes(thumb_file_id)
    else:
        data = make_thumb_bytes(tg_download_bytes(photo["file_id"]))

    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return data


def cached_image_response(data, content_type="image/jpeg", download_name=None):
    """Immutable long-lived caching + ETag, so a revisit costs 0 bytes."""
    etag = hashlib.md5(data).hexdigest()
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag, "Cache-Control": "private, max-age=31536000, immutable"})
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
        "Content-Length": str(len(data)),
    }
    if download_name:
        headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return Response(data, content_type=content_type, headers=headers)


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
    return render_template("admin_home.html", galleries=galleries)


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
            "videos": [],  # each: {id, file_id, message_id, filename, mimetype}
        }
        save_db(db)
    return redirect(url_for("admin_gallery", slug=slug))


@app.route("/admin/gallery/<slug>")
@admin_required
def admin_gallery(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Gallery not found", 404
    gallery.setdefault("videos", [])
    return render_template("admin_gallery.html", slug=slug, gallery=gallery)


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


@app.route("/admin/gallery/<slug>/upload", methods=["POST"])
@admin_required
def admin_upload(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404

    category = request.form.get("category", "").strip()

    uploaded = request.files.getlist("photos")
    added = []
    photos_meta = []
    for f in uploaded:
        if not f or not f.filename:
            continue
        try:
            raw = f.read()
            file_id, message_id = tg_send_bytes(
                f.filename, raw, f.mimetype or "image/jpeg", caption=gallery["name"]
            )
        except Exception as e:
            return jsonify({"error": str(e), "added": added}), 500

        photo_id = uuid.uuid4().hex[:12]

        # Build the web-sized thumbnail once, at upload time.
        thumb_file_id = None
        try:
            thumb = make_thumb_bytes(raw)
            thumb_cache_path(photo_id).write_bytes(thumb)
            thumb_file_id, _ = tg_send_bytes(
                f"thumb-{photo_id}.jpg", thumb, "image/jpeg", caption="thumb"
            )
        except Exception as e:
            print(f"WARNING: thumbnail failed for {photo_id}: {e}")

        photo = {
            "id": photo_id,
            "file_id": file_id,
            "thumb_file_id": thumb_file_id,
            "message_id": message_id,
            "favorite": False,
            "filename": secure_filename(f.filename),
            "category": category,
        }
        gallery["photos"].append(photo)
        added.append(photo["id"])
        photos_meta.append({
            "id": photo["id"],
            "thumb_url": url_for("photo_thumb", slug=slug, photo_id=photo["id"]),
            "full_url": url_for("photo_proxy", slug=slug, photo_id=photo["id"]),
            "category": category,
        })

    save_db(db)
    return jsonify({"added": added, "total": len(gallery["photos"]), "photos_meta": photos_meta})



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


@app.route("/admin/gallery/<slug>/photo/<photo_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(slug, photo_id):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    gallery["photos"] = [p for p in gallery["photos"] if p["id"] != photo_id]
    if gallery.get("cover_photo_id") == photo_id:
        gallery["cover_photo_id"] = None
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


@app.route("/admin/gallery/<slug>/delete", methods=["POST"])
@admin_required
def admin_delete_gallery(slug):
    db = load_db()
    db["galleries"].pop(slug, None)
    save_db(db)
    return redirect(url_for("admin_home"))


# ---------------------------------------------------------------------
# Client-facing gallery
# ---------------------------------------------------------------------

@app.route("/gallery/<slug>", methods=["GET", "POST"])
def client_gallery(slug):
    db = load_db()
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
        return render_template("gallery_login.html", gallery=gallery, error=error)

    return render_template("gallery.html", slug=slug, gallery=gallery)


@app.route("/api/gallery/<slug>/photos")
def api_gallery_photos(slug):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "not found"}), 404
    if not session.get(f"unlocked_{slug}"):
        return jsonify({"error": "locked"}), 403
    photos = [
        {
            "id": p["id"],
            "favorite": p.get("favorite", False),
            "thumb_url": url_for("photo_thumb", photo_id=p["id"], slug=slug),
            "full_url": url_for("photo_proxy", photo_id=p["id"], slug=slug),
            "download_url": url_for("photo_download", photo_id=p["id"], slug=slug),
            "category": p.get("category", ""),
        }
        for p in gallery["photos"]
    ]
    videos = [
        {
            "id": v["id"],
            "filename": v.get("filename", "video.mp4"),
            "video_url": url_for("video_proxy", slug=slug, video_id=v["id"]),
            "download_url": url_for("video_download", slug=slug, video_id=v["id"]),
        }
        for v in gallery.get("videos", [])
    ]
    return jsonify({
        "name": gallery["name"],
        "photos": photos,
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
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404

    file_path = tg_get_file_path(photo["file_id"])
    tg_url = f"{TG_FILE_API}/{file_path}"
    r = HTTP.get(tg_url, stream=True, timeout=120)

    headers = {"Cache-Control": "private, max-age=31536000, immutable"}
    if "Content-Length" in r.headers:
        headers["Content-Length"] = r.headers["Content-Length"]

    return Response(
        stream_with_context(r.iter_content(chunk_size=64 * 1024)),
        content_type=r.headers.get("Content-Type", "image/jpeg"),
        headers=headers,
    )


@app.route("/gallery/<slug>/photo/<photo_id>/thumb")
def photo_thumb(slug, photo_id):
    """Fast path used by the grid, hero and first lightbox paint."""
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403
    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404
    try:
        return cached_image_response(get_thumb(photo))
    except Exception as e:
        print(f"WARNING: thumb failed for {photo_id}: {e}")
        return photo_proxy(slug, photo_id)



@app.route("/gallery/<slug>/video/<video_id>/raw")
def video_proxy(slug, video_id):
    """Streams a Telegram-stored video to the client without exposing the bot token."""
    db = load_db()
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
            (v for v in load_db()["galleries"][slug].get("videos", []) if v["id"] == video_id),
            None,
        )
        filename = (video or {}).get("filename") or f"{video_id}.mp4"
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@app.route("/gallery/<slug>/photo/<photo_id>/download")
def photo_download(slug, photo_id):
    resp = photo_proxy(slug, photo_id)
    if isinstance(resp, Response):
        photo = next(
            (p for p in load_db()["galleries"][slug]["photos"] if p["id"] == photo_id),
            None,
        )
        filename = (photo or {}).get("filename") or f"{photo_id}.jpg"
        resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


if __name__ == "__main__":
    if not BOT_TOKEN or not CHANNEL_ID:
        print("WARNING: BOT_TOKEN / CHANNEL_ID not set. Copy .env.example to .env and fill it in.")
    port = int(os.environ.get("PORT", 5000))
    # threaded=True: image requests no longer queue behind each other.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
   
