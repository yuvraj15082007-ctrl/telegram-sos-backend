import os
import json
import uuid
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- CONFIG ----
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_FILE = "contacts.json"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change_this_secret")  # simple auth for /sos endpoint

# ---- SIMPLE FILE-BASED DB (hackathon speed; swap for Firestore/Postgres later) ----
_lock = threading.Lock()

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ---- ROUTES ----

@app.route("/")
def home():
    return "SOS backend is running."


@app.route("/generate_code", methods=["POST"])
def generate_code():
    """
    Called by the Android app when the girl taps 'Add Trusted Contact'.
    Returns a unique code + the deep link she shares with her contact.
    Body: { "user_id": "<some_device_or_app_generated_id>" }
    """
    body = request.get_json(force=True)
    user_id = body.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400

    code = uuid.uuid4().hex[:8]

    with _lock:
        data = load_data()
        data.setdefault(user_id, {})
        data[user_id]["pending_code"] = code
        data[user_id].setdefault("contacts", [])
        save_data(data)

    bot_username = body.get("bot_username", "YourBotUsername")
    deep_link = f"https://t.me/{bot_username}?start={code}"

    return jsonify({"code": code, "deep_link": deep_link})


@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    """
    Telegram sends updates here (set via setWebhook). Handles /start <code>.
    """
    update = request.get_json(force=True)

    message = update.get("message")
    if not message:
        return jsonify({"ok": True})

    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")
    first_name = message.get("chat", {}).get("first_name", "Contact")

    if text.startswith("/start"):
        parts = text.split(" ", 1)
        if len(parts) == 2:
            code = parts[1].strip()
            with _lock:
                data = load_data()
                matched_user = None
                for user_id, info in data.items():
                    if info.get("pending_code") == code:
                        matched_user = user_id
                        break

                if matched_user:
                    data[matched_user]["contacts"].append({
                        "chat_id": chat_id,
                        "name": first_name
                    })
                    data[matched_user]["pending_code"] = None
                    save_data(data)
                    send_message(chat_id, f"You're now a trusted safety contact ✅ You'll receive alerts here if help is needed.")
                else:
                    send_message(chat_id, "This link is invalid or expired.")
        else:
            send_message(chat_id, "Welcome. Please use the link shared with you to register as a trusted contact.")

    return jsonify({"ok": True})


@app.route("/sos", methods=["POST"])
def sos():
    """
    Called by the Android app when SOS is triggered.
    Body: {
      "user_id": "...",
      "secret": "WEBHOOK_SECRET",
      "latitude": ..., "longitude": ...,
      "message": "..."
      (photo/video sent separately via /sos_media, see below)
    }
    """
    body = request.get_json(force=True)
    if body.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    user_id = body.get("user_id")
    lat = body.get("latitude")
    lon = body.get("longitude")
    msg = body.get("message", "🚨 SOS Alert! Immediate help needed.")

    data = load_data()
    contacts = data.get(user_id, {}).get("contacts", [])

    if not contacts:
        return jsonify({"error": "no contacts registered"}), 404

    for contact in contacts:
        chat_id = contact["chat_id"]
        send_message(chat_id, msg)
        if lat is not None and lon is not None:
            send_location(chat_id, lat, lon)

    return jsonify({"ok": True, "notified": len(contacts)})


# For photo/video, the Android app can POST multipart form-data here,
# and we relay it straight to Telegram's sendPhoto / sendVideo using the file bytes.
@app.route("/sos_media", methods=["POST"])
def sos_media():
    secret = request.form.get("secret")
    if secret != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    user_id = request.form.get("user_id")
    media_type = request.form.get("type")  # "photo" or "video"
    file = request.files.get("file")

    if not file or media_type not in ("photo", "video"):
        return jsonify({"error": "file and type required"}), 400

    data = load_data()
    contacts = data.get(user_id, {}).get("contacts", [])
    if not contacts:
        return jsonify({"error": "no contacts registered"}), 404

    method = "sendPhoto" if media_type == "photo" else "sendVideo"
    field = "photo" if media_type == "photo" else "video"

    results = []
    for contact in contacts:
        chat_id = contact["chat_id"]
        file.stream.seek(0)
        resp = requests.post(
            f"{TELEGRAM_API}/{method}",
            data={"chat_id": chat_id},
            files={field: (file.filename, file.stream, file.mimetype)}
        )
        results.append(resp.status_code)

    return jsonify({"ok": True, "results": results})


# ---- HELPERS ----

def send_message(chat_id, text):
    requests.post(f"{TELEGRAM_API}/sendMessage", data={"chat_id": chat_id, "text": text})


def send_location(chat_id, lat, lon):
    requests.post(f"{TELEGRAM_API}/sendLocation", data={"chat_id": chat_id, "latitude": lat, "longitude": lon})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
