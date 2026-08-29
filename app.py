from flask import Flask, request
import os
import requests

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

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

    # ===== 測試查老師班級 =====
    if user_text.startswith("查老師"):

        lines = user_text.splitlines()

        school = ""
        teacher = ""

        for line in lines:
            line = line.strip()

            if "：" in line:
                key, value = line.split("：", 1)
            elif ":" in line:
                key, value = line.split(":", 1)
            else:
                continue

            key = key.strip()
            value = value.strip()

            if key == "學校":
                school = value

            elif key in ["老師", "老師姓名"]:
                teacher = value

        if not school or not teacher:
            return (
                "⚠️ 請用以下格式：\n\n"
                "查老師\n"
                "學校：天母國中\n"
                "老師姓名：王老師"
            )

        return lookup_teacher(school, teacher)

    # ===== 訂單確認 =====
    if user_text == "確認":

        order = pending_orders.get(user_id)

        if not order:
            return "⚠️ 找不到尚未確認的訂單，請重新輸入。"

        success = write_to_google_sheet(order)

        if success:
            pending_orders.pop(user_id, None)

            return (
                "✅ 訂單已確認\n\n"
                "已成功寫入 Google 試算表。"
            )

        return "❌ 訂單寫入失敗，請稍後再試。"

    # ===== 一般訂單 =====
    order = parse_order(user_text)

    missing = []

    if not order["teacher"]:
        missing.append("老師姓名")

    if not order["school"]:
        missing.append("學校")

    if not order["book"]:
        missing.append("書名")

    if not order["quantity"]:
        missing.append("數量")

    if not order["publisher"]:
        missing.append("出版社")

    if missing:
        return (
            "⚠️ 訂單資料不完整\n\n"
            "缺少：" + "、".join(missing) + "\n\n"
            "請用以下格式輸入：\n\n"
            "老師姓名：王老師\n"
            "學校：天母國中\n"
            "書名：國一數學講義\n"
            "數量：5\n"
            "出版社：翰林"
        )

    pending_orders[user_id] = order

    return (
        "📚 訂購確認\n\n"
        f"老師姓名：{order['teacher']}\n"
        f"學校：{order['school']}\n"
        f"書名：{order['book']}\n"
        f"數量：{order['quantity']}\n"
        f"出版社：{order['publisher']}\n\n"
        "如果正確，請回覆「確認」"
    )


def lookup_teacher(school, teacher):

    if not GOOGLE_SCRIPT_URL:
        return "❌ GOOGLE_SCRIPT_URL 沒有設定"

    data = {
        "action": "lookup_teacher",
        "school": school,
        "teacher": teacher
    }

    try:
        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        result = response.json()

        classes = result.get("classes", [])

        if not classes:
            return (
                "⚠️ 查不到老師資料\n\n"
                f"學校：{school}\n"
                f"老師：{teacher}"
            )

        total = 0
        class_lines = []

        for item in classes:
            class_name = item.get("class_name", "")
            students = int(item.get("students", 0))

            total += students

            class_lines.append(
                f"{class_name}：{students}人"
            )

        return (
            "👨‍🏫 老師班級資料\n\n"
            f"學校：{school}\n"
            f"老師：{teacher}\n\n"
            + "\n".join(class_lines)
            + f"\n\n總人數：{total}人"
        )

    except Exception as error:
        print("Teacher lookup error:", error)
        return "❌ 老師資料查詢失敗"


def parse_order(user_text):

    order = {
        "teacher": "",
        "school": "",
        "book": "",
        "quantity": "",
        "publisher": ""
    }

    lines = user_text.splitlines()

    for line in lines:
        line = line.strip()

        if "：" in line:
            key, value = line.split("：", 1)

        elif ":" in line:
            key, value = line.split(":", 1)

        else:
            continue

        key = key.strip()
        value = value.strip()

        if key in ["老師姓名", "老師"]:
            order["teacher"] = value

        elif key == "學校":
            order["school"] = value

        elif key == "書名":
            order["book"] = value

        elif key == "數量":
            order["quantity"] = value

        elif key == "出版社":
            order["publisher"] = value

    return order


def write_to_google_sheet(order):

    if not GOOGLE_SCRIPT_URL:
        return False

    data = {
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "quantity": order["quantity"],
        "publisher": order["publisher"],
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
