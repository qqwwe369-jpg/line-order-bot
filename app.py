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

        if event.get("type") != "message":
            continue

        message = event.get("message", {})

        if message.get("type") != "text":
            continue

        user_text = message.get("text", "").strip()
        reply_token = event.get("replyToken")

        reply_message = handle_message(user_text)

        reply_to_line(reply_token, reply_message)

    return "OK", 200


def handle_message(user_text):

    # 如果使用者輸入「確認」
    if user_text == "確認":

        return (
            "✅ 訂單已確認\n"
            "\n"
            "目前測試階段先到這裡。\n"
            "下一步會把訂單寫入 Google 試算表。"
        )

    # 其他文字都先視為訂購內容
    return (
        "📚 訂購確認\n"
        "\n"
        f"訂購內容：{user_text}\n"
        "\n"
        "請確認以上內容。\n"
        "如果正確，請回覆「確認」"
    )


def reply_to_line(reply_token, message):

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
                "text": message
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

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
