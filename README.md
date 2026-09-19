# 🎬 VideoUploader — YouTube & Instagram in One Click

A web-based tool that lets you upload a video to **YouTube** and **Instagram** simultaneously from a single, clean UI.

---

## ✨ Features

- 📁 Drag & drop (or browse) video file selection
- ▶️ YouTube upload via Google OAuth2 (no password stored)
- 📷 Instagram upload via username/password (session only)
- 🚀 Parallel uploads — both platforms at the same time
- 📊 Real-time progress bars for each platform
- 🌙 Dark-themed, responsive UI

---

## 📁 Project Structure

```
video-uploader/
├── app.py                      # Flask backend
├── requirements.txt            # Python dependencies
├── client_secrets.json.example # YouTube API credentials template
├── .gitignore
├── uploads/                    # Temporary video storage
├── templates/
│   ├── index.html              # Main UI
│   └── auth_success.html       # OAuth callback page
└── static/
    ├── style.css               # Dark theme styles
    ├── app.js                  # Frontend logic
    ├── youtube-icon.svg
    └── instagram-icon.svg
```

---

## 🚀 Setup & Run

### 1. Clone / open the project

```bash
cd video-uploader
```

### 2. Create a virtual environment

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Set up YouTube API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project → enable **YouTube Data API v3**
3. Create **OAuth 2.0 credentials** (type: **Web application**)
4. Add `http://localhost:5000/youtube/callback` as an **Authorized redirect URI**
5. Download the JSON and save it as `client_secrets.json` in the project root
   (use `client_secrets.json.example` as a template)

### 5. Allow OAuth in development (HTTP)

```bash
# Windows PowerShell
$env:OAUTHLIB_INSECURE_TRANSPORT = "1"

# macOS / Linux
export OAUTHLIB_INSECURE_TRANSPORT=1
```

### 6. Run the app

```bash
python app.py
```

Open your browser at **http://localhost:5000**

---

## 🎯 How to Use

| Step | Action |
|------|--------|
| 1 | Click **Connect YouTube** → sign in with Google in the popup |
| 2 | Enter your **Instagram username & password** → click Save |
| 3 | Drag & drop your video (MP4, MOV, AVI, MKV, WEBM — max 500 MB) |
| 4 | Fill in Title, Description, Tags, Privacy, and Instagram Caption |
| 5 | Check the platforms you want to upload to |
| 6 | Click **Upload Now** and watch the progress bars! |

---

## ⚠️ Instagram Notes

- Instagram does **not** have an official public API for video uploads.
  This tool uses [instagrapi](https://github.com/subzeroid/instagrapi), an unofficial library.
- Use an **app/test account** for development — frequent API calls may trigger rate limits.
- Your Instagram password is **only kept in the Flask session** and is never written to disk.

---

## 🔒 Security Notes

- Never commit `client_secrets.json` to version control (it's in `.gitignore`)
- Use HTTPS and a proper secret key in production
- Remove `OAUTHLIB_INSECURE_TRANSPORT=1` in production

---

## 📦 Dependencies

| Package | Purpose |
|---------|---------|
| Flask | Web framework |
| google-auth + google-api-python-client | YouTube Data API v3 |
| google-auth-oauthlib | YouTube OAuth2 flow |
| instagrapi | Instagram upload (unofficial) |
| werkzeug | Secure file handling |
