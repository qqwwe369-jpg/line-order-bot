from flask import Flask, request
import os
import requests
import re

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

# OpenAI 一般問答
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
AI_FALLBACK_MESSAGE = "👑 LeBron James 正在幫你處理中，請稍後再試一次。"

# 尚未確認的訂單
pending_orders = {}

# 最近查詢的老師
conversation_context = {}

# 最近查詢的歷史訂單
historical_order_context = {}

# 等待確認的歷史訂單修改
pending_history_updates = {}

# 一般 AI 問答的短期對話紀錄（Render 重啟後會清除）
ai_conversation_context = {}


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
        historical_order_context.pop(user_id, None)
        pending_history_updates.pop(user_id, None)
        ai_conversation_context.pop(user_id, None)

        return (
            "🔄 已重新開始\n\n"
            "目前的老師、尚未確認訂單與歷史訂單修改狀態都已清除。\n\n"
            "請告訴我你要查哪位老師。"
        )

    # -----------------------------------------------------
    # 2. 取消歷史訂單修改
    # -----------------------------------------------------
    if text in ["取消修改", "不要修改"]:

        if user_id not in pending_history_updates:
            return "目前沒有等待確認的歷史訂單修改。"

        pending_history_updates.pop(user_id, None)

        return "❌ 已取消這次歷史訂單修改，Google 原訂單沒有變動。"

    # -----------------------------------------------------
    # 3. 確認歷史訂單修改
    # 支援「確認修改」與直接「確認」
    # -----------------------------------------------------
    if (
        text in ["確認修改", "確認"]
        and user_id in pending_history_updates
    ):

        update_data = pending_history_updates.get(user_id)

        if not update_data:
            return "⚠️ 找不到等待確認的歷史訂單修改。"

        success, result = update_google_order(
            update_data["order"],
            update_data["modification_text"]
        )

        if not success:
            return "❌ 歷史訂單修改失敗，Google 原訂單沒有變動。"

        order_number = update_data["order"]["order_number"]

        refreshed = lookup_google_order(order_number)

        if refreshed:
            historical_order_context[user_id] = refreshed

        pending_history_updates.pop(user_id, None)

        return (
            "✅ 訂單修改完成\n\n"
            f"訂單編號：{order_number}\n"
            "Google 試算表已更新。\n"
            "狀態：已修改\n"
            "修改紀錄：已保存"
        )

    # -----------------------------------------------------
    # 4. 查歷史訂單
    # 支援：查001 / 001訂單內容是什麼 / 訂單001
    # -----------------------------------------------------
    order_number = extract_order_lookup_number(text)

    if order_number:

        order = lookup_google_order(order_number)

        if not order:
            return f"⚠️ 查不到訂單 {order_number}。"

        historical_order_context[user_id] = order
        pending_history_updates.pop(user_id, None)

        return make_historical_order_reply(order)

    # -----------------------------------------------------
    # 5. 查過歷史訂單後，可直接說 701改28
    # 這段一定要放在「直接指定訂單」之前，
    # 避免把 705改35 誤判成訂單 007。
    # -----------------------------------------------------
    if (
        user_id in historical_order_context
        and looks_like_history_edit(text)
        and user_id not in pending_orders
    ):

        return prepare_history_adjustment(
            user_id,
            historical_order_context[user_id],
            text
        )

    # -----------------------------------------------------
    # 6. 直接指定歷史訂單修改
    # 例如：001的701改28本
    # 或：訂單001的701改28本
    # -----------------------------------------------------
    direct_history = parse_direct_history_adjustment(text)

    if direct_history:

        order_number = direct_history["order_number"]
        edit_text = direct_history["edit_text"]

        order = lookup_google_order(order_number)

        if not order:
            return f"⚠️ 查不到訂單 {order_number}。"

        historical_order_context[user_id] = order

        return prepare_history_adjustment(
            user_id,
            order,
            edit_text
        )

    # -----------------------------------------------------
    # 7. 取消目前新訂單
    # -----------------------------------------------------
    if text in [
        "取消",
        "取消訂單",
        "不要了",
        "這筆不要"
    ]:

        if user_id not in pending_orders:
            return "目前沒有尚未確認的新訂單。"

        pending_orders.pop(user_id, None)

        return (
            "❌ 已取消這筆訂單。\n\n"
            "老師資料仍然保留，"
            "你可以繼續重新選班級訂書。"
        )

    # -----------------------------------------------------
    # 8. 顯示目前新訂單
    # -----------------------------------------------------
    if text in [
        "目前訂單",
        "看訂單",
        "訂單內容",
        "現在訂單"
    ]:

        order = pending_orders.get(user_id)

        if not order:
            return "目前沒有尚未確認的新訂單。"

        return make_order_confirmation(order)

    # -----------------------------------------------------
    # 9. 確認新訂單
    # Google 端此時正式產生 001 / 002 / 003...
    # -----------------------------------------------------
    if text == "確認":

        order = pending_orders.get(user_id)

        if not order:
            return (
                "⚠️ 找不到尚未確認的訂單，"
                "請重新輸入。"
            )

        success, order_number = write_to_google_sheet(order)

        if success:

            pending_orders.pop(user_id, None)

            return (
                "✅ 訂單已確認\n\n"
                f"訂單編號：{order_number}\n"
                "已成功寫入 Google 試算表。\n\n"
                f"老師：{order['teacher']}\n"
                f"書名：{order['book']}\n"
                f"出版社：{order['publisher']}\n"
                f"總數量：{order['quantity']}本\n\n"
                f"之後可以直接問「查{order_number}」"
            )

        return "❌ 訂單寫入失敗，請稍後再試。"

    # -----------------------------------------------------
    # 10. 已有待確認新訂單時，優先判斷修改指令
    # -----------------------------------------------------
    if user_id in pending_orders:

        if re.search(
            r"(不要|刪除|拿掉|移除)\s*\d{2,4}",
            text
        ):
            return remove_class_from_order(user_id, text)

        if re.search(
            r"(加|加入|增加)\s*\d{2,4}",
            text
        ):
            return add_class_to_order(user_id, text)

        if re.search(
            r"\d{2,4}\s*(?:改成|換成|改為)\s*\d{2,4}",
            text
        ):
            return replace_class_in_order(user_id, text)

        if re.search(
            r"\d{2,4}\s*(?:改成|改為|改|多|少)\s*\d+",
            text
        ):
            return adjust_pending_order(user_id, text)

        if (
            text.startswith("改成")
            or text.startswith("書名改成")
            or text.startswith("換成")
            or text.startswith("書改成")
        ):
            return change_book(user_id, text)

    # -----------------------------------------------------
    # 11. AI 草擬／整理訊息
    # 這類句子即使出現「王老師」「訂書」，也不要誤判成下單。
    # 若句子有訂單編號，例如「依照訂單001幫我寫一段話」，
    # 會先讀 Google 的真實訂單資料，再交給 AI 草擬。
    # -----------------------------------------------------
    if is_ai_writing_request(text):

        referenced_order_number = extract_referenced_order_number(text)

        if referenced_order_number:
            order = lookup_google_order(referenced_order_number)

            if not order:
                return f"⚠️ 查不到訂單 {referenced_order_number}。"

            historical_order_context[user_id] = order

            return ask_ai_with_order(
                user_id,
                user_text,
                order
            )

        return ask_ai(user_id, user_text)

    # -----------------------------------------------------
    # 12. 查老師教哪幾班
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

        return handle_teacher_lookup(user_id, text)

    # -----------------------------------------------------
    # 13. 延續剛剛老師直接訂
    # -----------------------------------------------------
    if text.startswith("訂") and "老師" not in text:

        context = conversation_context.get(user_id)

        if context:
            return create_order_from_context(
                user_id,
                text,
                context
            )

    # -----------------------------------------------------
    # 14. 完整一句話訂書
    # -----------------------------------------------------
    if "訂" in text and "老師" in text:

        return create_order_from_sentence(
            user_id,
            text
        )

    # -----------------------------------------------------
    # 15. 原本的查老師格式
    # -----------------------------------------------------
    if text.startswith("查老師"):

        return handle_form_teacher_lookup(
            user_id,
            user_text
        )

    # -----------------------------------------------------
    # 16. 不是訂書指令 → 交給 AI 一般問答
    # AI 只負責回答文字，不直接修改 Google 訂單。
    # -----------------------------------------------------
    return ask_ai(user_id, user_text)


# =========================================================
# 判斷是不是「請 AI 幫忙寫／改／整理文字」
# =========================================================
def is_ai_writing_request(text):

    writing_words = [
        "幫我寫",
        "幫我擬",
        "幫我打",
        "幫我整理",
        "幫我改寫",
        "幫我潤飾",
        "幫我回覆",
        "幫我回",
        "寫一段",
        "寫訊息",
        "寫給",
        "傳給",
        "怎麼跟",
        "怎麼回",
        "口氣",
        "正式一點",
        "輕鬆一點",
        "簡短一點"
    ]

    return any(
        word in text
        for word in writing_words
    )


# =========================================================
# 從一般句子抓「被引用的訂單編號」
# 例如：
# 依照訂單001幫我寫...
# 001幫我寫得簡短一點
# 根據001訂單...
# =========================================================
def extract_referenced_order_number(text):

    patterns = [
        r"訂單\s*(\d{1,})",
        r"依照\s*(\d{1,})\s*訂單",
        r"根據\s*(\d{1,})\s*訂單",
        r"^(\d{1,})\s*(?:幫我|請幫我|寫|改|整理)"
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            return normalize_order_number(
                match.group(1)
            )

    return None


# =========================================================
# 用 Google 真實訂單資料協助 AI 草擬訊息
# 只讀資料，不修改訂單。
# =========================================================
def ask_ai_with_order(
    user_id,
    user_text,
    order
):

    class_lines = []

    for item in order.get("classes", []):
        class_lines.append(
            f"{item['class_name']}："
            f"{int(item['students'])}本"
        )

    order_context = (
        "以下是 Google 試算表查到的真實訂單資料：\\n"
        f"訂單編號：{order.get('order_number', '')}\\n"
        f"老師：{order.get('teacher', '')}\\n"
        f"學校：{order.get('school', '')}\\n"
        f"書名：{order.get('book', '')}\\n"
        f"出版社：{order.get('publisher', '')}\\n"
        + "\\n".join(class_lines)
        + f"\\n總數量：{order.get('quantity', 0)}本\\n"
        f"狀態：{order.get('status', '')}"
    )

    return ask_ai(
        user_id,
        user_text,
        extra_context=order_context
    )


# =========================================================
# 一般 AI 問答
# =========================================================
def ask_ai(
    user_id,
    user_text,
    extra_context=""
):

    if not OPENAI_API_KEY:
        print("OpenAI API key missing")
        return AI_FALLBACK_MESSAGE

    history = ai_conversation_context.get(
        user_id,
        []
    )

    # 只保留最近幾輪，避免每次傳太多文字。
    recent_history = history[-8:]

    conversation_text = ""

    for item in recent_history:
        role_name = (
            "使用者"
            if item["role"] == "user"
            else "助理"
        )
        conversation_text += (
            f"{role_name}：{item['text']}\n"
        )

    if extra_context:
        conversation_text += (
            "\n【系統提供的訂單資料】\n"
            + extra_context
            + "\n【訂單資料結束】\n"
        )

    conversation_text += (
        f"使用者：{user_text}\n助理："
    )

    instructions = (
        "你是『大漢訂書小幫手』的工作助理。"
        "請使用繁體中文回答，口吻自然、簡潔、實用。"
        "你可以回答一般問題、整理文字、草擬訊息、"
        "計算與提供工作上的建議。"
        "你不能聲稱自己已經修改、建立或取消任何訂單，"
        "也不能聲稱已經修改 Google 試算表。"
        "如果系統提供了真實訂單資料，你可以引用那些資料，"
        "協助使用者草擬要傳給老師的 LINE 訊息、摘要或通知。"
        "不要自行捏造訂單中沒有的班級、數量、書名或處理進度。"
        "若使用者只說『正在處理中』，可以照此語意草擬，"
        "但不要擅自改成『已完成』『已出貨』或『已到貨』。"
        "訂單查詢、建立、修改與確認都由外層固定程式處理。"
        "如果使用者要求你直接更動訂單資料，"
        "請提醒他使用明確的訂書指令。"
        "回答適合直接顯示在 LINE，不要使用 Markdown 表格。"
    )

    url = "https://api.openai.com/v1/responses"

    headers = {
        "Authorization":
            "Bearer " + OPENAI_API_KEY,
        "Content-Type":
            "application/json"
    }

    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": conversation_text,
        "max_output_tokens": 600
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        print(
            "OpenAI status:",
            response.status_code
        )

        if response.status_code != 200:
            print(
                "OpenAI error:",
                response.text
            )
            return AI_FALLBACK_MESSAGE

        data = response.json()
        answer = extract_openai_text(data)

        if not answer:
            print("OpenAI returned empty answer")
            return AI_FALLBACK_MESSAGE

        # LINE 單則文字訊息上限很高，
        # 這裡仍限制長度，避免回答過長。
        answer = answer.strip()[:4500]

        history.append({
            "role": "user",
            "text": user_text
        })
        history.append({
            "role": "assistant",
            "text": answer
        })

        ai_conversation_context[user_id] = (
            history[-10:]
        )

        return answer

    except Exception as error:
        print(
            "OpenAI request error:",
            error
        )
        return AI_FALLBACK_MESSAGE


def extract_openai_text(data):

    # Responses API 的文字通常位於：
    # output[] -> content[] -> output_text -> text
    texts = []

    for item in data.get("output", []):
        for content in item.get("content", []):
            if (
                content.get("type")
                == "output_text"
                and content.get("text")
            ):
                texts.append(
                    content.get("text")
                )

    if texts:
        return "\n".join(texts)

    # 保留相容性：若 API 回傳頂層 output_text。
    if data.get("output_text"):
        return str(data.get("output_text"))

    return ""


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
# 歷史訂單：辨識查詢
# =========================================================
def extract_order_lookup_number(text):

    patterns = [
        r"^查\s*(\d{1,})$",
        r"^查\s*訂單\s*(\d{1,})$",
        r"^訂單\s*(\d{1,})$",
        r"^(\d{1,})\s*訂單(?:內容)?(?:是什麼|內容是什麼|呢|？|\?)?$",
        r"^查\s*(\d{1,})\s*訂單(?:內容)?$"
    ]

    for pattern in patterns:

        match = re.search(pattern, text)

        if match:
            return normalize_order_number(match.group(1))

    return None


# =========================================================
# 歷史訂單：直接指定訂單修改
# 001的701改28本
# =========================================================
def parse_direct_history_adjustment(text):

    # 必須明確寫出「的」，例如：
    # 001的705改35
    # 訂單001的705改35
    # 001的705改50本 701取消
    # 這樣單獨的 705改35 不會被誤認成訂單編號。
    match = re.search(
        r"^(?:訂單)?(\d{1,})\s*的\s*(.+)$",
        text
    )

    if not match:
        return None

    edit_text = match.group(2).strip()

    if not looks_like_history_edit(edit_text):
        return None

    return {
        "order_number": normalize_order_number(match.group(1)),
        "edit_text": edit_text
    }


# =========================================================
# 歷史訂單：判斷是否像修改指令
# 支援：
# 705改50
# 701取消
# 705改50本 701取消
# =========================================================
def looks_like_history_edit(text):

    has_quantity_edit = re.search(
        r"\d{2,4}\s*(?:改成|改為|改|多|少)\s*\d+\s*(?:人|本)?",
        text
    )

    has_remove_edit = re.search(
        r"\d{2,4}\s*(?:取消|不要|移除|刪除|拿掉)",
        text
    )

    return bool(
        has_quantity_edit
        or has_remove_edit
    )


# =========================================================
# 歷史訂單：準備修改，但先不寫 Google
# =========================================================
def prepare_history_adjustment(
    user_id,
    original_order,
    text
):

    order = copy_order(original_order)

    quantity_pattern = re.compile(
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

    remove_pattern = re.compile(
        r"(?<!\d)"
        r"(\d{2,4})"
        r"(?!\d)"
        r"\s*"
        r"(取消|不要|移除|刪除|拿掉)"
    )

    quantity_matches = list(
        quantity_pattern.finditer(text)
    )

    remove_matches = list(
        remove_pattern.finditer(text)
    )

    if not quantity_matches and not remove_matches:
        return "⚠️ 我看不懂要修改哪個班級。"

    changes = []

    # -----------------------------------------------------
    # 先處理數量修改
    # -----------------------------------------------------
    for match in quantity_matches:

        class_name = match.group(1)
        action = match.group(2)
        value = int(match.group(3))

        target = find_order_class(
            order,
            class_name
        )

        if not target:
            return (
                f"⚠️ 訂單 {order['order_number']} "
                f"裡沒有 {class_name}。"
            )

        old_value = int(
            target["students"]
        )

        if action in ["改", "改成", "改為"]:
            new_value = value

        elif action == "多":
            new_value = old_value + value

        else:
            new_value = old_value - value

        if new_value < 0:
            return "⚠️ 數量不能小於 0。"

        target["students"] = new_value

        changes.append(
            f"{class_name}：{old_value}→{new_value}本"
        )

    # -----------------------------------------------------
    # 再處理取消／移除班級
    # -----------------------------------------------------
    for match in remove_matches:

        class_name = match.group(1)

        target = find_order_class(
            order,
            class_name
        )

        if not target:
            return (
                f"⚠️ 訂單 {order['order_number']} "
                f"裡沒有 {class_name}。"
            )

        if len(order["classes"]) <= 1:
            return (
                "⚠️ 不能取消這張訂單最後一個班級。"
            )

        old_value = int(
            target["students"]
        )

        order["classes"] = [
            item
            for item in order["classes"]
            if str(item["class_name"])
            != str(class_name)
        ]

        changes.append(
            f"{class_name}：{old_value}本→取消"
        )

    refresh_order_total(order)

    modification_text = "；".join(changes)

    pending_history_updates[user_id] = {
        "order": order,
        "modification_text": modification_text
    }

    return make_history_update_confirmation(
        original_order,
        order,
        changes
    )


# =========================================================
# 歷史訂單：修改確認畫面
# =========================================================
def make_history_update_confirmation(
    original_order,
    new_order,
    changes
):

    class_lines = []

    for item in new_order["classes"]:

        class_lines.append(
            f"{item['class_name']}："
            f"{item['students']}本"
        )

    return (
        "🔄 訂單修改確認\n\n"
        f"訂單編號：{new_order['order_number']}\n"
        f"老師：{new_order['teacher']}\n"
        f"書名：{new_order['book']}\n\n"
        "修改內容：\n"
        + "\n".join(changes)
        + "\n\n修改後：\n"
        + "\n".join(class_lines)
        + f"\n\n原總數：{original_order['quantity']}本"
        + f"\n新總數：{new_order['quantity']}本\n\n"
        "如果正確，請回覆「確認」或「確認修改」\n"
        "不要修改請回覆「取消修改」"
    )


# =========================================================
# 歷史訂單：顯示查詢結果
# =========================================================
def make_historical_order_reply(order):

    class_lines = []

    for item in order.get("classes", []):

        class_lines.append(
            f"{item['class_name']}："
            f"{item['students']}本"
        )

    message = (
        "📋 歷史訂單\n\n"
        f"訂單編號：{order['order_number']}\n"
        f"老師：{order['teacher']}\n"
        f"學校：{order['school']}\n"
        f"書名：{order['book']}\n"
        f"出版社：{order['publisher']}\n\n"
        + "\n".join(class_lines)
        + f"\n\n總數量：{order['quantity']}本\n"
        + f"狀態：{order.get('status', '')}"
    )

    if order.get("order_time"):
        message += f"\n訂購時間：{order['order_time']}"

    if order.get("last_modified"):
        message += f"\n最後修改：{order['last_modified']}"

    if order.get("modification_log"):
        message += f"\n修改紀錄：{order['modification_log']}"

    message += (
        "\n\n如果要調整，可以直接說：\n"
        "701改28本"
    )

    return message


# =========================================================
# 歷史訂單：從 Google 查詢
# =========================================================
def lookup_google_order(order_number):

    if not GOOGLE_SCRIPT_URL:
        return None

    data = {
        "action": "lookup_order",
        "order_number": normalize_order_number(order_number)
    }

    try:

        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        result = response.json()

        if not result.get("success"):
            print("Lookup order error:", result)
            return None

        if not result.get("found"):
            return None

        order = result.get("order", {})

        order["order_number"] = normalize_order_number(
            order.get("order_number", order_number)
        )

        order["classes"] = copy_classes(
            order.get("classes", [])
        )

        order["quantity"] = calculate_total(
            order["classes"]
        )

        return order

    except Exception as error:

        print("Lookup order error:", error)
        return None


# =========================================================
# 歷史訂單：正式更新 Google
# =========================================================
def update_google_order(
    order,
    modification_text
):

    if not GOOGLE_SCRIPT_URL:
        return False, None

    data = {
        "action": "update_order",
        "order_number": order["order_number"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get("classes", []),
        "modification_text": modification_text
    }

    try:

        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        result = response.json()

        print("Update order status:", response.status_code)
        print("Update order response:", response.text)

        return (
            response.status_code == 200
            and result.get("success") is True,
            result
        )

    except Exception as error:

        print("Update order error:", error)
        return False, None


def normalize_order_number(value):

    digits = re.sub(
        r"\D",
        "",
        str(value or "")
    )

    if not digits:
        return ""

    return str(int(digits)).zfill(3)


def copy_order(order):

    result = dict(order)

    result["classes"] = copy_classes(
        order.get("classes", [])
    )

    result["quantity"] = calculate_total(
        result["classes"]
    )

    return result


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
        return False, None

    data = {
        "action": "create_order",
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get(
            "classes",
            []
        )
    }

    try:

        response = requests.post(
            GOOGLE_SCRIPT_URL,
            json=data,
            timeout=15
        )

        result = response.json()

        print(
            "Google Sheet status:",
            response.status_code
        )

        print(
            "Google Sheet response:",
            response.text
        )

        if (
            response.status_code == 200
            and result.get("success") is True
        ):
            return (
                True,
                result.get("order_number")
            )

        return False, None

    except Exception as error:

        print(
            "Google Sheet error:",
            error
        )

        return False, None


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
