from flask import Flask, request
import os
import requests

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

# 暫時記住每個使用者最後一筆訂購內容
pending_orders = {}


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

        source = event.get("source", {})
        user_id = source.get("userId", "unknown")

        reply_message = handle_message(user_id, user_text)

        reply_to_line(reply_token, reply_message)

    return "OK", 200


def handle_message(user_id, user_text):

    # 使用者回覆「確認」
    if user_text == "確認":

        order_content = pending_orders.get(user_id)

        if not order_content:
            return "⚠️ 找不到尚未確認的訂單，請重新輸入訂購內容。"

        success = write_to_google_sheet(order_content)

        if success:
            pending_orders.pop(user_id, None)

            return (
                "✅ 訂單已確認\n"
                "\n"
                "已成功寫入 Google 試算表。"
            )

        return (
            "❌ 訂單確認失敗\n"
            "\n"
            "Google 試算表寫入失敗，請稍後再試。"
        )

    # 記住這次輸入的訂購內容
    pending_orders[user_id] = user_text

    return (
        "📚 訂購確認\n"
        "\n"
        f"訂購內容：{user_text}\n"
        "\n"
        "請確認以上內容。\n"
        "如果正確，請回覆「確認」"
    )


def write_to_google_sheet(order_content):

    if not GOOGLE_SCRIPT_URL:
        print("❌ GOOGLE_SCRIPT_URL 沒有讀到")
        return False

    data = {
        "order_content": order_content,
        "status": "已確認"
    }

    try:
        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        print("Google Sheet status:", response.status_code)
        print("Google Sheet response:", response.text)

        return response.status_code == 200

    except Exception as error:
        print("Google Sheet error:", error)
        return False


def reply_to_line(reply_token, message):

    url = "https://api.line.me/v2/bot/message/reply"

    if not CHANNEL_ACCESS_TOKEN:
        print("❌ LINE_CHANNEL_ACCESS_TOKEN 沒有讀到")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + CHANNEL_ACCESS_TOKEN
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
        json=data,
        timeout=10
    )

    print("LINE reply status:", response.status_code)
    print("LINE reply response:", response.text)


if __name__ == "__main__":

    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port
    )
