import os
import base64
import hashlib
import hmac

import requests
from flask import Flask, abort, request

app = Flask(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


def verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        CHANNEL_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def reply_message(reply_token: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }

    response = requests.post(url, headers=headers, json=payload, timeout=15)
    response.raise_for_status()


@app.get("/")
def home():
    return "LINE Order Bot is running.", 200


@app.post("/callback")
def callback():
    if not CHANNEL_SECRET or not CHANNEL_ACCESS_TOKEN:
        return "Missing LINE environment variables.", 500

    body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not signature or not verify_signature(body, signature):
        abort(400)

    data = request.get_json(silent=True) or {}

    for event in data.get("events", []):
        if (
            event.get("type") == "message"
            and event.get("message", {}).get("type") == "text"
            and event.get("replyToken")
        ):
            user_text = event["message"]["text"]
            reply_message(
                event["replyToken"],
                f"收到訂單訊息：{user_text}",
            )

    return "OK", 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
