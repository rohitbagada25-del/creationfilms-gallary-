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
from functools import wraps
from pathlib import Path

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
    return render_template("admin_gallery.html", slug=slug, gallery=gallery)


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
            file_id, message_id = tg_send_photo(f, caption=gallery["name"])
        except Exception as e:
            return jsonify({"error": str(e), "added": added}), 500
        photo = {
            "id": uuid.uuid4().hex[:12],
            "file_id": file_id,
            "message_id": message_id,
            "favorite": False,
            "filename": secure_filename(f.filename),
            "category": category,
        }
        gallery["photos"].append(photo)
        added.append(photo["id"])
        photos_meta.append({
            "id": photo["id"],
            "thumb_url": url_for("photo_proxy", slug=slug, photo_id=photo["id"]),
            "category": category,
        })

    save_db(db)
    return jsonify({"added": added, "total": len(gallery["photos"]), "photos_meta": photos_meta})


@app.route("/admin/gallery/<slug>/photo/<photo_id>/delete", methods=["POST"])
@admin_required
def admin_delete_photo(slug, photo_id):
    db = load_db()
    gallery = db["galleries"].get(slug)
    if not gallery:
        return jsonify({"error": "Gallery not found"}), 404
    gallery["photos"] = [p for p in gallery["photos"] if p["id"] != photo_id]
    save_db(db)
    return jsonify({"ok": True})


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
            "thumb_url": url_for("photo_proxy", photo_id=p["id"], slug=slug),
            "category": p.get("category", ""),
        }
        for p in gallery["photos"]
    ]
    return jsonify({"name": gallery["name"], "photos": photos})


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
    r = requests.get(tg_url, stream=True, timeout=60)

    return Response(
        stream_with_context(r.iter_content(chunk_size=8192)),
        content_type=r.headers.get("Content-Type", "image/jpeg"),
    )


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
    app.run(debug=True, port=5000)
                                       
