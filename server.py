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
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path

import requests
from PIL import Image, ImageOps
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
THUMB_DIR = DATA_DIR / "thumbs"
DATA_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

# Prevent many first-time thumbnail requests from downloading/decode the
# same large originals at once. Only one large source image is held at a time.
THUMB_SEMAPHORE = threading.Semaphore(1)
DB_LOAD_LOCK = threading.Lock()
DB_LOADED_ONCE = False

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
THUMB_CACHE_MAX = 40
THUMB_CACHE_LOCK = threading.Lock()

def thumb_cache_get(key):
    with THUMB_CACHE_LOCK:
        value = THUMB_CACHE.get(key)
        if value is not None:
            THUMB_CACHE.move_to_end(key)
            return value
    # Persistent local cache survives normal restarts while the Render
    # instance filesystem remains available; Telegram remains the source
    # of truth and thumbnails can always be regenerated.
    path = THUMB_DIR / f"{secure_cache_name(key)}.jpg"
    try:
        if path.exists():
            value = path.read_bytes()
            thumb_cache_set(key, value)
            return value
    except Exception as e:
        print(f"WARNING: thumbnail cache read failed: {e}")
    return None

def thumb_cache_set(key, value):
    with THUMB_CACHE_LOCK:
        THUMB_CACHE[key] = value
        THUMB_CACHE.move_to_end(key)
        while len(THUMB_CACHE) > THUMB_CACHE_MAX:
            THUMB_CACHE.popitem(last=False)
    try:
        path = THUMB_DIR / f"{secure_cache_name(key)}.jpg"
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(value)
        tmp.replace(path)
    except Exception as e:
        print(f"WARNING: thumbnail cache write failed: {e}")

def secure_cache_name(key):
    import hashlib
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


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

    return {"galleries": {}}


def save_db(db):
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=2)
    if BOT_TOKEN and CHANNEL_ID:
        try:
            tg_backup_db(db)
        except Exception as e:
            # Never let a backup failure break the actual user action.
            print(f"WARNING: Telegram backup failed: {e}")


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




def tg_send_thumbnail_bytes(image_bytes, filename):
    """Store a compact gallery thumbnail in Telegram as a separate document."""
    thumb_name = f"{Path(filename or 'photo').stem}_thumb.jpg"
    files = {"document": (thumb_name, io.BytesIO(image_bytes), "image/jpeg")}
    data = {"chat_id": CHANNEL_ID, "caption": "gallery-thumbnail"}
    r = requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=120)
    r.raise_for_status()
    result = r.json()
    if not result.get("ok"):
        raise RuntimeError(result)
    doc = result["result"]["document"]
    return doc["file_id"], result["result"]["message_id"]

def create_thumbnail_bytes(raw_bytes):
    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)

    if img.mode in ("RGBA", "LA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    img.thumbnail((900, 900), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
    return out.getvalue()

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

        # Original is ALWAYS stored first. This is the source of truth.
        try:
            file_id, message_id = tg_send_photo(f, caption=gallery["name"])
        except Exception as e:
            return jsonify({"error": str(e), "added": added}), 500

        photo = {
            "id": uuid.uuid4().hex[:12],
            "file_id": file_id,
            "thumb_file_id": None,
            "message_id": message_id,
            "favorite": False,
            "filename": secure_filename(f.filename) or "photo",
            "category": category,
        }

        # Thumbnail is secondary. If it fails, the original remains fully usable.
        try:
            f.stream.seek(0)
            raw = f.stream.read()
            thumb_bytes = create_thumbnail_bytes(raw)
            thumb_id, _ = tg_send_thumbnail_bytes(thumb_bytes, f.filename)
            photo["thumb_file_id"] = thumb_id
        except Exception as e:
            print(f"WARNING: thumbnail creation/upload failed for {f.filename}: {e}")

        gallery["photos"].append(photo)
        added.append(photo["id"])
        photos_meta.append({
            "id": photo["id"],
            "thumb_url": url_for("photo_thumb", slug=slug, photo_id=photo["id"]),
            "full_url": url_for("photo_proxy", slug=slug, photo_id=photo["id"]),
            "category": category,
        })

        # Persist after every photo so a restart cannot lose completed uploads.
        save_db(db)

    return jsonify({
        "added": added,
        "total": len(gallery["photos"]),
        "photos_meta": photos_meta
    })



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
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return "Not found", 404
    if not session.get(f"unlocked_{slug}") and not session.get("is_admin"):
        return "Locked", 403

    photo = next((p for p in gallery["photos"] if p["id"] == photo_id), None)
    if not photo:
        return "Not found", 404

    file_id = photo["file_id"]
    try:
        file_path = FILE_PATH_CACHE.get(file_id)
        if not file_path:
            file_path = tg_get_file_path(file_id)
            FILE_PATH_CACHE[file_id] = file_path

        tg_url = f"{TG_FILE_API}/{file_path}"
        r = requests.get(tg_url, stream=True, timeout=120)
        r.raise_for_status()

        return Response(
            stream_with_context(r.iter_content(chunk_size=65536)),
            content_type=r.headers.get("Content-Type", "image/jpeg"),
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{file_id}"',
            },
        )
    except Exception as e:
        print(f"ERROR original photo {photo_id}: {e}")
        return "Photo unavailable", 502



@app.route("/admin/gallery/<slug>/generate-thumbnails", methods=["POST"])
@admin_required
def generate_missing_thumbnails(slug):
    """Generate one missing thumbnail per request.

    Processing a single photo keeps Render's free instance from running out
    of memory or timing out while a large wedding gallery is being migrated.
    The browser calls this endpoint repeatedly until all missing thumbnails
    are finished.
    """
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"ok": False, "error": "Gallery not found"}), 404

    photos = [p for p in gallery.get("photos", []) if not p.get("thumb_file_id")]
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0

    total = len(photos)
    if offset >= total:
        return jsonify({
            "ok": True,
            "created": 0,
            "failed": 0,
            "done": True,
            "total": total,
            "next_offset": offset,
        })

    photo = photos[offset]
    try:
        file_id = photo["file_id"]
        file_path = tg_get_file_path(file_id)
        r = requests.get(f"{TG_FILE_API}/{file_path}", timeout=120)
        r.raise_for_status()

        thumb_bytes = create_thumbnail_bytes(r.content)
        thumb_id, _ = tg_send_thumbnail_bytes(
            thumb_bytes, photo.get("filename") or "photo.jpg"
        )

        target = next(
            (p for p in gallery.get("photos", []) if p.get("id") == photo.get("id")),
            None,
        )
        if target is not None:
            target["thumb_file_id"] = thumb_id
            save_db(db)

        return jsonify({
            "ok": True,
            "created": 1,
            "failed": 0,
            "done": offset + 1 >= total,
            "total": total,
            "next_offset": offset + 1,
        })
    except Exception as e:
        print(f"WARNING thumbnail generation failed for {photo.get('id')}: {e}")
        return jsonify({
            "ok": True,
            "created": 0,
            "failed": 1,
            "done": offset + 1 >= total,
            "total": total,
            "next_offset": offset + 1,
            "error": str(e),
        })


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
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
   
