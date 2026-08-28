from flask import Flask, request
import os
import requests

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")


@app.route("/", methods=["GET"])
def home():
    return "LINE Order Bot is running!"


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_json()

    print("Webhook received")
    print(body)

    events = body.get("events", [])

    for event in events:
        if event.get("type") == "message":
            message = event.get("message", {})

            if message.get("type") == "text":
                user_text = message.get("text", "")
                reply_token = event.get("replyToken")

                reply_message = make_order_reply(user_text)

                reply_to_line(reply_token, reply_message)

    return "OK", 200


def make_order_reply(user_text):

    # 測試用：先把收到的文字整理成訂購確認格式
    reply_message = (
        "📚 訂購確認\n"
        "\n"
        f"訂購內容：{user_text}\n"
        "\n"
        "請確認以上內容。\n"
        "如果正確，請回覆「確認」"
    )

    return reply_message


def reply_to_line(reply_token, message_text):

    url = "https://api.line.me/v2/bot/message/reply"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"
    }

    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": message_text
            }
        ]
    }

    response = requests.post(
        url,
        headers=headers,
        json=data
    )

    print("LINE reply status:", response.status_code)
    print("LINE reply response:", response.text)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
