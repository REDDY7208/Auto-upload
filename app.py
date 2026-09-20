import os
import json
import logging
import threading
import time
import requests as http_requests
from flask import Flask, request, jsonify, render_template, session, redirect
from dotenv import load_dotenv

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("video-uploader")

# Silence noisy third-party loggers
logging.getLogger("werkzeug").setLevel(logging.INFO)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# ── Token persistence helpers ─────────────────────────────────────────────────
TOKEN_FILE = os.path.join(os.path.dirname(__file__), ".ig_token.json")

def save_ig_token(access_token: str, user_id: str):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"access_token": access_token, "user_id": user_id}, f)
    log.info("Instagram token saved to file (user_id=%s)", user_id)

def load_ig_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        log.debug("Instagram token loaded from file (user_id=%s)", data.get("user_id"))
        return data
    log.debug("No Instagram token file found")
    return None

def clear_ig_token():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
        log.info("Instagram token file cleared")

# ── Load env ──────────────────────────────────────────────────────────────────
load_dotenv()
log.info("Environment loaded")

# Allow OAuth over HTTP for local development only
if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    log.warning("OAUTHLIB_INSECURE_TRANSPORT enabled (local dev mode)")

from werkzeug.utils import secure_filename
import google.oauth2.credentials
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24)

# ── Configuration ─────────────────────────────────────────────────────────────
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
ALLOWED_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm"}
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

log.info("Upload folder: %s", UPLOAD_FOLDER)

# ── YouTube OAuth2 ────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secrets.json")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

def get_youtube_client_config():
    env_json = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
    if env_json:
        log.debug("YouTube config loaded from YOUTUBE_CLIENT_SECRETS_JSON env var")
        return json.loads(env_json)
    if os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        log.debug("YouTube config loaded from client_secrets.json file")
        with open(YOUTUBE_CLIENT_SECRETS_FILE) as f:
            return json.load(f)
    log.error("No YouTube client config found!")
    return None

# ── Instagram Graph API ───────────────────────────────────────────────────────
IG_APP_ID       = os.environ.get("IG_APP_ID", "1967636900575659")
IG_APP_SECRET   = os.environ.get("IG_APP_SECRET", "")
IG_GRAPH_URL    = "https://graph.facebook.com/v19.0"
IG_REDIRECT_URI = os.environ.get(
    "IG_REDIRECT_URI",
    "http://localhost:5000/instagram/callback"
)
IG_SCOPES       = "instagram_business_basic,instagram_business_content_publish"

log.info("Instagram App ID: %s", IG_APP_ID)
log.info("Instagram Redirect URI: %s", IG_REDIRECT_URI)
log.info("Instagram App Secret set: %s", bool(IG_APP_SECRET))

# In-memory upload progress store
upload_status: dict = {}


# ── Helpers ───────────────────────────────────────────────────────────────────
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def update_status(task_id: str, platform: str, status: str, progress: int, message: str):
    upload_status.setdefault(task_id, {})[platform] = {
        "status": status,
        "progress": progress,
        "message": message,
    }
    log.debug("[%s] [%s] status=%s progress=%d%% msg=%s", task_id, platform, status, progress, message)


# ═════════════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOAD
# ═════════════════════════════════════════════════════════════════════════════
def upload_to_youtube(task_id: str, filepath: str, title: str, description: str,
                      tags: list, privacy: str, credentials_dict: dict):
    log.info("[%s] YouTube upload started | file=%s title=%s privacy=%s", task_id, filepath, title, privacy)
    try:
        update_status(task_id, "youtube", "uploading", 0, "Starting YouTube upload…")

        credentials = google.oauth2.credentials.Credentials(**credentials_dict)
        log.debug("[%s] YouTube credentials built, client_id=%s", task_id, credentials_dict.get("client_id"))

        youtube = googleapiclient.discovery.build(
            YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=credentials
        )
        log.debug("[%s] YouTube API client built", task_id)

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": "22",
            },
            "status": {"privacyStatus": privacy},
        }

        media = googleapiclient.http.MediaFileUpload(
            filepath, chunksize=256 * 1024, resumable=True
        )
        insert_request = youtube.videos().insert(
            part=",".join(body.keys()), body=body, media_body=media
        )
        log.info("[%s] YouTube resumable upload request created", task_id)

        response = None
        while response is None:
            status_obj, response = insert_request.next_chunk()
            if status_obj:
                pct = int(status_obj.progress() * 100)
                log.debug("[%s] YouTube upload chunk progress: %d%%", task_id, pct)
                update_status(task_id, "youtube", "uploading", pct, f"Uploading… {pct}%")

        video_id = response.get("id", "")
        log.info("[%s] YouTube upload SUCCESS | video_id=%s", task_id, video_id)
        update_status(
            task_id, "youtube", "success", 100,
            f"Uploaded! https://www.youtube.com/watch?v={video_id}"
        )

    except Exception as exc:
        log.exception("[%s] YouTube upload FAILED: %s", task_id, exc)
        update_status(task_id, "youtube", "error", 0, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM GRAPH API UPLOAD
# ═════════════════════════════════════════════════════════════════════════════
def get_ig_user_id(access_token: str) -> str:
    log.debug("Fetching Facebook pages for IG user ID resolution")
    pages_resp = http_requests.get(
        f"{IG_GRAPH_URL}/me/accounts",
        params={"access_token": access_token}
    )
    log.debug("Pages response status: %d", pages_resp.status_code)
    pages_resp.raise_for_status()
    pages = pages_resp.json().get("data", [])
    log.info("Found %d Facebook page(s)", len(pages))

    if not pages:
        raise RuntimeError(
            "No Facebook Pages found. Your Instagram Business account must be linked to a Facebook Page."
        )

    for page in pages:
        page_token = page.get("access_token")
        page_id    = page.get("id")
        log.debug("Checking page_id=%s for linked Instagram account", page_id)

        ig_resp = http_requests.get(
            f"{IG_GRAPH_URL}/{page_id}",
            params={
                "fields": "instagram_business_account",
                "access_token": page_token,
            }
        )
        ig_resp.raise_for_status()
        ig_data    = ig_resp.json()
        ig_account = ig_data.get("instagram_business_account")

        if ig_account:
            log.info("Found Instagram Business Account: %s", ig_account["id"])
            return ig_account["id"], page_token

    raise RuntimeError(
        "No Instagram Business Account found linked to your Facebook Pages."
    )


def upload_to_instagram(task_id: str, filepath: str, caption: str,
                        access_token: str, ig_user_id: str):
    log.info("[%s] Instagram upload started | file=%s ig_user_id=%s", task_id, filepath, ig_user_id)
    try:
        update_status(task_id, "instagram", "uploading", 5, "Preparing Instagram upload…")

        file_size = os.path.getsize(filepath)
        log.info("[%s] File size: %d bytes (%.2f MB)", task_id, file_size, file_size / 1024 / 1024)

        update_status(task_id, "instagram", "uploading", 10, "Creating media container…")

        # Step 1: Start resumable upload session
        log.debug("[%s] Starting Facebook resumable upload session", task_id)
        start_resp = http_requests.post(
            f"https://rupload.facebook.com/video-upload/v19.0/{ig_user_id}/video",
            headers={
                "Authorization": f"OAuth {access_token}",
                "X-FB-Video-File-Size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            params={"upload_phase": "start"},
        )
        log.debug("[%s] Upload session start response: %d | %s", task_id, start_resp.status_code, start_resp.text[:300])
        start_resp.raise_for_status()

        upload_session_id = start_resp.json().get("upload_session_id") or start_resp.json().get("video_id")
        if not upload_session_id:
            raise RuntimeError("Could not start resumable upload session: " + start_resp.text)

        log.info("[%s] Upload session ID: %s", task_id, upload_session_id)
        update_status(task_id, "instagram", "uploading", 20, "Uploading video bytes…")

        # Step 2: Upload video bytes
        log.debug("[%s] Reading video file and uploading bytes…", task_id)
        with open(filepath, "rb") as f:
            video_bytes = f.read()

        upload_resp = http_requests.post(
            f"https://rupload.facebook.com/video-upload/v19.0/{ig_user_id}/video",
            headers={
                "Authorization": f"OAuth {access_token}",
                "X-FB-Upload-Session-ID": upload_session_id,
                "Content-Type": "application/octet-stream",
                "X-Entity-Length": str(file_size),
                "X-Entity-Name": os.path.basename(filepath),
                "X-Entity-Type": "video/mp4",
            },
            data=video_bytes,
        )
        log.debug("[%s] Upload bytes response: %d | %s", task_id, upload_resp.status_code, upload_resp.text[:300])
        upload_resp.raise_for_status()

        fb_video_id = upload_resp.json().get("video_id") or upload_session_id
        log.info("[%s] FB video ID: %s", task_id, fb_video_id)
        update_status(task_id, "instagram", "uploading", 50, "Video uploaded, creating container…")

        # Step 3: Create IG media container
        log.debug("[%s] Creating Instagram media container", task_id)
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
        log.debug("[%s] Container response: %d | %s", task_id, container_resp.status_code, container_resp.text[:300])
        container_resp.raise_for_status()

        container_id = container_resp.json().get("id")
        if not container_id:
            raise RuntimeError("Failed to create media container: " + container_resp.text)

        log.info("[%s] Container ID: %s", task_id, container_id)
        update_status(task_id, "instagram", "uploading", 65, "Processing video… please wait")

        # Step 4: Poll until FINISHED
        for attempt in range(30):
            time.sleep(5)
            status_resp = http_requests.get(
                f"{IG_GRAPH_URL}/{container_id}",
                params={
                    "fields":       "status_code,status",
                    "access_token": access_token,
                }
            )
            status_resp.raise_for_status()
            status_data   = status_resp.json()
            status_code   = status_data.get("status_code", "")
            status_detail = status_data.get("status", "")

            log.debug("[%s] Container poll attempt %d | status_code=%s detail=%s",
                      task_id, attempt + 1, status_code, status_detail)

            pct = min(65 + attempt, 88)
            update_status(task_id, "instagram", "uploading", pct, f"Processing… ({status_code})")

            if status_code == "FINISHED":
                log.info("[%s] Container processing FINISHED", task_id)
                break
            elif status_code == "ERROR":
                raise RuntimeError(f"Instagram processing failed: {status_detail}")
        else:
            raise RuntimeError("Timed out waiting for Instagram to process the video.")

        # Step 5: Publish
        update_status(task_id, "instagram", "uploading", 90, "Publishing to Instagram…")
        log.debug("[%s] Publishing container %s", task_id, container_id)

        publish_resp = http_requests.post(
            f"{IG_GRAPH_URL}/{ig_user_id}/media_publish",
            params={
                "creation_id":  container_id,
                "access_token": access_token,
            }
        )
        log.debug("[%s] Publish response: %d | %s", task_id, publish_resp.status_code, publish_resp.text[:300])
        publish_resp.raise_for_status()

        media_id = publish_resp.json().get("id", "")
        log.info("[%s] Instagram upload SUCCESS | media_id=%s", task_id, media_id)
        update_status(task_id, "instagram", "success", 100, f"Published! Media ID: {media_id}")

    except Exception as exc:
        log.exception("[%s] Instagram upload FAILED: %s", task_id, exc)
        update_status(task_id, "instagram", "error", 0, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — General
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    log.debug("GET / — serving index page")
    return render_template("index.html")


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — YouTube OAuth2
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/youtube/auth")
def youtube_auth():
    log.info("GET /youtube/auth — starting YouTube OAuth flow")
    try:
        config = get_youtube_client_config()
        if not config:
            log.error("YouTube client config missing")
            return jsonify({"error": "YouTube credentials not configured. Set YOUTUBE_CLIENT_SECRETS_JSON environment variable."}), 400

        redirect_uri = request.url_root.rstrip("/") + "/youtube/callback"
        log.info("YouTube redirect_uri: %s", redirect_uri)
        config["web"]["redirect_uris"] = [redirect_uri]

        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            config, scopes=YOUTUBE_SCOPES
        )
        flow.redirect_uri = redirect_uri
        auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
        session["youtube_state"] = state
        log.info("YouTube auth URL generated, state=%s", state)
        return jsonify({"auth_url": auth_url})
    except Exception as exc:
        log.exception("YouTube auth error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/youtube/callback")
def youtube_callback():
    log.info("GET /youtube/callback — processing YouTube OAuth callback")
    try:
        config = get_youtube_client_config()
        if not config:
            log.error("YouTube client config missing in callback")
            return "YouTube credentials not configured.", 400

        redirect_uri = request.url_root.rstrip("/") + "/youtube/callback"
        config["web"]["redirect_uris"] = [redirect_uri]

        flow = google_auth_oauthlib.flow.Flow.from_client_config(
            config,
            scopes=YOUTUBE_SCOPES,
            state=session.get("youtube_state"),
        )
        flow.redirect_uri = redirect_uri
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        session["youtube_credentials"] = {
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes),
        }
        log.info("YouTube OAuth SUCCESS | client_id=%s", creds.client_id)
        return render_template("auth_success.html", platform="YouTube")
    except Exception as exc:
        log.exception("YouTube callback error: %s", exc)
        return f"<h3>YouTube auth failed: {exc}</h3>", 400


@app.route("/youtube/status")
def youtube_status():
    authenticated = "youtube_credentials" in session
    log.debug("GET /youtube/status — authenticated=%s", authenticated)
    return jsonify({"authenticated": authenticated})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Instagram OAuth2
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/instagram/auth")
def instagram_auth():
    log.info("GET /instagram/auth — building Instagram OAuth URL")
    try:
        from urllib.parse import quote
        ig_oauth_url = (
            "https://www.instagram.com/oauth/authorize"
            f"?client_id={IG_APP_ID}"
            f"&redirect_uri={quote(IG_REDIRECT_URI, safe='')}"
            f"&scope={IG_SCOPES}"
            "&response_type=code"
        )
        log.info("Instagram auth URL: %s", ig_oauth_url)
        return jsonify({"auth_url": ig_oauth_url})
    except Exception as exc:
        log.exception("Instagram auth error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/instagram/callback")
def instagram_callback():
    log.info("GET /instagram/callback — processing Instagram OAuth callback")
    error = request.args.get("error")
    if error:
        log.error("Instagram OAuth error: %s — %s", error, request.args.get("error_description"))
        return f"<h3>Instagram auth failed: {request.args.get('error_description', error)}</h3>", 400

    code = request.args.get("code")
    if not code:
        log.error("No code in Instagram callback")
        return "<h3>No code returned from Instagram.</h3>", 400

    log.debug("Instagram auth code received (length=%d)", len(code))

    # Exchange code for short-lived token
    log.debug("Exchanging code for short-lived token")
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
    log.debug("Token exchange response: %d | %s", token_resp.status_code, token_resp.text[:300])
    token_data = token_resp.json()

    if "error_type" in token_data or "error" in token_data:
        msg = token_data.get("error_message") or token_data.get("error", {}).get("message", "Unknown error")
        log.error("Token exchange failed: %s", msg)
        return f"<h3>Token exchange failed: {msg}</h3>", 400

    short_token = token_data.get("access_token")
    ig_user_id  = str(token_data.get("user_id", ""))
    log.info("Short-lived token obtained | ig_user_id=%s", ig_user_id)

    # Exchange for long-lived token
    log.debug("Exchanging for long-lived token")
    long_resp = http_requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type":    "ig_exchange_token",
            "client_secret": IG_APP_SECRET,
            "access_token":  short_token,
        }
    )
    log.debug("Long-lived token response: %d | %s", long_resp.status_code, long_resp.text[:200])
    long_data  = long_resp.json()
    long_token = long_data.get("access_token", short_token)

    session["instagram_access_token"] = long_token
    session["instagram_user_id"]      = ig_user_id
    save_ig_token(long_token, ig_user_id)

    log.info("Instagram OAuth SUCCESS | ig_user_id=%s", ig_user_id)
    return render_template("auth_success.html", platform="Instagram")


@app.route("/instagram/status")
def instagram_status():
    authenticated = "instagram_user_id" in session or load_ig_token() is not None
    log.debug("GET /instagram/status — authenticated=%s", authenticated)
    return jsonify({"authenticated": authenticated})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Upload
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/upload", methods=["POST"])
def upload():
    log.info("POST /upload — request received")
    try:
        return _upload_inner()
    except Exception as exc:
        log.exception("Upload route unhandled error: %s", exc)
        return jsonify({"error": f"Server error: {str(exc)}"}), 500


def _upload_inner():
    if "video" not in request.files:
        log.warning("Upload rejected — no video file in request")
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if file.filename == "" or not allowed_file(file.filename):
        log.warning("Upload rejected — invalid file: %s", file.filename)
        return jsonify({"error": "Invalid or unsupported file type"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    log.info("File saved: %s (%.2f MB)", filepath, os.path.getsize(filepath) / 1024 / 1024)

    title       = request.form.get("title", "My Video")
    description = request.form.get("description", "")
    tags        = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    privacy     = request.form.get("privacy", "public")
    caption     = request.form.get("caption", title)
    platforms   = request.form.getlist("platforms")

    log.info("Upload params | title=%s privacy=%s platforms=%s tags=%s", title, privacy, platforms, tags)

    task_id = str(int(time.time() * 1000))
    log.info("Task ID: %s", task_id)
    threads = []

    if "youtube" in platforms:
        if "youtube_credentials" not in session:
            log.warning("[%s] YouTube not authenticated", task_id)
            return jsonify({"error": "YouTube not authenticated. Connect your YouTube account first."}), 401
        log.info("[%s] Spawning YouTube upload thread", task_id)
        t = threading.Thread(
            target=upload_to_youtube,
            args=(task_id, filepath, title, description, tags, privacy,
                  session["youtube_credentials"]),
            daemon=True,
        )
        threads.append(t)

    if "instagram" in platforms:
        if "instagram_user_id" not in session:
            ig_token_data = load_ig_token()
            if ig_token_data:
                log.info("[%s] Instagram token loaded from file", task_id)
                session["instagram_access_token"] = ig_token_data["access_token"]
                session["instagram_user_id"]      = ig_token_data["user_id"]
            else:
                log.warning("[%s] Instagram not authenticated", task_id)
                return jsonify({"error": "Instagram not authenticated. Connect your Instagram account first."}), 401
        log.info("[%s] Spawning Instagram upload thread | ig_user_id=%s",
                 task_id, session["instagram_user_id"])
        t = threading.Thread(
            target=upload_to_instagram,
            args=(task_id, filepath, caption,
                  session["instagram_access_token"],
                  session["instagram_user_id"]),
            daemon=True,
        )
        threads.append(t)

    if not threads:
        log.warning("[%s] No platforms selected", task_id)
        return jsonify({"error": "Select at least one platform."}), 400

    for t in threads:
        t.start()
    log.info("[%s] %d upload thread(s) started", task_id, len(threads))

    return jsonify({"task_id": task_id, "platforms": platforms})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Progress polling
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/status/<task_id>")
def get_status(task_id: str):
    data = upload_status.get(task_id, {})
    log.debug("GET /status/%s — %s", task_id, data)
    return jsonify(data)


# ═════════════════════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLERS — always return JSON, never HTML
# ═════════════════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    log.warning("404 Not Found: %s", request.path)
    return jsonify({"error": "Not found", "details": str(e)}), 404

@app.errorhandler(500)
def server_error(e):
    log.error("500 Server Error: %s", e)
    return jsonify({"error": "Server error", "details": str(e)}), 500

@app.errorhandler(Exception)
def unhandled(e):
    log.exception("Unhandled exception: %s", e)
    return jsonify({"error": "Unexpected error", "details": str(e)}), 500


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    log.info("Starting VideoUpload Pro on port 5000")
    app.run(debug=True, port=5000)
