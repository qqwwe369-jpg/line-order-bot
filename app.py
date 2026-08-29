from flask import Flask, request
import os
import requests
import re

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

# 尚未確認的訂單
pending_orders = {}

# 最近查詢的老師
conversation_context = {}


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

        reply_message = handle_message(
            user_id,
            user_text
        )

        reply_to_line(
            reply_token,
            reply_message
        )

    return "OK", 200


def handle_message(user_id, user_text):

    # =========================
    # 1. 確認訂單
    # =========================
    if user_text == "確認":

        order = pending_orders.get(user_id)

        if not order:
            return (
                "⚠️ 找不到尚未確認的訂單，"
                "請重新輸入。"
            )

        success = write_to_google_sheet(order)

        if success:

            pending_orders.pop(
                user_id,
                None
            )

            return (
                "✅ 訂單已確認\n\n"
                "已成功寫入 Google 試算表。\n\n"
                f"老師：{order['teacher']}\n"
                f"書名：{order['book']}\n"
                f"出版社：{order['publisher']}\n"
                f"總數量：{order['quantity']}本"
            )

        return (
            "❌ 訂單寫入失敗，"
            "請稍後再試。"
        )


    # =========================
    # 2. 修改尚未確認的訂單
    #
    # 例如：
    # 701改28人
    # 701改28本
    # 701少2本
    # 705多1本
    # =========================
    if user_id in pending_orders:

        if re.search(
            r"\d+\s*(改成|改為|改|多|少)\s*\d+",
            user_text
        ):

            return adjust_pending_order(
                user_id,
                user_text
            )


    # =========================
    # 3. 查老師教哪幾班
    # =========================
    if (
        "老師" in user_text
        and (
            "教幾個班" in user_text
            or "教幾班" in user_text
            or "教哪幾班" in user_text
            or "教哪些班" in user_text
            or "總共教幾個班" in user_text
        )
        and "訂" not in user_text
    ):

        return handle_teacher_lookup(
            user_id,
            user_text
        )


    # =========================
    # 4. 延續上一位老師直接訂
    #
    # 例如：
    # 訂701跟705，國一數學講義
    # =========================
    if (
        user_text.startswith("訂")
        and "老師" not in user_text
    ):

        context = conversation_context.get(
            user_id
        )

        if context:

            return create_order_from_context(
                user_id,
                user_text,
                context
            )


    # =========================
    # 5. 完整一句話訂書
    #
    # 例如：
    # 王老師訂國一數學講義三個班
    # =========================
    if (
        "訂" in user_text
        and "老師" in user_text
    ):

        return create_order_from_sentence(
            user_id,
            user_text
        )


    # =========================
    # 6. 原本查老師格式
    # =========================
    if user_text.startswith("查老師"):

        lines = user_text.splitlines()

        school = ""
        teacher = ""

        for line in lines:

            line = line.strip()

            if "：" in line:
                key, value = line.split(
                    "：",
                    1
                )

            elif ":" in line:
                key, value = line.split(
                    ":",
                    1
                )

            else:
                continue

            key = key.strip()
            value = value.strip()

            if key == "學校":
                school = value

            elif key in [
                "老師",
                "老師姓名"
            ]:
                teacher = value

        if not school or not teacher:

            return (
                "⚠️ 請用以下格式：\n\n"
                "查老師\n"
                "學校：天母國中\n"
                "老師姓名：王老師"
            )

        classes = get_teacher_classes(
            school,
            teacher
        )

        if not classes:
            return "⚠️ 查不到老師資料"

        conversation_context[user_id] = {
            "school": school,
            "teacher": teacher,
            "classes": classes
        }

        return make_teacher_reply(
            school,
            teacher,
            classes
        )


    return (
        "📚 請輸入訂購內容\n\n"
        "例如：\n"
        "王老師訂國一數學講義三個班\n\n"
        "也可以先問：\n"
        "王老師教哪幾班？"
    )


# =========================
# 修改尚未確認的訂單
# =========================
def adjust_pending_order(
    user_id,
    text
):

    order = pending_orders.get(user_id)

    if not order:

        return (
            "⚠️ 目前沒有尚未確認的訂單。"
        )

    classes = order.get(
        "classes",
        []
    )

    if not classes:

        return (
            "⚠️ 這筆訂單沒有班級資料，"
            "無法調整。"
        )

    # 支援：
    # 701改28
    # 701改成28
    # 701改為28
    # 701少2
    # 701多1
    #
    # 也支援一次修改多班：
    # 701改28，705改30

    pattern = re.compile(
        r"(?<!\d)"
        r"(\d{2,4})"
        r"(?!\d)"
        r"\s*"
        r"(改成|改為|改|多|少)"
        r"\s*"
        r"(\d+)"
        r"\s*"
        r"(?:人|本)?"
    )

    matches = list(
        pattern.finditer(text)
    )

    if not matches:

        return (
            "⚠️ 我看不懂要怎麼修改。\n\n"
            "例如可以說：\n"
            "701改28人\n"
            "或\n"
            "701少2本"
        )

    changed = []
    errors = []

    for match in matches:

        class_name = match.group(1)
        action = match.group(2)
        value = int(match.group(3))

        target_class = None

        for item in classes:

            if (
                str(item["class_name"])
                == class_name
            ):

                target_class = item
                break

        if not target_class:

            errors.append(
                f"{class_name} 不在這筆訂單裡"
            )

            continue

        old_quantity = int(
            target_class.get(
                "students",
                0
            )
        )

        # -------------------------
        # 改成指定數量
        # -------------------------
        if action in [
            "改",
            "改成",
            "改為"
        ]:

            new_quantity = value


        # -------------------------
        # 多幾本
        # -------------------------
        elif action == "多":

            new_quantity = (
                old_quantity + value
            )


        # -------------------------
        # 少幾本
        # -------------------------
        elif action == "少":

            new_quantity = (
                old_quantity - value
            )


        else:

            continue


        # 不允許負數
        if new_quantity < 0:

            errors.append(
                f"{class_name} 調整後不能小於 0"
            )

            continue


        target_class["students"] = (
            new_quantity
        )

        changed.append(
            f"{class_name}："
            f"{old_quantity} → "
            f"{new_quantity}本"
        )


    if not changed:

        if errors:

            return (
                "⚠️ 無法修改訂單\n\n"
                + "\n".join(errors)
            )

        return (
            "⚠️ 沒有找到可以修改的班級。"
        )


    # =========================
    # 重新計算總數
    # =========================
    total = sum(
        int(item["students"])
        for item in classes
    )

    order["quantity"] = total
    order["classes"] = classes

    pending_orders[user_id] = order


    # =========================
    # 回傳新的確認畫面
    # =========================
    message = (
        "✅ 已調整訂單\n\n"
        + "\n".join(changed)
    )

    if errors:

        message += (
            "\n\n⚠️ "
            + "\n".join(errors)
        )

    message += (
        "\n\n"
        + make_order_confirmation(order)
    )

    return message


# =========================
# 查老師
# =========================
def handle_teacher_lookup(
    user_id,
    text
):

    teacher_match = re.search(
        r"(.+?老師)",
        text
    )

    if not teacher_match:
        return "⚠️ 找不到老師姓名"

    teacher = teacher_match.group(1).strip()

    # 目前測試階段先固定天母國中
    school = "天母國中"

    classes = get_teacher_classes(
        school,
        teacher
    )

    if not classes:

        return (
            "⚠️ 查不到老師資料\n\n"
            f"學校：{school}\n"
            f"老師：{teacher}"
        )

    conversation_context[user_id] = {
        "school": school,
        "teacher": teacher,
        "classes": classes
    }

    return make_teacher_reply(
        school,
        teacher,
        classes
    )


# =========================
# 老師班級資料回覆
# =========================
def make_teacher_reply(
    school,
    teacher,
    classes
):

    total = sum(
        item["students"]
        for item in classes
    )

    class_lines = []

    for item in classes:

        class_lines.append(
            f"{item['class_name']}："
            f"{item['students']}人"
        )

    return (
        "👨‍🏫 老師班級資料\n\n"
        f"學校：{school}\n"
        f"老師：{teacher}\n"
        f"共教 {len(classes)} 個班\n\n"
        + "\n".join(class_lines)
        + f"\n\n總人數：{total}人\n\n"
        "你希望我幫你訂哪幾個班？"
    )


# =========================
# 延續剛剛查詢的老師訂書
# =========================
def create_order_from_context(
    user_id,
    text,
    context
):

    teacher = context["teacher"]
    school = context["school"]

    all_classes = context.get(
        "classes",
        []
    )

    if not all_classes:

        all_classes = get_teacher_classes(
            school,
            teacher
        )

    if not all_classes:

        return (
            "⚠️ 找不到剛才的老師班級資料，"
            "請重新查一次老師。"
        )

    # 去掉開頭的「訂」
    order_part = text[1:].strip()

    return build_order(
        user_id=user_id,
        teacher=teacher,
        school=school,
        order_part=order_part,
        all_classes=all_classes,
        original_text=text
    )


# =========================
# 完整一句話訂書
# =========================
def create_order_from_sentence(
    user_id,
    text
):

    teacher_match = re.search(
        r"(.+?老師)",
        text
    )

    if not teacher_match:
        return "⚠️ 找不到老師姓名"

    teacher = teacher_match.group(1).strip()

    # 目前測試階段先固定天母國中
    school = "天母國中"

    order_position = text.find("訂")

    if order_position == -1:
        return "⚠️ 找不到訂購內容"

    order_part = text[
        order_position + 1:
    ].strip()

    all_classes = get_teacher_classes(
        school,
        teacher
    )

    if not all_classes:

        return (
            "⚠️ 查不到老師班級資料\n\n"
            f"老師：{teacher}"
        )

    conversation_context[user_id] = {
        "school": school,
        "teacher": teacher,
        "classes": all_classes
    }

    return build_order(
        user_id=user_id,
        teacher=teacher,
        school=school,
        order_part=order_part,
        all_classes=all_classes,
        original_text=text
    )


# =========================
# 建立訂單
# =========================
def build_order(
    user_id,
    teacher,
    school,
    order_part,
    all_classes,
    original_text
):

    available_class_names = [
        str(item["class_name"])
        for item in all_classes
    ]

    selected_class_names = []

    # =========================
    # 找指定班級
    # =========================
    for class_name in available_class_names:

        pattern = (
            r"(?<!\d)"
            + re.escape(class_name)
            + r"(?!\d)"
        )

        if re.search(
            pattern,
            order_part
        ):

            selected_class_names.append(
                class_name
            )


    # =========================
    # 有指定班級
    # =========================
    if selected_class_names:

        classes = []

        for item in all_classes:

            if (
                str(item["class_name"])
                in selected_class_names
            ):

                # 建立新的 dict
                # 避免修改到 conversation_context
                classes.append({
                    "class_name":
                        str(item["class_name"]),

                    "students":
                        int(item["students"])
                })

        book = order_part

        # 移除班級號碼
        for class_name in selected_class_names:

            book = re.sub(
                r"(?<!\d)"
                + re.escape(class_name)
                + r"(?!\d)",
                "",
                book
            )

        # 清除標點
        book = re.sub(
            r"[、,，/]+",
            " ",
            book
        )

        # 清除前面的跟、和、與
        book = re.sub(
            r"^[跟和與]+",
            "",
            book
        )

        book = re.sub(
            r"\s+",
            " ",
            book
        ).strip()


    # =========================
    # 沒指定班級
    # =========================
    else:

        # 建立新的班級資料
        classes = []

        for item in all_classes:

            classes.append({
                "class_name":
                    str(item["class_name"]),

                "students":
                    int(item["students"])
            })

        book = re.sub(
            r"[一二三四五六七八九十\d]+個班.*$",
            "",
            order_part
        ).strip()

        requested_count = extract_class_count(
            original_text
        )

        if requested_count:

            if (
                requested_count
                != len(all_classes)
            ):

                class_names = "、".join(
                    available_class_names
                )

                return (
                    "⚠️ 需要指定班級\n\n"
                    f"{teacher}共有 "
                    f"{len(all_classes)} 個班：\n"
                    f"{class_names}\n\n"
                    f"你這次要訂 "
                    f"{requested_count} 個班。\n\n"
                    "請直接告訴我要哪幾班。"
                )


    # =========================
    # 清理書名
    # =========================
    book = clean_book_name(book)

    if not book:

        class_names = "、".join(
            selected_class_names
        )

        if class_names:

            return (
                f"好的，目前選的是 "
                f"{class_names}。\n\n"
                "請再告訴我書名。"
            )

        return "⚠️ 找不到書名"


    # =========================
    # 查出版社
    # =========================
    publisher = get_book_publisher(
        book
    )

    if not publisher:

        return (
            "⚠️ 查不到書籍資料\n\n"
            f"書名：{book}\n\n"
            "請先到「書籍資料」"
            "工作表新增這本書與出版社。"
        )


    # =========================
    # 算總數量
    # =========================
    total = sum(
        item["students"]
        for item in classes
    )


    # =========================
    # 暫存訂單
    # =========================
    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "quantity": total,
        "publisher": publisher,
        "classes": classes
    }

    pending_orders[user_id] = order


    # =========================
    # 產生確認畫面
    # =========================
    return make_order_confirmation(
        order
    )


# =========================
# 統一產生訂購確認
# =========================
def make_order_confirmation(order):

    class_lines = []

    for item in order["classes"]:

        class_lines.append(
            f"{item['class_name']}："
            f"{item['students']}本"
        )

    return (
        "📚 訂購確認\n\n"
        f"老師：{order['teacher']}\n"
        f"學校：{order['school']}\n"
        f"書名：{order['book']}\n"
        f"出版社：{order['publisher']}\n\n"
        + "\n".join(class_lines)
        + f"\n\n總數量："
        f"{order['quantity']}本\n\n"
        "如果正確，請回覆「確認」"
    )


# =========================
# 清理書名
# =========================
def clean_book_name(book):

    book = book.strip()

    book = re.sub(
        r"^[、,，。:：\s]+",
        "",
        book
    )

    book = re.sub(
        r"^[跟和與]+",
        "",
        book
    )

    book = re.sub(
        r"[、,，。:：\s]+$",
        "",
        book
    )

    return book.strip()


# =========================
# 找「幾個班」
# =========================
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


# =========================
# 查老師班級
# =========================
def get_teacher_classes(
    school,
    teacher
):

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
                "class_name": str(
                    item.get(
                        "class_name",
                        ""
                    )
                ),

                "students": int(
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


# =========================
# 查出版社
# =========================
def get_book_publisher(book):

    if not GOOGLE_SCRIPT_URL:
        return None

    data = {
        "action": "lookup_book",
        "book": book
    }

    try:

        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        result = response.json()

        if result.get("found"):

            return result.get(
                "publisher",
                ""
            )

        return None

    except Exception as error:

        print(
            "Book lookup error:",
            error
        )

        return None


# =========================
# 寫入 Google 試算表
# =========================
def write_to_google_sheet(order):

    if not GOOGLE_SCRIPT_URL:
        return False

    data = {
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "quantity": order["quantity"],
        "publisher": order["publisher"],

        "classes": order.get(
            "classes",
            []
        ),

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

        return (
            response.status_code
            == 200
        )

    except Exception as error:

        print(
            "Google Sheet error:",
            error
        )

        return False


# =========================
# LINE 回覆
# =========================
def reply_to_line(
    reply_token,
    message
):

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
            "Bearer "
            + CHANNEL_ACCESS_TOKEN
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
