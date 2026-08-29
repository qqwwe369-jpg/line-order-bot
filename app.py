from flask import Flask, request
import os
import requests
import re

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

    # =========================
    # 確認訂單
    # =========================

    if user_text == "確認":

        order = pending_orders.get(user_id)

        if not order:
            return "⚠️ 找不到尚未確認的訂單，請重新輸入。"

        success = write_to_google_sheet(order)

        if success:

            pending_orders.pop(user_id, None)

            return (
                "✅ 訂單已確認\n\n"
                "已成功寫入 Google 試算表。\n\n"
                f"老師：{order['teacher']}\n"
                f"書名：{order['book']}\n"
                f"總數量：{order['quantity']}本"
            )

        return "❌ 訂單寫入失敗，請稍後再試。"


    # =========================
    # 一句話訂書
    # 例如：
    # 王老師訂國一數學講義三個班
    # =========================

    if "訂" in user_text and "老師" in user_text:

        return create_order_from_sentence(
            user_id,
            user_text
        )


    # =========================
    # 查老師功能保留
    # =========================

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

        result = get_teacher_classes(
            school,
            teacher
        )

        if not result:
            return "⚠️ 查不到老師資料"

        classes = result

        total = sum(
            item["students"]
            for item in classes
        )

        lines = []

        for item in classes:

            lines.append(
                f"{item['class_name']}："
                f"{item['students']}人"
            )

        return (
            "👨‍🏫 老師班級資料\n\n"
            f"學校：{school}\n"
            f"老師：{teacher}\n\n"
            + "\n".join(lines)
            + f"\n\n總人數：{total}人"
        )


    return (
        "📚 請輸入訂購內容\n\n"
        "例如：\n"
        "王老師訂國一數學講義三個班"
    )


def create_order_from_sentence(user_id, text):

    # 找老師姓名
    teacher_match = re.search(
        r"(.+?老師)",
        text
    )

    if not teacher_match:
        return "⚠️ 找不到老師姓名"

    teacher = teacher_match.group(1).strip()


    # 找「訂」的位置
    order_position = text.find("訂")

    if order_position == -1:
        return "⚠️ 找不到訂購內容"


    # 抓「訂」後面的文字
    book_part = text[
        order_position + 1:
    ].strip()


    # 移除「三個班 / 3個班」等字樣
    book = re.sub(
        r"[一二三四五六七八九十\d]+個班.*$",
        "",
        book_part
    ).strip()


    if not book:
        return "⚠️ 找不到書名"


    # 目前測試學校
    school = "天母國中"


    # 查老師班級
    classes = get_teacher_classes(
        school,
        teacher
    )

    if not classes:

        return (
            "⚠️ 查不到老師班級資料\n\n"
            f"老師：{teacher}"
        )


    # 判斷使用者說幾個班
    requested_count = extract_class_count(text)

    if requested_count:

        if requested_count != len(classes):

            return (
                "⚠️ 班級數量不一致\n\n"
                f"{teacher}資料庫共有 "
                f"{len(classes)} 個班，\n"
                f"但你這次說要訂 "
                f"{requested_count} 個班。\n\n"
                "請指定要訂哪些班級。"
            )


    total = sum(
        item["students"]
        for item in classes
    )


    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "quantity": total,
        "publisher": "",
        "classes": classes
    }

    pending_orders[user_id] = order


    class_lines = []

    for item in classes:

        class_lines.append(
            f"{item['class_name']}："
            f"{item['students']}本"
        )


    return (
        "📚 訂購確認\n\n"
        f"老師：{teacher}\n"
        f"學校：{school}\n"
        f"書名：{book}\n\n"
        + "\n".join(class_lines)
        + f"\n\n總數量：{total}本\n\n"
        "如果正確，請回覆「確認」"
    )


def extract_class_count(text):

    number_map = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10
    }

    match = re.search(
        r"([一二三四五六七八九十\d]+)個班",
        text
    )

    if not match:
        return None

    value = match.group(1)

    if value.isdigit():
        return int(value)

    return number_map.get(value)


def get_teacher_classes(school, teacher):

    if not GOOGLE_SCRIPT_URL:
        return None

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

        raw_classes = result.get(
            "classes",
            []
        )

        classes = []

        for item in raw_classes:

            classes.append({
                "class_name":
                    str(
                        item.get(
                            "class_name",
                            ""
                        )
                    ),

                "students":
                    int(
                        item.get(
                            "students",
                            0
                        )
                    )
            })

        return classes

    except Exception as error:

        print(
            "Teacher lookup error:",
            error
        )

        return None


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

        print(
            "Google Sheet status:",
            response.status_code
        )

        print(
            "Google Sheet response:",
            response.text
        )

        return response.status_code == 200

    except Exception as error:

        print(
            "Google Sheet error:",
            error
        )

        return False


def reply_to_line(reply_token, message):

    url = (
        "https://api.line.me/"
        "v2/bot/message/reply"
    )

    if not CHANNEL_ACCESS_TOKEN:
        print(
            "❌ LINE_CHANNEL_ACCESS_TOKEN "
            "沒有讀到"
        )
        return

    headers = {
        "Content-Type":
            "application/json",

        "Authorization":
            "Bearer " +
            CHANNEL_ACCESS_TOKEN
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

    print(
        "LINE reply status:",
        response.status_code
    )

    print(
        "LINE reply response:",
        response.text
    )


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
