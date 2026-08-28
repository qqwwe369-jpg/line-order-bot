from flask import Flask, request
import os
import json
import urllib.request

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "LINE Order Bot is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_json(silent=True)

    print("Webhook received")
    print(body)

    if not body:
        return "OK", 200

    events = body.get("events", [])

    for event in events:
        if (
            event.get("type") == "message"
            and event.get("message", {}).get("type") == "text"
        ):
            user_message = event["message"]["text"]
            reply_token = event.get("replyToken")

            print("收到訊息：", user_message)

            if reply_token:
                reply_message(reply_token, f"我收到你的訊息了：{user_message}")

    return "OK", 200


def reply_message(reply_token, text):
    channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")

    if not channel_access_token:
        print("錯誤：找不到 LINE_CHANNEL_ACCESS_TOKEN")
        return

    url = "https://api.line.me/v2/bot/message/reply"

    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": text
            }
        ]
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {channel_access_token}"
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers=headers,
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as response:
            print("LINE 回覆成功：", response.status)
    except Exception as e:
        print("LINE 回覆失敗：", e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
