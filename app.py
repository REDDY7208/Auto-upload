import os
import json
import logging
import threading
import time
import requests as http_requests
from flask import Flask, request, jsonify, render_template, session, redirect
from dotenv import load_dotenv

# ══════════════════════════════════════════════════════════════════════════════
#  LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("video-uploader")

# Silence noisy third-party loggers
logging.getLogger("werkzeug").setLevel(logging.INFO)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)

def _sep(label=""):
    """Print a visible separator line in the terminal."""
    if label:
        log.info("─" * 20 + f" {label} " + "─" * 20)
    else:
        log.info("─" * 60)


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ig_token.json")

def save_ig_token(access_token: str, user_id: str):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "user_id": user_id}, f)
    log.info("✅ Instagram token saved to disk  (user_id=%s)", user_id)

def load_ig_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        log.debug("📂 Instagram token loaded from disk  (user_id=%s)", data.get("user_id"))
        return data
    log.debug("📂 No Instagram token file found on disk")
    return None

def clear_ig_token():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
        log.info("🗑  Instagram token file deleted")


# ══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ══════════════════════════════════════════════════════════════════════════════
_sep("APP STARTUP")
load_dotenv()
log.info("✅ .env loaded")

if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    log.warning("⚠️  OAUTHLIB_INSECURE_TRANSPORT=1  (local HTTP dev mode)")

from werkzeug.utils import secure_filename
import google.oauth2.credentials
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

UPLOAD_FOLDER      = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024   # 500 MB

app.config["UPLOAD_FOLDER"]      = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

log.info("📁 Upload folder : %s", UPLOAD_FOLDER)

# ── YouTube ───────────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secrets.json")
YOUTUBE_SCOPES              = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE_NAME    = "youtube"
YOUTUBE_API_VERSION         = "v3"

# ── Instagram ─────────────────────────────────────────────────────────────────
IG_APP_ID       = os.environ.get("IG_APP_ID", "1967636900575659")
IG_APP_SECRET   = os.environ.get("IG_APP_SECRET", "")
IG_GRAPH_URL    = "https://graph.facebook.com/v19.0"
IG_REDIRECT_URI = os.environ.get("IG_REDIRECT_URI", "http://localhost:5000/instagram/callback")
IG_SCOPES       = "instagram_business_basic,instagram_business_content_publish"

log.info("🔑 IG_APP_ID       : %s", IG_APP_ID)
log.info("🔑 IG_REDIRECT_URI : %s", IG_REDIRECT_URI)
log.info("🔑 IG_APP_SECRET   : %s", "SET ✅" if IG_APP_SECRET else "MISSING ❌")
log.info("🔑 YT secrets file : %s", "EXISTS ✅" if os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE) else "MISSING (will use env var)")
log.info("🔑 YT env var      : %s", "SET ✅" if os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON") else "NOT SET")
_sep()

# In-memory upload progress store
upload_status: dict = {}


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_youtube_client_config():
    env_json = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
    if env_json:
        log.debug("  YouTube config  ← YOUTUBE_CLIENT_SECRETS_JSON env var")
        return json.loads(env_json)
    if os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        log.debug("  YouTube config  ← client_secrets.json file")
        with open(YOUTUBE_CLIENT_SECRETS_FILE) as f:
            return json.load(f)
    log.error("  YouTube config  ← NOT FOUND ❌")
    return None


def update_status(task_id: str, platform: str, status: str, progress: int, message: str):
    upload_status.setdefault(task_id, {})[platform] = {
        "status": status, "progress": progress, "message": message,
    }
    icon = "✅" if status == "success" else ("❌" if status == "error" else "🔄")
    log.info("  %s [%s][%s] %3d%%  %s", icon, task_id, platform.upper(), progress, message)


# ══════════════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOAD
# ══════════════════════════════════════════════════════════════════════════════
def upload_to_youtube(task_id: str, filepath: str, title: str, description: str,
                      tags: list, privacy: str, credentials_dict: dict):
    _sep(f"YOUTUBE UPLOAD [{task_id}]")
    log.info("  📹 File    : %s", filepath)
    log.info("  📝 Title   : %s", title)
    log.info("  🔒 Privacy : %s", privacy)
    log.info("  🏷  Tags    : %s", tags)

    try:
        # STEP 1 — Build credentials
        log.info("  ── STEP 1/4 : Building YouTube credentials")
        update_status(task_id, "youtube", "uploading", 0, "Starting YouTube upload…")
        credentials = google.oauth2.credentials.Credentials(**credentials_dict)
        log.info("  ✅ Credentials OK  client_id=%s", credentials_dict.get("client_id"))

        # STEP 2 — Build API client
        log.info("  ── STEP 2/4 : Building YouTube API client")
        youtube = googleapiclient.discovery.build(
            YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=credentials
        )
        log.info("  ✅ YouTube API client ready")

        # STEP 3 — Create upload request
        log.info("  ── STEP 3/4 : Creating resumable upload request")
        body = {
            "snippet": {
                "title": title, "description": description,
                "tags": tags, "categoryId": "22",
            },
            "status": {"privacyStatus": privacy},
        }
        media = googleapiclient.http.MediaFileUpload(
            filepath, chunksize=256 * 1024, resumable=True
        )
        insert_request = youtube.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media
        )
        log.info("  ✅ Resumable upload request created")

        # STEP 4 — Upload chunks
        log.info("  ── STEP 4/4 : Uploading chunks to YouTube…")
        response = None
        chunk_count = 0
        while response is None:
            status_obj, response = insert_request.next_chunk()
            chunk_count += 1
            if status_obj:
                pct = int(status_obj.progress() * 100)
                log.info("  📤 Chunk #%d uploaded — %d%%", chunk_count, pct)
                update_status(task_id, "youtube", "uploading", pct, f"Uploading… {pct}%")

        video_id = response.get("id", "")
        log.info("  ✅ YouTube upload COMPLETE  video_id=%s", video_id)
        log.info("  🔗 https://www.youtube.com/watch?v=%s", video_id)
        update_status(task_id, "youtube", "success", 100,
                      f"Uploaded! https://www.youtube.com/watch?v={video_id}")

    except Exception as exc:
        log.exception("  ❌ YouTube upload FAILED: %s", exc)
        update_status(task_id, "youtube", "error", 0, str(exc))
    finally:
        _sep()


# ══════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM UPLOAD
# ══════════════════════════════════════════════════════════════════════════════
def upload_to_instagram(task_id: str, filepath: str, caption: str,
                        access_token: str, ig_user_id: str):
    _sep(f"INSTAGRAM UPLOAD [{task_id}]")
    file_size = os.path.getsize(filepath)
    log.info("  📹 File        : %s", filepath)
    log.info("  📏 Size        : %d bytes  (%.2f MB)", file_size, file_size / 1024 / 1024)
    log.info("  👤 IG user ID  : %s", ig_user_id)
    log.info("  💬 Caption     : %s", caption[:80])

    try:
        update_status(task_id, "instagram", "uploading", 5, "Preparing Instagram upload…")

        # STEP 1 — Start resumable upload session
        log.info("  ── STEP 1/5 : Starting Facebook resumable upload session")
        t0 = time.time()
        start_resp = http_requests.post(
            f"https://rupload.facebook.com/video-upload/v19.0/{ig_user_id}/video",
            headers={
                "Authorization":       f"OAuth {access_token}",
                "X-FB-Video-File-Size": str(file_size),
                "Content-Type":        "application/octet-stream",
            },
            params={"upload_phase": "start"},
        )
        log.info("  📡 Response  : HTTP %d  (%.2fs)", start_resp.status_code, time.time() - t0)
        log.debug("  📡 Body      : %s", start_resp.text[:500])
        start_resp.raise_for_status()

        resp_json         = start_resp.json()
        upload_session_id = resp_json.get("upload_session_id") or resp_json.get("video_id")
        if not upload_session_id:
            raise RuntimeError("Could not get upload_session_id: " + start_resp.text)

        log.info("  ✅ Upload session ID : %s", upload_session_id)
        update_status(task_id, "instagram", "uploading", 10, "Upload session created…")

        # STEP 2 — Upload video bytes
        log.info("  ── STEP 2/5 : Uploading video bytes to Facebook CDN")
        update_status(task_id, "instagram", "uploading", 20, "Uploading video bytes…")
        t0 = time.time()
        with open(filepath, "rb") as f:
            video_bytes = f.read()
        log.info("  📂 File read into memory  (%.2f MB)", len(video_bytes) / 1024 / 1024)

        upload_resp = http_requests.post(
            f"https://rupload.facebook.com/video-upload/v19.0/{ig_user_id}/video",
            headers={
                "Authorization":         f"OAuth {access_token}",
                "X-FB-Upload-Session-ID": upload_session_id,
                "Content-Type":          "application/octet-stream",
                "X-Entity-Length":       str(file_size),
                "X-Entity-Name":         os.path.basename(filepath),
                "X-Entity-Type":         "video/mp4",
            },
            data=video_bytes,
        )
        log.info("  📡 Response  : HTTP %d  (%.2fs)", upload_resp.status_code, time.time() - t0)
        log.debug("  📡 Body      : %s", upload_resp.text[:500])
        upload_resp.raise_for_status()

        fb_video_id = upload_resp.json().get("video_id") or upload_session_id
        log.info("  ✅ FB video ID : %s", fb_video_id)
        update_status(task_id, "instagram", "uploading", 50, "Video uploaded to CDN…")

        # STEP 3 — Create IG media container
        log.info("  ── STEP 3/5 : Creating Instagram media container (REELS)")
        t0 = time.time()
        container_resp = http_requests.post(
            f"{IG_GRAPH_URL}/{ig_user_id}/media",
            params={
                "media_type":    "REELS",
                "video_id":      fb_video_id,
                "caption":       caption,
                "share_to_feed": "true",
                "access_token":  access_token,
            }
        )
        log.info("  📡 Response  : HTTP %d  (%.2fs)", container_resp.status_code, time.time() - t0)
        log.debug("  📡 Body      : %s", container_resp.text[:500])
        container_resp.raise_for_status()

        container_id = container_resp.json().get("id")
        if not container_id:
            raise RuntimeError("No container ID returned: " + container_resp.text)

        log.info("  ✅ Container ID : %s", container_id)
        update_status(task_id, "instagram", "uploading", 65, "Processing video… please wait")

        # STEP 4 — Poll until container status = FINISHED
        log.info("  ── STEP 4/5 : Polling container status (max 30 × 5s = 150s)")
        for attempt in range(1, 31):
            time.sleep(5)
            poll_resp = http_requests.get(
                f"{IG_GRAPH_URL}/{container_id}",
                params={"fields": "status_code,status", "access_token": access_token}
            )
            poll_resp.raise_for_status()
            poll_data   = poll_resp.json()
            status_code = poll_data.get("status_code", "")
            status_msg  = poll_data.get("status", "")

            pct = min(65 + attempt, 88)
            log.info("  🔄 Poll #%02d  status_code=%-12s  detail=%s", attempt, status_code, status_msg)
            update_status(task_id, "instagram", "uploading", pct, f"Processing… ({status_code})")

            if status_code == "FINISHED":
                log.info("  ✅ Container FINISHED after %d poll(s)", attempt)
                break
            elif status_code == "ERROR":
                raise RuntimeError(f"Instagram processing error: {status_msg}")
        else:
            raise RuntimeError("Timed out: container still not FINISHED after 150 seconds")

        # STEP 5 — Publish
        log.info("  ── STEP 5/5 : Publishing reel")
        update_status(task_id, "instagram", "uploading", 90, "Publishing to Instagram…")
        t0 = time.time()
        publish_resp = http_requests.post(
            f"{IG_GRAPH_URL}/{ig_user_id}/media_publish",
            params={"creation_id": container_id, "access_token": access_token}
        )
        log.info("  📡 Response  : HTTP %d  (%.2fs)", publish_resp.status_code, time.time() - t0)
        log.debug("  📡 Body      : %s", publish_resp.text[:500])
        publish_resp.raise_for_status()

        media_id = publish_resp.json().get("id", "")
        log.info("  ✅ Instagram upload COMPLETE  media_id=%s", media_id)
        update_status(task_id, "instagram", "success", 100, f"Published! Media ID: {media_id}")

    except Exception as exc:
        log.exception("  ❌ Instagram upload FAILED: %s", exc)
        update_status(task_id, "instagram", "error", 0, str(exc))
    finally:
        _sep()


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — General
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    log.debug("GET /  →  index.html")
    return render_template("index.html")


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — YouTube OAuth
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/youtube/auth")
def youtube_auth():
    _sep("YOUTUBE AUTH START")
    try:
        log.info("  STEP 1/2 : Loading YouTube client config")
        config = get_youtube_client_config()
        if not config:
            log.error("  ❌ No YouTube client config found")
            return jsonify({"error": "YouTube credentials not configured."}), 400

        redirect_uri = request.url_root.rstrip("/") + "/youtube/callback"
        log.info("  STEP 2/2 : Building OAuth flow")
        log.info("  🔗 redirect_uri = %s", redirect_uri)
        config["web"]["redirect_uris"] = [redirect_uri]

        flow = google_auth_oauthlib.flow.Flow.from_client_config(config, scopes=YOUTUBE_SCOPES)
        flow.redirect_uri = redirect_uri
        auth_url, state  = flow.authorization_url(access_type="offline", include_granted_scopes="true")
        session["youtube_state"] = state

        log.info("  ✅ Auth URL generated  state=%s", state)
        log.debug("  🔗 auth_url = %s", auth_url)
        _sep()
        return jsonify({"auth_url": auth_url})
    except Exception as exc:
        log.exception("  ❌ YouTube auth error: %s", exc)
        _sep()
        return jsonify({"error": str(exc)}), 500


@app.route("/youtube/callback")
def youtube_callback():
    _sep("YOUTUBE CALLBACK")
    try:
        log.info("  STEP 1/3 : Loading YouTube client config")
        config = get_youtube_client_config()
        if not config:
            return "YouTube credentials not configured.", 400

        redirect_uri = request.url_root.rstrip("/") + "/youtube/callback"
        config["web"]["redirect_uris"] = [redirect_uri]

        log.info("  STEP 2/3 : Fetching token from Google")
        log.info("  🔗 redirect_uri = %s", redirect_uri)
        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            config, scopes=YOUTUBE_SCOPES, state=session.get("youtube_state")
        )
        flow.redirect_uri = redirect_uri
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        log.info("  STEP 3/3 : Storing credentials in session")
        session["youtube_credentials"] = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes),
        }
        log.info("  ✅ YouTube OAuth SUCCESS  client_id=%s", creds.client_id)
        log.info("  🔄 Token : %s…", (creds.token or "")[:20])
        log.info("  🔄 Refresh token present : %s", bool(creds.refresh_token))
        _sep()
        return render_template("auth_success.html", platform="YouTube")
    except Exception as exc:
        log.exception("  ❌ YouTube callback error: %s", exc)
        _sep()
        return f"<h3>YouTube auth failed: {exc}</h3>", 400


@app.route("/youtube/status")
def youtube_status():
    authenticated = "youtube_credentials" in session
    log.debug("GET /youtube/status  →  authenticated=%s", authenticated)
    return jsonify({"authenticated": authenticated})


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — Instagram OAuth
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/instagram/auth")
def instagram_auth():
    _sep("INSTAGRAM AUTH START")
    try:
        from urllib.parse import quote
        ig_oauth_url = (
            "https://www.instagram.com/oauth/authorize"
            f"?client_id={IG_APP_ID}"
            f"&redirect_uri={quote(IG_REDIRECT_URI, safe='')}"
            f"&scope={IG_SCOPES}"
            "&response_type=code"
        )
        log.info("  IG_APP_ID       : %s", IG_APP_ID)
        log.info("  IG_REDIRECT_URI : %s", IG_REDIRECT_URI)
        log.info("  IG_SCOPES       : %s", IG_SCOPES)
        log.info("  ✅ Auth URL built")
        log.debug("  🔗 %s", ig_oauth_url)
        _sep()
        return jsonify({"auth_url": ig_oauth_url})
    except Exception as exc:
        log.exception("  ❌ Instagram auth error: %s", exc)
        _sep()
        return jsonify({"error": str(exc)}), 500


@app.route("/instagram/callback")
def instagram_callback():
    _sep("INSTAGRAM CALLBACK")
    error = request.args.get("error")
    if error:
        log.error("  ❌ Instagram returned error: %s — %s",
                  error, request.args.get("error_description"))
        _sep()
        return f"<h3>Instagram auth failed: {request.args.get('error_description', error)}</h3>", 400

    code = request.args.get("code")
    if not code:
        log.error("  ❌ No code param in callback")
        _sep()
        return "<h3>No code returned from Instagram.</h3>", 400

    log.info("  ✅ Auth code received  (length=%d)", len(code))

    # STEP 1 — Exchange code for short-lived token
    log.info("  STEP 1/3 : Exchanging code for short-lived token")
    t0 = time.time()
    token_resp = http_requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={
            "client_id":     IG_APP_ID,
            "client_secret": IG_APP_SECRET,
            "grant_type":    "authorization_code",
            "redirect_uri":  IG_REDIRECT_URI,
            "code":          code,
        }
    )
    log.info("  📡 Token exchange  : HTTP %d  (%.2fs)", token_resp.status_code, time.time() - t0)
    log.debug("  📡 Body            : %s", token_resp.text[:400])
    token_data = token_resp.json()

    if "error_type" in token_data or "error" in token_data:
        msg = token_data.get("error_message") or token_data.get("error", {}).get("message", "Unknown")
        log.error("  ❌ Token exchange failed: %s", msg)
        _sep()
        return f"<h3>Token exchange failed: {msg}</h3>", 400

    short_token = token_data.get("access_token")
    ig_user_id  = str(token_data.get("user_id", ""))
    log.info("  ✅ Short-lived token  ig_user_id=%s", ig_user_id)

    # STEP 2 — Exchange for long-lived token
    log.info("  STEP 2/3 : Exchanging for long-lived token (60 days)")
    t0 = time.time()
    long_resp = http_requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type":    "ig_exchange_token",
            "client_secret": IG_APP_SECRET,
            "access_token":  short_token,
        }
    )
    log.info("  📡 Long token  : HTTP %d  (%.2fs)", long_resp.status_code, time.time() - t0)
    log.debug("  📡 Body        : %s", long_resp.text[:300])
    long_data  = long_resp.json()
    long_token = long_data.get("access_token", short_token)

    if long_data.get("access_token"):
        expires = long_data.get("expires_in", "unknown")
        log.info("  ✅ Long-lived token obtained  expires_in=%s seconds", expires)
    else:
        log.warning("  ⚠️  Could not get long-lived token, using short-lived token")

    # STEP 3 — Save to session + disk
    log.info("  STEP 3/3 : Saving token to session and disk")
    session["instagram_access_token"] = long_token
    session["instagram_user_id"]      = ig_user_id
    save_ig_token(long_token, ig_user_id)

    log.info("  ✅ Instagram OAuth SUCCESS  ig_user_id=%s", ig_user_id)
    _sep()
    return render_template("auth_success.html", platform="Instagram")


@app.route("/instagram/status")
def instagram_status():
    in_session = "instagram_user_id" in session
    on_disk    = load_ig_token() is not None
    authenticated = in_session or on_disk
    log.debug("GET /instagram/status  →  in_session=%s  on_disk=%s  result=%s",
              in_session, on_disk, authenticated)
    return jsonify({"authenticated": authenticated})


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — Upload
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/upload", methods=["POST"])
def upload():
    _sep("UPLOAD REQUEST")
    try:
        return _upload_inner()
    except Exception as exc:
        log.exception("  ❌ Upload route unhandled error: %s", exc)
        _sep()
        return jsonify({"error": f"Server error: {str(exc)}"}), 500


def _upload_inner():
    # STEP 1 — Validate file
    log.info("  STEP 1/4 : Validating uploaded file")
    if "video" not in request.files:
        log.warning("  ❌ No video file in request")
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if file.filename == "" or not allowed_file(file.filename):
        log.warning("  ❌ Invalid/unsupported file: '%s'", file.filename)
        return jsonify({"error": "Invalid or unsupported file type"}), 400

    log.info("  ✅ File valid  name=%s", file.filename)

    # STEP 2 — Save to disk
    log.info("  STEP 2/4 : Saving file to upload folder")
    filename = secure_filename(file.filename)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    size_mb  = os.path.getsize(filepath) / 1024 / 1024
    log.info("  ✅ Saved  path=%s  size=%.2f MB", filepath, size_mb)

    # STEP 3 — Parse form fields
    log.info("  STEP 3/4 : Parsing form fields")
    title       = request.form.get("title", "My Video")
    description = request.form.get("description", "")
    tags        = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    privacy     = request.form.get("privacy", "public")
    caption     = request.form.get("caption", title)
    platforms   = request.form.getlist("platforms")

    log.info("  📝 title       : %s", title)
    log.info("  🔒 privacy     : %s", privacy)
    log.info("  📣 platforms   : %s", platforms)
    log.info("  🏷  tags        : %s", tags)
    log.info("  💬 caption     : %s", caption[:80])

    task_id = str(int(time.time() * 1000))
    log.info("  🆔 task_id     : %s", task_id)

    # STEP 4 — Spawn upload threads
    log.info("  STEP 4/4 : Spawning upload threads")
    threads = []

    if "youtube" in platforms:
        if "youtube_credentials" not in session:
            log.warning("  ❌ YouTube not authenticated")
            return jsonify({"error": "YouTube not authenticated. Connect your YouTube account first."}), 401
        log.info("  🧵 Spawning YouTube thread")
        threads.append(threading.Thread(
            target=upload_to_youtube,
            args=(task_id, filepath, title, description, tags, privacy,
                  session["youtube_credentials"]),
            daemon=True,
        ))

    if "instagram" in platforms:
        if "instagram_user_id" not in session:
            ig_token_data = load_ig_token()
            if ig_token_data:
                log.info("  📂 Instagram token loaded from disk  user_id=%s", ig_token_data["user_id"])
                session["instagram_access_token"] = ig_token_data["access_token"]
                session["instagram_user_id"]      = ig_token_data["user_id"]
            else:
                log.warning("  ❌ Instagram not authenticated")
                return jsonify({"error": "Instagram not authenticated. Connect your Instagram account first."}), 401
        log.info("  🧵 Spawning Instagram thread  ig_user_id=%s", session["instagram_user_id"])
        threads.append(threading.Thread(
            target=upload_to_instagram,
            args=(task_id, filepath, caption,
                  session["instagram_access_token"],
                  session["instagram_user_id"]),
            daemon=True,
        ))

    if not threads:
        log.warning("  ❌ No platforms selected")
        return jsonify({"error": "Select at least one platform."}), 400

    for t in threads:
        t.start()

    log.info("  ✅ %d thread(s) started  task_id=%s", len(threads), task_id)
    _sep()
    return jsonify({"task_id": task_id, "platforms": platforms})


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES — Status polling
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/status/<task_id>")
def get_status(task_id: str):
    data = upload_status.get(task_id, {})
    log.debug("GET /status/%s  →  %s", task_id, data)
    return jsonify(data)


# ══════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    log.warning("404  path=%s", request.path)
    return jsonify({"error": "Not found", "details": str(e)}), 404

@app.errorhandler(500)
def server_error(e):
    log.error("500  %s", e)
    return jsonify({"error": "Server error", "details": str(e)}), 500

@app.errorhandler(Exception)
def unhandled(e):
    log.exception("Unhandled exception: %s", e)
    return jsonify({"error": "Unexpected error", "details": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    _sep("FLASK SERVER STARTING")
    log.info("  🚀 VideoUpload Pro  →  http://127.0.0.1:5000")
    _sep()
    app.run(debug=True, port=5000)
