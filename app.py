import os
import json
import threading
import time
import requests as http_requests
from flask import Flask, request, jsonify, render_template, session, redirect
from dotenv import load_dotenv

# Load .env file (ignored if not present)
load_dotenv()

# Allow OAuth over HTTP for local development only
if os.environ.get("FLASK_ENV") != "production":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

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

# ── YouTube OAuth2 ────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_SECRETS_FILE = os.path.join(os.path.dirname(__file__), "client_secrets.json")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"

def get_youtube_client_config():
    """
    Returns client config dict. Prefers YOUTUBE_CLIENT_SECRETS_JSON env var
    (for production), falls back to client_secrets.json file (for local dev).
    """
    env_json = os.environ.get("YOUTUBE_CLIENT_SECRETS_JSON")
    if env_json:
        return json.loads(env_json)
    if os.path.exists(YOUTUBE_CLIENT_SECRETS_FILE):
        with open(YOUTUBE_CLIENT_SECRETS_FILE) as f:
            return json.load(f)
    return None

# ── Instagram Graph API ───────────────────────────────────────────────────────
# Uses the dedicated Instagram app (business login)
IG_APP_ID       = os.environ.get("IG_APP_ID", "1967636900575659")
IG_APP_SECRET   = os.environ.get("IG_APP_SECRET", "")
IG_GRAPH_URL    = "https://graph.facebook.com/v19.0"
IG_REDIRECT_URI = os.environ.get(
    "IG_REDIRECT_URI",
    "http://localhost:5000/instagram/callback"
)
# Scopes for Instagram Business Login (new Instagram API, no Facebook Login needed)
IG_SCOPES       = "instagram_business_basic,instagram_business_content_publish"

# In-memory upload progress store  { task_id: { platform: { status, progress, message } } }
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


# ═════════════════════════════════════════════════════════════════════════════
#  YOUTUBE UPLOAD
# ═════════════════════════════════════════════════════════════════════════════
def upload_to_youtube(task_id: str, filepath: str, title: str, description: str,
                      tags: list, privacy: str, credentials_dict: dict):
    try:
        update_status(task_id, "youtube", "uploading", 0, "Starting YouTube upload…")

        credentials = google.oauth2.credentials.Credentials(**credentials_dict)
        youtube = googleapiclient.discovery.build(
            YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=credentials
        )

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

        response = None
        while response is None:
            status_obj, response = insert_request.next_chunk()
            if status_obj:
                pct = int(status_obj.progress() * 100)
                update_status(task_id, "youtube", "uploading", pct, f"Uploading… {pct}%")

        video_id = response.get("id", "")
        update_status(
            task_id, "youtube", "success", 100,
            f"Uploaded! https://www.youtube.com/watch?v={video_id}"
        )

    except Exception as exc:
        update_status(task_id, "youtube", "error", 0, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
#  INSTAGRAM GRAPH API UPLOAD
# ═════════════════════════════════════════════════════════════════════════════
def get_ig_user_id(access_token: str) -> str:
    """
    Retrieve the Instagram Business Account ID linked to the token.
    Flow: token → Facebook pages → linked IG business account id.
    """
    # Get Facebook pages the user manages
    pages_resp = http_requests.get(
        f"{IG_GRAPH_URL}/me/accounts",
        params={"access_token": access_token}
    )
    pages_resp.raise_for_status()
    pages = pages_resp.json().get("data", [])

    if not pages:
        raise RuntimeError(
            "No Facebook Pages found. Your Instagram Business account must be linked to a Facebook Page."
        )

    # Use first page's long-lived page token to find Instagram account
    for page in pages:
        page_token = page.get("access_token")
        page_id    = page.get("id")

        ig_resp = http_requests.get(
            f"{IG_GRAPH_URL}/{page_id}",
            params={
                "fields": "instagram_business_account",
                "access_token": page_token,
            }
        )
        ig_resp.raise_for_status()
        ig_data = ig_resp.json()

        ig_account = ig_data.get("instagram_business_account")
        if ig_account:
            return ig_account["id"], page_token

    raise RuntimeError(
        "No Instagram Business Account found linked to your Facebook Pages. "
        "Make sure your Instagram account is set to Business/Creator and is connected to a Facebook Page."
    )


def upload_to_instagram(task_id: str, filepath: str, caption: str,
                        access_token: str, ig_user_id: str):
    """
    Upload a video to Instagram as a Reel using the Graph API.
    Steps:
      1. Create a media container (upload the video file URL or resume upload)
      2. Poll until container status = FINISHED
      3. Publish the container
    """
    try:
        update_status(task_id, "instagram", "uploading", 5, "Preparing Instagram upload…")

        # ── Step 1: Upload video bytes to the resumable upload endpoint ────────
        # First create the container specifying media_type=REELS
        update_status(task_id, "instagram", "uploading", 10, "Creating media container…")

        # We use the video upload URL approach — upload file to Facebook CDN first
        # via the resumable upload API, then publish
        file_size = os.path.getsize(filepath)

        # Start resumable upload session
        start_resp = http_requests.post(
            f"https://rupload.facebook.com/video-upload/v19.0/{ig_user_id}/video",
            headers={
                "Authorization": f"OAuth {access_token}",
                "X-FB-Video-File-Size": str(file_size),
                "Content-Type": "application/octet-stream",
            },
            params={"upload_phase": "start"},
        )
        start_resp.raise_for_status()
        upload_session_id = start_resp.json().get("upload_session_id") or start_resp.json().get("video_id")

        if not upload_session_id:
            # Fallback: use container creation with file_url not available locally
            # so we use the direct container + publish via /reels/publish endpoint
            raise RuntimeError("Could not start resumable upload session: " + start_resp.text)

        update_status(task_id, "instagram", "uploading", 20, "Uploading video bytes…")

        # Upload the actual bytes
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
        upload_resp.raise_for_status()
        fb_video_id = upload_resp.json().get("video_id") or upload_session_id

        update_status(task_id, "instagram", "uploading", 50, "Video uploaded, creating container…")

        # ── Step 2: Create Instagram media container from uploaded video ────────
        container_resp = http_requests.post(
            f"{IG_GRAPH_URL}/{ig_user_id}/media",
            params={
                "media_type":  "REELS",
                "video_id":    fb_video_id,
                "caption":     caption,
                "share_to_feed": "true",
                "access_token": access_token,
            }
        )
        container_resp.raise_for_status()
        container_id = container_resp.json().get("id")

        if not container_id:
            raise RuntimeError("Failed to create media container: " + container_resp.text)

        update_status(task_id, "instagram", "uploading", 65, "Processing video… please wait")

        # ── Step 3: Poll container status until FINISHED ──────────────────────
        for attempt in range(30):
            time.sleep(5)
            status_resp = http_requests.get(
                f"{IG_GRAPH_URL}/{container_id}",
                params={
                    "fields": "status_code,status",
                    "access_token": access_token,
                }
            )
            status_resp.raise_for_status()
            status_data   = status_resp.json()
            status_code   = status_data.get("status_code", "")
            status_detail = status_data.get("status", "")

            pct = min(65 + attempt * 1, 88)
            update_status(task_id, "instagram", "uploading", pct,
                          f"Processing… ({status_code})")

            if status_code == "FINISHED":
                break
            elif status_code == "ERROR":
                raise RuntimeError(f"Instagram processing failed: {status_detail}")
            # IN_PROGRESS or PUBLISHED — keep waiting

        else:
            raise RuntimeError("Timed out waiting for Instagram to process the video.")

        update_status(task_id, "instagram", "uploading", 90, "Publishing to Instagram…")

        # ── Step 4: Publish ───────────────────────────────────────────────────
        publish_resp = http_requests.post(
            f"{IG_GRAPH_URL}/{ig_user_id}/media_publish",
            params={
                "creation_id":  container_id,
                "access_token": access_token,
            }
        )
        publish_resp.raise_for_status()
        media_id = publish_resp.json().get("id", "")

        update_status(
            task_id, "instagram", "success", 100,
            f"Published! Media ID: {media_id}"
        )

    except Exception as exc:
        update_status(task_id, "instagram", "error", 0, str(exc))


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — General
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — YouTube OAuth2
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/youtube/auth")
def youtube_auth():
    config = get_youtube_client_config()
    if not config:
        return jsonify({"error": "YouTube credentials not configured. Set YOUTUBE_CLIENT_SECRETS_JSON environment variable."}), 400

    redirect_uri = request.url_root.rstrip("/") + "/youtube/callback"

    # Inject the current redirect_uri into config so it's always valid
    config["web"]["redirect_uris"] = [redirect_uri]

    flow = google_auth_oauthlib.flow.Flow.from_client_config(
        config, scopes=YOUTUBE_SCOPES
    )
    flow.redirect_uri = redirect_uri
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    session["youtube_state"] = state
    return jsonify({"auth_url": auth_url})


@app.route("/youtube/callback")
def youtube_callback():
    config = get_youtube_client_config()
    if not config:
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
    return render_template("auth_success.html", platform="YouTube")


@app.route("/youtube/status")
def youtube_status():
    return jsonify({"authenticated": "youtube_credentials" in session})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Instagram OAuth2 (Facebook Graph API)
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/instagram/auth")
def instagram_auth():
    """Redirect user to Instagram Business Login OAuth."""
    from urllib.parse import quote
    ig_oauth_url = (
        "https://www.instagram.com/oauth/authorize"
        f"?client_id={IG_APP_ID}"
        f"&redirect_uri={quote(IG_REDIRECT_URI, safe='')}"
        f"&scope={IG_SCOPES}"
        "&response_type=code"
    )
    return jsonify({"auth_url": ig_oauth_url})


@app.route("/instagram/callback")
def instagram_callback():
    """Handle Instagram OAuth callback, exchange code for access token."""
    error = request.args.get("error")
    if error:
        return f"<h3>Instagram auth failed: {request.args.get('error_description', error)}</h3>", 400

    code = request.args.get("code")
    if not code:
        return "<h3>No code returned from Instagram.</h3>", 400

    # Exchange code for short-lived token
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
    token_data = token_resp.json()
    if "error_type" in token_data or "error" in token_data:
        msg = token_data.get("error_message") or token_data.get("error", {}).get("message", "Unknown error")
        return f"<h3>Token exchange failed: {msg}</h3>", 400

    short_token = token_data.get("access_token")
    ig_user_id  = str(token_data.get("user_id", ""))

    # Exchange for long-lived token (60 days)
    long_resp = http_requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type":        "ig_exchange_token",
            "client_secret":     IG_APP_SECRET,
            "access_token":      short_token,
        }
    )
    long_data  = long_resp.json()
    long_token = long_data.get("access_token", short_token)

    # Store in session
    session["instagram_access_token"] = long_token
    session["instagram_user_id"]      = ig_user_id

    return render_template("auth_success.html", platform="Instagram")


@app.route("/instagram/status")
def instagram_status():
    return jsonify({"authenticated": "instagram_user_id" in session})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Upload
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400

    file = request.files["video"]
    if file.filename == "" or not allowed_file(file.filename):
        return jsonify({"error": "Invalid or unsupported file type"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    title       = request.form.get("title", "My Video")
    description = request.form.get("description", "")
    tags        = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
    privacy     = request.form.get("privacy", "public")
    caption     = request.form.get("caption", title)
    platforms   = request.form.getlist("platforms")

    task_id = str(int(time.time() * 1000))
    threads = []

    if "youtube" in platforms:
        if "youtube_credentials" not in session:
            return jsonify({"error": "YouTube not authenticated. Connect your YouTube account first."}), 401
        t = threading.Thread(
            target=upload_to_youtube,
            args=(task_id, filepath, title, description, tags, privacy,
                  session["youtube_credentials"]),
            daemon=True,
        )
        threads.append(t)

    if "instagram" in platforms:
        if "instagram_user_id" not in session:
            return jsonify({"error": "Instagram not authenticated. Connect your Instagram account first."}), 401
        t = threading.Thread(
            target=upload_to_instagram,
            args=(task_id, filepath, caption,
                  session["instagram_access_token"],
                  session["instagram_user_id"]),
            daemon=True,
        )
        threads.append(t)
    if not threads:
        return jsonify({"error": "Select at least one platform."}), 400

    for t in threads:
        t.start()

    return jsonify({"task_id": task_id, "platforms": platforms})


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES — Progress polling
# ═════════════════════════════════════════════════════════════════════════════
@app.route("/status/<task_id>")
def get_status(task_id: str):
    return jsonify(upload_status.get(task_id, {}))


if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    app.run(debug=True, port=5000)
