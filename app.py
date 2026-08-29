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

    body = request.get_json(silent=True) or {}

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


# =========================================================
# 主對話處理
# =========================================================
def handle_message(user_id, user_text):

    text = normalize_text(user_text)

    # -----------------------------------------------------
    # 1. 重來
    # -----------------------------------------------------
    if text in ["重來", "重新開始", "全部重來"]:

        pending_orders.pop(user_id, None)
        conversation_context.pop(user_id, None)

        return (
            "🔄 已重新開始\n\n"
            "目前的老師與尚未確認訂單都已清除。\n\n"
            "請告訴我你要查哪位老師。"
        )


    # -----------------------------------------------------
    # 2. 取消目前訂單
    # -----------------------------------------------------
    if text in [
        "取消",
        "取消訂單",
        "不要了",
        "這筆不要"
    ]:

        if user_id not in pending_orders:

            return (
                "目前沒有尚未確認的訂單。"
            )

        pending_orders.pop(user_id, None)

        return (
            "❌ 已取消這筆訂單。\n\n"
            "老師資料仍然保留，"
            "你可以繼續重新選班級訂書。"
        )


    # -----------------------------------------------------
    # 3. 顯示目前訂單
    # -----------------------------------------------------
    if text in [
        "目前訂單",
        "看訂單",
        "訂單內容",
        "現在訂單"
    ]:

        order = pending_orders.get(user_id)

        if not order:

            return (
                "目前沒有尚未確認的訂單。"
            )

        return make_order_confirmation(order)


    # -----------------------------------------------------
    # 4. 確認訂單
    # -----------------------------------------------------
    if text == "確認":

        order = pending_orders.get(user_id)

        if not order:

            return (
                "⚠️ 找不到尚未確認的訂單，"
                "請重新輸入。"
            )

        success = write_to_google_sheet(order)

        if success:

            pending_orders.pop(user_id, None)

            return (
                "✅ 訂單已確認\n\n"
                "已成功寫入 Google 試算表。\n\n"
                f"老師：{order['teacher']}\n"
                f"書名：{order['book']}\n"
                f"出版社：{order['publisher']}\n"
                f"總數量：{order['quantity']}本"
            )

        return (
            "❌ 訂單寫入失敗，請稍後再試。"
        )


    # -----------------------------------------------------
    # 5. 已有待確認訂單時，優先判斷修改指令
    # -----------------------------------------------------
    if user_id in pending_orders:

        # 不要705
        if re.search(
            r"(不要|刪除|拿掉|移除)\s*\d{2,4}",
            text
        ):
            return remove_class_from_order(
                user_id,
                text
            )

        # 加703
        if re.search(
            r"(加|加入|增加)\s*\d{2,4}",
            text
        ):
            return add_class_to_order(
                user_id,
                text
            )

        # 701改成703
        if re.search(
            r"\d{2,4}\s*(?:改成|換成|改為)\s*\d{2,4}",
            text
        ):
            return replace_class_in_order(
                user_id,
                text
            )

        # 701改28 / 701少2 / 701多1
        if re.search(
            r"\d{2,4}\s*(?:改成|改為|改|多|少)\s*\d+",
            text
        ):
            return adjust_pending_order(
                user_id,
                text
            )

        # 改成國一自然講義
        if (
            text.startswith("改成")
            or text.startswith("書名改成")
            or text.startswith("換成")
            or text.startswith("書改成")
        ):
            return change_book(
                user_id,
                text
            )


    # -----------------------------------------------------
    # 6. 查老師教哪幾班
    # -----------------------------------------------------
    if (
        "老師" in text
        and (
            "教幾個班" in text
            or "教幾班" in text
            or "教哪幾班" in text
            or "教哪些班" in text
            or "總共教幾個班" in text
        )
        and "訂" not in text
    ):

        return handle_teacher_lookup(
            user_id,
            text
        )


    # -----------------------------------------------------
    # 7. 延續剛剛老師直接訂
    # -----------------------------------------------------
    if (
        text.startswith("訂")
        and "老師" not in text
    ):

        context = conversation_context.get(
            user_id
        )

        if context:

            return create_order_from_context(
                user_id,
                text,
                context
            )


    # -----------------------------------------------------
    # 8. 完整一句話訂書
    # -----------------------------------------------------
    if (
        "訂" in text
        and "老師" in text
    ):

        return create_order_from_sentence(
            user_id,
            text
        )


    # -----------------------------------------------------
    # 9. 原本的查老師格式
    # -----------------------------------------------------
    if text.startswith("查老師"):

        return handle_form_teacher_lookup(
            user_id,
            user_text
        )


    return (
        "📚 請輸入訂購內容\n\n"
        "你可以先問：\n"
        "王老師教哪幾班？\n\n"
        "也可以直接說：\n"
        "王老師訂國一數學講義三個班"
    )


# =========================================================
# 文字標準化
# =========================================================
def normalize_text(text):

    text = text.strip()

    text = text.replace("　", " ")

    return text


# =========================================================
# 查老師
# =========================================================
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

    # 目前測試階段固定天母國中
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
        "classes": copy_classes(classes)
    }

    return make_teacher_reply(
        school,
        teacher,
        classes
    )


def handle_form_teacher_lookup(
    user_id,
    user_text
):

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
        "classes": copy_classes(classes)
    }

    return make_teacher_reply(
        school,
        teacher,
        classes
    )


def make_teacher_reply(
    school,
    teacher,
    classes
):

    total = sum(
        int(item["students"])
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


# =========================================================
# 延續老師訂書
# =========================================================
def create_order_from_context(
    user_id,
    text,
    context
):

    teacher = context["teacher"]
    school = context["school"]

    all_classes = copy_classes(
        context.get("classes", [])
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

    order_part = text[1:].strip()

    return build_order(
        user_id,
        teacher,
        school,
        order_part,
        all_classes,
        text
    )


# =========================================================
# 完整一句話訂書
# =========================================================
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
        "classes": copy_classes(all_classes)
    }

    return build_order(
        user_id,
        teacher,
        school,
        order_part,
        all_classes,
        text
    )


# =========================================================
# 建立訂單
# =========================================================
def build_order(
    user_id,
    teacher,
    school,
    order_part,
    all_classes,
    original_text
):

    available_names = [
        str(item["class_name"])
        for item in all_classes
    ]

    selected_names = []

    for class_name in available_names:

        pattern = (
            r"(?<!\d)"
            + re.escape(class_name)
            + r"(?!\d)"
        )

        if re.search(pattern, order_part):

            selected_names.append(
                class_name
            )


    # -----------------------------------------------------
    # 有指定班級
    # -----------------------------------------------------
    if selected_names:

        classes = []

        for item in all_classes:

            if str(item["class_name"]) in selected_names:

                classes.append({
                    "class_name":
                        str(item["class_name"]),

                    "students":
                        int(item["students"])
                })

        book = order_part

        for class_name in selected_names:

            book = re.sub(
                r"(?<!\d)"
                + re.escape(class_name)
                + r"(?!\d)",
                "",
                book
            )

        book = re.sub(
            r"[、,，/]+",
            " ",
            book
        )

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


    # -----------------------------------------------------
    # 沒指定班級
    # -----------------------------------------------------
    else:

        classes = copy_classes(
            all_classes
        )

        book = re.sub(
            r"[一二三四五六七八九十\d]+個班.*$",
            "",
            order_part
        ).strip()

        requested_count = extract_class_count(
            original_text
        )

        if (
            requested_count
            and requested_count != len(all_classes)
        ):

            class_names = "、".join(
                available_names
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


    book = clean_book_name(book)

    if not book:

        return (
            "⚠️ 找不到書名，"
            "請把班級和書名一起告訴我。"
        )


    publisher = get_book_publisher(
        book
    )

    if not publisher:

        return (
            "⚠️ 查不到書籍資料\n\n"
            f"書名：{book}\n\n"
            "請先到「書籍資料」工作表"
            "新增這本書與出版社。"
        )


    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "publisher": publisher,
        "classes": classes,
        "quantity": calculate_total(classes)
    }

    pending_orders[user_id] = order

    return make_order_confirmation(order)


# =========================================================
# 改班級數量
# 701改28
# 701少2
# 701多1
# =========================================================
def adjust_pending_order(
    user_id,
    text
):

    order = pending_orders[user_id]

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
            "⚠️ 我看不懂要怎麼修改。"
        )

    changes = []

    for match in matches:

        class_name = match.group(1)
        action = match.group(2)
        value = int(match.group(3))

        target = find_order_class(
            order,
            class_name
        )

        if not target:

            return (
                f"⚠️ {class_name} "
                "目前不在這筆訂單裡。"
            )

        old_value = int(
            target["students"]
        )

        if action in [
            "改",
            "改成",
            "改為"
        ]:
            new_value = value

        elif action == "多":
            new_value = old_value + value

        else:
            new_value = old_value - value

        if new_value < 0:

            return (
                "⚠️ 數量不能小於 0。"
            )

        target["students"] = new_value

        changes.append(
            f"{class_name}："
            f"{old_value} → {new_value}本"
        )

    refresh_order_total(order)

    return (
        "✅ 已調整\n\n"
        + "\n".join(changes)
        + "\n\n"
        + make_order_confirmation(order)
    )


# =========================================================
# 移除班級
# 不要705
# =========================================================
def remove_class_from_order(
    user_id,
    text
):

    order = pending_orders[user_id]

    match = re.search(
        r"(?:不要|刪除|拿掉|移除)\s*(\d{2,4})",
        text
    )

    if not match:
        return "⚠️ 找不到要移除的班級。"

    class_name = match.group(1)

    target = find_order_class(
        order,
        class_name
    )

    if not target:

        return (
            f"⚠️ {class_name} "
            "目前不在這筆訂單裡。"
        )

    if len(order["classes"]) <= 1:

        return (
            "⚠️ 這是訂單最後一個班級。\n\n"
            "如果整筆不要了，請直接說「取消」。"
        )

    order["classes"] = [
        item
        for item in order["classes"]
        if str(item["class_name"]) != class_name
    ]

    refresh_order_total(order)

    return (
        f"✅ 已移除 {class_name}\n\n"
        + make_order_confirmation(order)
    )


# =========================================================
# 加入班級
# 加703
# =========================================================
def add_class_to_order(
    user_id,
    text
):

    order = pending_orders[user_id]

    match = re.search(
        r"(?:加|加入|增加)\s*(\d{2,4})",
        text
    )

    if not match:
        return "⚠️ 找不到要加入的班級。"

    class_name = match.group(1)

    if find_order_class(order, class_name):

        return (
            f"⚠️ {class_name} "
            "已經在這筆訂單裡了。"
        )

    context = conversation_context.get(
        user_id
    )

    if not context:

        return (
            "⚠️ 找不到老師班級資料，"
            "請重新查一次老師。"
        )

    source_class = None

    for item in context.get(
        "classes",
        []
    ):

        if (
            str(item["class_name"])
            == class_name
        ):
            source_class = item
            break

    if not source_class:

        return (
            f"⚠️ {order['teacher']} "
            f"沒有 {class_name} 這個班。"
        )

    order["classes"].append({
        "class_name":
            str(source_class["class_name"]),

        "students":
            int(source_class["students"])
    })

    refresh_order_total(order)

    return (
        f"✅ 已加入 {class_name}\n\n"
        + make_order_confirmation(order)
    )


# =========================================================
# 更換班級
# 701改成703
# =========================================================
def replace_class_in_order(
    user_id,
    text
):

    order = pending_orders[user_id]

    match = re.search(
        r"(\d{2,4})\s*"
        r"(?:改成|換成|改為)\s*"
        r"(\d{2,4})",
        text
    )

    if not match:

        return (
            "⚠️ 找不到要更換的班級。"
        )

    old_class = match.group(1)
    new_class = match.group(2)

    old_target = find_order_class(
        order,
        old_class
    )

    if not old_target:

        return (
            f"⚠️ {old_class} "
            "目前不在這筆訂單裡。"
        )

    if find_order_class(
        order,
        new_class
    ):

        return (
            f"⚠️ {new_class} "
            "已經在這筆訂單裡。"
        )

    context = conversation_context.get(
        user_id
    )

    if not context:

        return (
            "⚠️ 找不到老師班級資料。"
        )

    new_source = None

    for item in context.get(
        "classes",
        []
    ):

        if (
            str(item["class_name"])
            == new_class
        ):
            new_source = item
            break

    if not new_source:

        return (
            f"⚠️ {order['teacher']} "
            f"沒有 {new_class} 這個班。"
        )

    order["classes"] = [
        item
        for item in order["classes"]
        if str(item["class_name"]) != old_class
    ]

    order["classes"].append({
        "class_name":
            str(new_source["class_name"]),

        "students":
            int(new_source["students"])
    })

    refresh_order_total(order)

    return (
        f"✅ 已把 {old_class} "
        f"改成 {new_class}\n\n"
        + make_order_confirmation(order)
    )


# =========================================================
# 更換書名
# 改成國一自然講義
# =========================================================
def change_book(
    user_id,
    text
):

    order = pending_orders[user_id]

    new_book = re.sub(
        r"^(?:書名)?(?:改成|換成|書改成)",
        "",
        text
    ).strip()

    new_book = clean_book_name(
        new_book
    )

    if not new_book:

        return (
            "⚠️ 請告訴我要改成哪一本書。"
        )

    publisher = get_book_publisher(
        new_book
    )

    if not publisher:

        return (
            "⚠️ 查不到這本書\n\n"
            f"書名：{new_book}\n\n"
            "請先確認「書籍資料」裡"
            "是否已經有這本書。"
        )

    old_book = order["book"]

    order["book"] = new_book
    order["publisher"] = publisher

    return (
        "✅ 已更換書籍\n\n"
        f"{old_book}\n"
        f"→ {new_book}\n\n"
        + make_order_confirmation(order)
    )


# =========================================================
# 訂購確認
# =========================================================
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


# =========================================================
# 小工具
# =========================================================
def find_order_class(
    order,
    class_name
):

    for item in order.get(
        "classes",
        []
    ):

        if (
            str(item["class_name"])
            == str(class_name)
        ):
            return item

    return None


def copy_classes(classes):

    result = []

    for item in classes:

        result.append({
            "class_name":
                str(item["class_name"]),

            "students":
                int(item["students"])
        })

    return result


def calculate_total(classes):

    return sum(
        int(item["students"])
        for item in classes
    )


def refresh_order_total(order):

    order["quantity"] = (
        calculate_total(
            order["classes"]
        )
    )


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


# =========================================================
# Google Sheet：查老師
# =========================================================
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


# =========================================================
# Google Sheet：查書籍
# =========================================================
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


# =========================================================
# 寫入 Google Sheet
# =========================================================
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
            response.status_code == 200
        )

    except Exception as error:

        print(
            "Google Sheet error:",
            error
        )

        return False


# =========================================================
# LINE 回覆
# =========================================================
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

    try:

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

    except Exception as error:

        print(
            "LINE reply error:",
            error
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
