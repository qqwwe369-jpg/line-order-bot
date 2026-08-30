from flask import Flask, request
import os
import re
import time
import requests
from datetime import datetime

app = Flask(__name__)

# =========================================================
# 環境變數
# =========================================================
CHANNEL_ACCESS_TOKEN = (
    os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    or os.environ.get("CHANNEL_ACCESS_TOKEN")
)
CHANNEL_SECRET = (
    os.environ.get("LINE_CHANNEL_SECRET")
    or os.environ.get("CHANNEL_SECRET")
)
GOOGLE_SCRIPT_URL = os.environ.get("GOOGLE_SCRIPT_URL")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.6-luna")
AI_FALLBACK_MESSAGE = "⚠️ 目前無法處理這段訊息，請換個說法再試一次。"

DEFAULT_SCHOOL = os.environ.get("DEFAULT_SCHOOL", "天母國中")

# =========================================================
# 對話狀態
# =========================================================
pending_orders = {}
order_flow_context = {}
conversation_context = {}
teacher_lookup_context = {}

historical_order_context = {}
pending_history_updates = {}
pending_history_cancels = {}

pending_other_orders = {}
pending_other_updates = {}
other_order_context = {}

ai_conversation_context = {}

# =========================================================
# Flask
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return "LINE Order Bot is running!"


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_json(silent=True) or {}

    print("Webhook received")
    print(body)

    for event in body.get("events", []):
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        if message.get("type") != "text":
            continue

        user_text = str(message.get("text", "")).strip()
        reply_token = event.get("replyToken")

        source = event.get("source", {})
        user_id = source.get("userId", "unknown")

        try:
            reply_message = handle_message(user_id, user_text)
        except Exception as error:
            print("handle_message error:", error)
            reply_message = "⚠️ 系統剛剛處理失敗，請再傳一次。"

        reply_to_line(reply_token, reply_message)

    return "OK", 200


# =========================================================
# 主流程
# =========================================================
def handle_message(user_id, user_text):
    text = normalize_text(user_text)

    # 0. 重來：一定最優先
    if text in ["重來", "重新開始", "全部重來", "全部重設"]:
        clear_all_user_state(user_id)
        return (
            "🔄 已重新開始\n\n"
            "目前的老師、訂書草稿、待確認訂單、歷史訂單修改狀態都已清除。\n\n"
            "你可以直接重新輸入，例如：\n"
            "701訂國一數學講義\n\n"
            "我會記住班級＋書名，再問你是哪一位老師。"
        )

    # 1. 固定功能選單
    if is_help_request(text):
        return get_help_reply()

    # 2. 取消修改
    if text in ["取消修改", "不要修改"]:
        if user_id in pending_other_updates:
            pending_other_updates.pop(user_id, None)
            return "❌ 已取消這次「其他訂單」修改，Google 資料沒有變動。"

        if user_id in pending_history_updates:
            pending_history_updates.pop(user_id, None)
            return "❌ 已取消這次歷史訂單修改，Google 原訂單沒有變動。"

        if user_id in pending_history_cancels:
            pending_history_cancels.pop(user_id, None)
            return "❌ 已取消這次歷史訂單取消動作，Google 原訂單沒有變動。"

        return "目前沒有等待確認的修改。"

    # 3. 確認各種待辦
    if text in ["確認取消", "確認"] and user_id in pending_history_cancels:
        return confirm_history_cancel(user_id)

    if text in ["確認修改", "確認"] and user_id in pending_other_updates:
        return confirm_other_order_update(user_id)

    if text in ["確認修改", "確認"] and user_id in pending_history_updates:
        return confirm_history_update(user_id)

    if text == "確認" and user_id in pending_other_orders:
        return confirm_other_order(user_id)

    if text == "確認" and user_id in pending_orders:
        return confirm_new_order(user_id)

    # 4. 取消目前新訂單／其他訂單
    if text in ["取消", "取消訂單", "不要了", "這筆不要"]:
        if user_id in pending_other_orders:
            pending_other_orders.pop(user_id, None)
            return "❌ 已取消這筆「其他訂單」，Google 沒有寫入。"

        if user_id in pending_orders:
            pending_orders.pop(user_id, None)
            return (
                "❌ 已取消這筆訂單。\n\n"
                "老師資料仍保留，你可以直接重新輸入班級＋書名。"
            )

        if user_id in order_flow_context:
            order_flow_context.pop(user_id, None)
            return "❌ 已取消這次訂書草稿。"

        return "目前沒有尚未確認的訂單。"

    # 5. 顯示目前訂單
    if text in ["目前訂單", "看訂單", "訂單內容", "現在訂單"]:
        order = pending_orders.get(user_id)
        if not order:
            return "目前沒有尚未確認的新訂單。"
        return make_order_confirmation(order)

    # 6. 待確認新訂單修改
    if user_id in pending_orders:
        pending_reply = handle_pending_order_edit(user_id, text)
        if pending_reply is not None:
            return pending_reply

    # 7. 查詢單日訂單（新功能）
    date_query = parse_daily_order_query(text)
    if date_query:
        orders = lookup_orders_by_date(date_query)
        if orders is None:
            return "⚠️ 單日訂單查詢失敗，請稍後再試。"
        if not orders:
            return f"📅 {date_query} 目前查不到訂書訂單。"
        return make_daily_orders_reply(date_query, orders)

    # 8. 直接取消指定歷史訂單（新功能）
    cancel_number = parse_history_cancel_request(text)
    if cancel_number:
        order = lookup_google_order(cancel_number)
        if not order:
            return f"⚠️ 查不到訂單 {cancel_number}。"

        historical_order_context[user_id] = order
        pending_history_cancels[user_id] = copy_order(order)
        return make_history_cancel_confirmation(order)

    # 9. 歷史訂單查詢
    order_number = extract_order_lookup_number(text)
    if order_number:
        order = lookup_google_order(order_number)
        if not order:
            return f"⚠️ 查不到訂單 {order_number}。"

        historical_order_context[user_id] = order
        pending_history_updates.pop(user_id, None)
        pending_history_cancels.pop(user_id, None)
        return make_historical_order_reply(order)

    # 10. 查完歷史訂單後直接說「取消這張」
    if user_id in historical_order_context and text in [
        "取消這張", "取消這筆", "取消這張訂單", "取消這筆訂單", "這張取消"
    ]:
        order = historical_order_context[user_id]
        pending_history_cancels[user_id] = copy_order(order)
        return make_history_cancel_confirmation(order)

    # 11. 直接指定歷史訂單修改：005的701改30
    direct_history = parse_direct_history_adjustment(text)
    if direct_history:
        order = lookup_google_order(direct_history["order_number"])
        if not order:
            return f"⚠️ 查不到訂單 {direct_history['order_number']}。"

        historical_order_context[user_id] = order
        return prepare_history_adjustment(
            user_id,
            order,
            direct_history["edit_text"]
        )

    # 12. 已查過歷史訂單後：701改28 / 703取消
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

    # 13. 老師訂書歷史／進度
    teacher_order_query = parse_teacher_book_order_query(text)
    if teacher_order_query:
        teacher = teacher_order_query["teacher"]
        orders = lookup_book_orders_by_teacher(teacher)

        if not orders:
            return (
                "⚠️ 查不到這位老師的訂書紀錄。\n\n"
                f"老師：{teacher}"
            )

        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]

        return make_teacher_book_orders_reply(teacher, orders)

    # 14. AI 寫訊息
    if is_ai_writing_request(text):
        ref_number = extract_referenced_order_number(text)

        if ref_number:
            order = lookup_google_order(ref_number)
            if not order:
                return f"⚠️ 查不到訂單 {ref_number}。"

            historical_order_context[user_id] = order
            return ask_ai_with_order(user_id, user_text, order)

        if user_id in historical_order_context:
            return ask_ai_with_order(
                user_id,
                user_text,
                historical_order_context[user_id]
            )

        return ask_ai(user_id, user_text)

    # 15. 學校教科書版本
    version_query = parse_school_version_query(user_id, text)
    if version_query:
        return handle_school_version_query(version_query)

    # 16. 學校／年級／班級學生人數
    stats_query = parse_school_stats_query(user_id, text)
    if stats_query:
        return handle_school_stats_query(stats_query)

    # 17. 老師資料庫
    if looks_like_teacher_lookup(text):
        return handle_teacher_lookup(user_id, text)

    if user_id in teacher_lookup_context and looks_like_teacher_followup(text):
        return handle_teacher_followup(user_id)

    # 18. 其他訂單
    other_query = parse_other_order_query(text)
    if other_query:
        orders = lookup_other_orders(
            teacher=other_query.get("teacher", ""),
            item_keyword=other_query.get("item_keyword", "")
        )

        if not orders:
            return "⚠️ 查不到符合條件的其他訂單。"

        if len(orders) == 1:
            other_order_context[user_id] = orders[0]
        else:
            other_order_context.pop(user_id, None)

        return make_other_orders_reply(orders)

    other_update = parse_other_order_update(user_id, text)
    if other_update:
        target = resolve_other_order_target(user_id, other_update)

        if isinstance(target, str):
            return target

        if not target:
            return "⚠️ 找不到要修改的其他訂單。"

        pending_other_updates[user_id] = {
            "row_number": target["row_number"],
            "teacher": target.get("teacher", ""),
            "field": other_update["field"],
            "value": other_update["value"]
        }

        return make_other_order_update_confirmation(
            target,
            other_update["field"],
            other_update["value"]
        )

    parsed_other = parse_other_order(user_id, text)
    if parsed_other:
        pending_other_orders[user_id] = parsed_other
        return make_other_order_confirmation(parsed_other)

    # 19. 訂書流程 —— 優先於一般 AI
    order_reply = handle_order_flow(user_id, text)
    if order_reply is not None:
        return order_reply

    # 20. 其他內容 → 一般 AI
    return ask_ai(user_id, user_text)


# =========================================================
# 狀態
# =========================================================
def clear_all_user_state(user_id):
    pending_orders.pop(user_id, None)
    order_flow_context.pop(user_id, None)
    conversation_context.pop(user_id, None)
    teacher_lookup_context.pop(user_id, None)
    historical_order_context.pop(user_id, None)
    pending_history_updates.pop(user_id, None)
    pending_history_cancels.pop(user_id, None)
    pending_other_orders.pop(user_id, None)
    pending_other_updates.pop(user_id, None)
    other_order_context.pop(user_id, None)
    ai_conversation_context.pop(user_id, None)


# =========================================================
# 功能選單
# =========================================================
def is_help_request(text):
    compact = re.sub(r"\s+", "", str(text or "").lower())
    phrases = {
        "功能", "功能介紹", "使用說明", "說明", "幫助", "help",
        "怎麼用", "如何使用", "你會什麼", "你可以幹嘛",
        "你可以做什麼", "你能幹嘛", "你能做什麼",
        "你能幫我做什麼", "可以幫我做什麼",
        "有什麼功能", "有哪些功能", "你有什麼功能",
        "你有哪些功能"
    }
    return compact in phrases


def get_help_reply():
    return (
        "📚 大漢訂書小幫手\n\n"
        "我可以幫你：\n"
        "📚 訂書\n"
        "📅 查詢單日訂單\n"
        "👨‍🏫 查老師歷史訂單\n"
        "✏️ 修改歷史訂單\n"
        "❌ 取消歷史訂單\n"
        "👥 查老師／班級人數\n"
        "📖 查學校教科書版本\n"
        "📦 其他訂單\n"
        "✍️ 整理／草擬 LINE 訊息\n\n"
        "💡 直接用平常講話告訴我就可以。"
    )


# =========================================================
# 訂書流程
# =========================================================
def handle_order_flow(user_id, text):
    clean = normalize_order_typo(text)

    start_phrases = [
        "訂書", "我要訂書", "我訂書", "開始訂書", "幫我訂書",
        "我要下單", "幫我下單", "要訂書"
    ]

    # 純粹啟動訂書流程：固定從老師開始引導
    if clean in start_phrases:
        order_flow_context[user_id] = {
            "teacher": "",
            "school": "",
            "classes": [],
            "book": ""
        }
        return make_order_guide_reply(order_flow_context[user_id])

    draft = order_flow_context.get(user_id, {
        "teacher": "",
        "school": "",
        "classes": [],
        "book": ""
    })

    parsed = parse_order_message(clean)

    if not parsed["has_order_intent"] and user_id not in order_flow_context:
        recent = conversation_context.get(user_id)
        if recent and looks_like_contextual_class_book(clean, recent):
            parsed = parse_contextual_class_book(clean, recent)
        else:
            return None

    if user_id in order_flow_context:
        parsed = merge_followup_into_parsed(clean, parsed, draft)

    if parsed.get("teacher"):
        draft["teacher"] = parsed["teacher"]

    if parsed.get("school"):
        draft["school"] = parsed["school"]

    if parsed.get("classes"):
        draft["classes"] = unique_list(parsed["classes"])

    if parsed.get("book"):
        draft["book"] = parsed["book"]

    # 查完老師資料後直接沿用老師與學校
    if not draft["teacher"]:
        recent = conversation_context.get(user_id, {})
        if recent.get("teacher") and recent.get("school"):
            draft["teacher"] = recent["teacher"]
            draft["school"] = recent["school"]

    if draft["teacher"] and not draft["school"]:
        for recent in [
            teacher_lookup_context.get(user_id, {}),
            conversation_context.get(user_id, {})
        ]:
            if recent.get("teacher") == draft["teacher"] and recent.get("school"):
                draft["school"] = recent["school"]
                break

        if not draft["school"]:
            draft["school"] = DEFAULT_SCHOOL

    order_flow_context[user_id] = draft

    # 老師＋書名已足夠：沒指定班級時，自動帶入該老師全部班級
    if draft["teacher"] and draft["book"]:
        result = build_order_from_draft(user_id, draft)
        if user_id in pending_orders:
            order_flow_context.pop(user_id, None)
        return result

    # 資料不足時只問缺少的欄位，回答格式固定
    return make_order_guide_reply(draft)


def make_order_guide_reply(draft):
    lines = ["📚 訂書", ""]

    if draft.get("teacher"):
        lines.append(f"老師：{draft['teacher']}")
    if draft.get("classes"):
        lines.append(f"班級：{'、'.join(draft['classes'])}")
    if draft.get("book"):
        lines.append(f"書名：{draft['book']}")

    if len(lines) > 2:
        lines.append("")

    if not draft.get("teacher"):
        lines.append("請告訴我是哪一位老師？")
    elif not draft.get("book"):
        lines.append("請告訴我要訂哪一本書？")
    else:
        lines.append("請告訴我要訂哪些班級？")

    return "\n".join(lines)


def parse_order_message(text):
    clean = str(text or "").strip()
    teacher, school = extract_teacher_and_school(clean)
    classes = extract_classes(clean)

    order_words = [
        "訂書", "要訂", "想訂", "幫我訂", "我要訂", "訂講義",
        "訂評量", "訂教材", "下單", "幫我下單"
    ]
    book_words = [
        "講義", "評量", "教材", "複習", "測驗", "題本",
        "自修", "課本", "習作", "學習單"
    ]

    has_order_word = (
        any(word in clean for word in order_words)
        or bool(re.search(r"訂(?!單|書進度|書紀錄)", clean))
    )
    oral_order = (
        bool(teacher or classes)
        and "要" in clean
        and any(word in clean for word in book_words)
    )

    has_order_intent = has_order_word or oral_order
    book = extract_book_candidate(clean, teacher, classes) if has_order_intent else ""

    return {
        "has_order_intent": has_order_intent,
        "teacher": teacher,
        "school": school,
        "classes": classes,
        "book": book
    }


def extract_book_candidate(text, teacher="", classes=None):
    clean = str(text or "").strip()
    classes = classes or []

    # 「王老師要訂書」中的「書」不是書名
    if re.fullmatch(
        r".*(?:老師)?(?:要|想要|準備)?(?:幫我)?(?:訂書|下單)[。！!？?]?",
        clean
    ):
        return ""

    candidate = clean

    if teacher:
        candidate = candidate.replace(teacher, " ")

    for class_name in classes:
        candidate = re.sub(
            r"(?<!\d)" + re.escape(class_name) + r"(?!\d)",
            " ",
            candidate
        )

    # 移除常見口語與訂書動詞，只留下可能的書名
    candidate = re.sub(
        r"(?:麻煩|請|幫我|幫忙|我要|我想要|想要|想訂|要訂|訂購|訂|下單|要|需要|那邊|這邊|的)",
        " ",
        candidate
    )
    candidate = re.sub(r"[跟和與、,，：:。.!！?？\s]+", " ", candidate).strip()
    candidate = clean_book_name(candidate)

    if candidate in ["", "書", "訂書"]:
        return ""
    return candidate


def merge_followup_into_parsed(clean, parsed, draft):
    result = dict(parsed)

    if not result.get("teacher"):
        teacher, school = extract_teacher_and_school(clean)
        if teacher:
            result["teacher"] = teacher
            result["school"] = school

    classes = extract_classes(clean)
    if classes and not result.get("classes"):
        result["classes"] = classes

    # 對話進行中，只要書名還缺，就允許直接口語補書名
    if not draft.get("book") and not result.get("book"):
        teacher_in_text = result.get("teacher", "")
        classes_in_text = result.get("classes", [])
        candidate = extract_book_candidate(
            clean,
            teacher_in_text,
            classes_in_text
        )

        # 單獨回「王老師」或只有班級號碼時，不誤判成書名
        if candidate and "老師" not in candidate and not re.fullmatch(
            r"[\d、,，跟和與\s]+", candidate
        ):
            result["book"] = candidate

    result["has_order_intent"] = True
    return result


def build_order_from_draft(user_id, draft):
    teacher = draft["teacher"]
    school = draft.get("school") or DEFAULT_SCHOOL
    requested_classes = unique_list(draft.get("classes", []))
    book = clean_book_name(draft["book"])

    teacher_classes = get_teacher_classes(school, teacher)

    if not teacher_classes:
        return (
            "⚠️ 查不到老師班級資料\n\n"
            f"學校：{school}\n"
            f"老師：{teacher}\n\n"
            "請確認學校或老師名稱。"
        )

    # 未指定班級時，預設帶入老師資料庫中的全部班級，先讓使用者確認。
    if not requested_classes:
        requested_classes = [
            str(item.get("class_name", ""))
            for item in teacher_classes
            if str(item.get("class_name", ""))
        ]

    selected = []

    for class_name in requested_classes:
        found = next(
            (
                item for item in teacher_classes
                if str(item.get("class_name")) == str(class_name)
            ),
            None
        )

        if not found:
            available = "、".join(
                str(item.get("class_name"))
                for item in teacher_classes
            )
            return (
                f"⚠️ {teacher} 的資料裡找不到 {class_name} 班。\n\n"
                f"目前班級：{available}"
            )

        selected.append({
            "class_name": str(found["class_name"]),
            "students": int(found["students"])
        })

    publisher = get_book_publisher(book)

    if not publisher:
        return (
            "⚠️ 查不到書籍資料\n\n"
            f"書名：{book}\n\n"
            "請確認 Google「書籍資料」是否已建立這本書與出版社。"
        )

    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "publisher": publisher,
        "classes": selected,
        "quantity": calculate_total(selected)
    }

    pending_orders[user_id] = order

    context = {
        "teacher": teacher,
        "school": school,
        "classes": copy_classes(teacher_classes)
    }
    conversation_context[user_id] = context
    teacher_lookup_context[user_id] = context

    return make_order_confirmation(order)


def normalize_order_typo(text):
    clean = str(text or "").strip()

    clean = re.sub(
        r"(?<=\d)\s*定\s*(?=[^\d])",
        "訂",
        clean,
        count=1
    )

    clean = re.sub(
        r"^(我要|我想要|想要)?\s*定書$",
        lambda m: (m.group(1) or "") + "訂書",
        clean
    )

    return clean


def looks_like_contextual_class_book(text, context):
    known = {
        str(item.get("class_name"))
        for item in context.get("classes", [])
    }

    mentioned = extract_classes(text)
    if not mentioned:
        return False
    if not all(item in known for item in mentioned):
        return False

    remainder = text
    for class_name in mentioned:
        remainder = re.sub(
            r"(?<!\d)" + re.escape(class_name) + r"(?!\d)",
            " ",
            remainder
        )

    remainder = re.sub(r"[跟和與、,，/\s]+", " ", remainder).strip()
    return len(remainder) >= 2


def parse_contextual_class_book(text, context):
    clean = normalize_order_typo(text)
    classes = extract_classes(clean)

    if "訂" in clean:
        book = clean.split("訂", 1)[1].strip()
    else:
        book = clean
        for class_name in classes:
            book = re.sub(
                r"(?<!\d)" + re.escape(class_name) + r"(?!\d)",
                " ",
                book
            )

    book = re.sub(r"^[跟和與、,，/\s]+", "", book)
    book = clean_book_name(book)

    return {
        "has_order_intent": True,
        "teacher": context.get("teacher", ""),
        "school": context.get("school", ""),
        "classes": classes,
        "book": book
    }


def extract_teacher_and_school(text):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))

    m = re.search(
        r"([\u4e00-\u9fff]{2,16}(?:國中|高中|國小))"
        r"([\u4e00-\u9fff]{1,4})老師",
        clean
    )
    if m:
        return m.group(2) + "老師", m.group(1)

    m = re.fullmatch(
        r"([\u4e00-\u9fff]{2,8})([\u4e00-\u9fff])老師",
        clean
    )
    if m:
        return m.group(2) + "老師", m.group(1) + "國中"

    m = re.search(r"([\u4e00-\u9fff]{1,4})老師", clean)
    if m:
        name = m.group(1)
        if name in ["哪位", "這位", "那位", "一位", "我的", "我們"]:
            return "", ""
        return name + "老師", ""

    return "", ""


def extract_classes(text):
    matches = re.findall(r"(?<!\d)([789]\d{2})(?!\d)", str(text or ""))
    return unique_list(matches)


def unique_list(items):
    result = []
    for item in items:
        value = str(item)
        if value not in result:
            result.append(value)
    return result


# =========================================================
# 新訂單確認／修改
# =========================================================
def make_order_confirmation(order):
    class_lines = [
        f"{item['class_name']}：{int(item['students'])}本"
        for item in order.get("classes", [])
    ]

    return (
        "📚 訂購確認\n\n"
        f"老師：{order['teacher']}\n"
        f"學校：{order['school']}\n"
        f"書名：{order['book']}\n"
        f"出版社：{order['publisher']}\n\n"
        + "\n".join(class_lines)
        + f"\n\n總數量：{int(order.get('quantity', 0))}本\n\n"
        "確認無誤請回覆「確認」。\n"
        "若班級不對，直接告訴我要保留、增加或取消哪些班級。"
    )


def confirm_new_order(user_id):
    order = pending_orders.get(user_id)
    if not order:
        return "⚠️ 找不到尚未確認的訂單，請重新輸入。"

    success, order_number = write_to_google_sheet(order)
    if not success:
        return "❌ 訂單寫入失敗，請稍後再試。"

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


def handle_pending_order_edit(user_id, text):
    order = pending_orders[user_id]

    # 「只要701跟703」「保留701、703」：直接縮成指定班級
    if any(word in text for word in ["只要", "保留", "就要", "只留"]):
        wanted = extract_classes(text)
        if wanted:
            context = conversation_context.get(user_id, {})
            available = {
                str(item.get("class_name")): item
                for item in context.get("classes", [])
            }
            missing = [name for name in wanted if name not in available]
            if missing:
                return (
                    "⚠️ 找不到班級資料\n\n"
                    + "、".join(missing)
                )

            order["classes"] = [
                {
                    "class_name": name,
                    "students": int(available[name].get("students", 0))
                }
                for name in wanted
            ]
            refresh_order_total(order)
            return make_order_confirmation(order)

    m = re.fullmatch(r"(?:不要|刪除|刪掉|拿掉|移除)\s*(\d{3})", text)
    if not m:
        m = re.fullmatch(r"(\d{3})\s*(?:取消|不要|刪除|刪掉|拿掉|移除)", text)

    if m:
        class_name = m.group(1)
        if not find_order_class(order, class_name):
            return f"⚠️ 目前訂單裡沒有 {class_name}。"
        if len(order["classes"]) <= 1:
            return (
                "⚠️ 目前只剩最後一個班級。\n"
                "如果要取消整張訂單，請直接輸入「取消」。"
            )
        order["classes"] = [
            item for item in order["classes"]
            if str(item["class_name"]) != class_name
        ]
        refresh_order_total(order)
        return make_order_confirmation(order)

    m = re.fullmatch(r"(?:加|加入|增加)\s*(\d{3})", text)
    if m:
        class_name = m.group(1)
        if find_order_class(order, class_name):
            return f"⚠️ {class_name} 已經在這筆訂單裡了。"

        context = conversation_context.get(user_id, {})
        source = next(
            (
                item for item in context.get("classes", [])
                if str(item["class_name"]) == class_name
            ),
            None
        )
        if not source:
            return f"⚠️ {order['teacher']} 沒有 {class_name} 這個班。"

        order["classes"].append({
            "class_name": class_name,
            "students": int(source["students"])
        })
        refresh_order_total(order)
        return make_order_confirmation(order)

    m = re.fullmatch(r"(\d{3})\s*(?:改成|改為|換成)\s*(\d{3})", text)
    if m:
        old_class, new_class = m.groups()
        old_target = find_order_class(order, old_class)
        if not old_target:
            return f"⚠️ {old_class} 目前不在這筆訂單裡。"

        context = conversation_context.get(user_id, {})
        source = next(
            (
                item for item in context.get("classes", [])
                if str(item["class_name"]) == new_class
            ),
            None
        )
        if not source:
            return f"⚠️ {order['teacher']} 沒有 {new_class} 這個班。"
        if find_order_class(order, new_class):
            return f"⚠️ {new_class} 已經在這筆訂單裡。"

        order["classes"] = [
            item for item in order["classes"]
            if str(item["class_name"]) != old_class
        ]
        order["classes"].append({
            "class_name": new_class,
            "students": int(source["students"])
        })
        refresh_order_total(order)
        return make_order_confirmation(order)

    matches = list(re.finditer(
        r"(?<!\d)(\d{3})(?!\d)\s*"
        r"(改成|改為|改|多|少)\s*"
        r"(\d+)\s*(?:人|本)?",
        text
    ))

    if matches:
        changes = []
        for match in matches:
            class_name, action, raw_value = match.groups()
            target = find_order_class(order, class_name)
            if not target:
                return f"⚠️ 目前訂單裡沒有 {class_name}。"

            old_value = int(target["students"])
            value = int(raw_value)

            if action in ["改", "改成", "改為"]:
                new_value = value
            elif action == "多":
                new_value = old_value + value
            else:
                new_value = old_value - value

            if new_value < 0:
                return "⚠️ 數量不能小於 0。"

            target["students"] = new_value
            changes.append(f"{class_name}：{old_value}→{new_value}本")

        refresh_order_total(order)
        return (
            "✅ 已調整\n\n"
            + "\n".join(changes)
            + "\n\n"
            + make_order_confirmation(order)
        )

    m = re.fullmatch(r"(?:書名)?(?:改成|改為|換成|書改成)\s*(.+)", text)
    if m:
        new_book = clean_book_name(m.group(1))
        publisher = get_book_publisher(new_book)

        if not publisher:
            return "⚠️ 查不到這本書\n\n" f"書名：{new_book}"

        order["book"] = new_book
        order["publisher"] = publisher
        return make_order_confirmation(order)

    return None


# =========================================================
# 老師資料庫
# =========================================================
def looks_like_teacher_lookup(text):
    if "老師" not in text:
        return False
    if "訂單" in text or "訂書進度" in text or is_ai_writing_request(text):
        return False

    teacher, _ = extract_teacher_and_school(text)
    if not teacher:
        return False

    words = [
        "教幾個班", "教幾班", "教哪幾班", "教哪些班",
        "有幾個班", "有哪些班", "哪幾班", "哪幾個班",
        "班級資料", "班級人數", "每班幾人", "每班人數",
        "學生人數", "總人數", "幾個學生", "多少學生"
    ]
    return any(word in text for word in words)


def looks_like_teacher_followup(text):
    words = [
        "總共幾個班", "幾個班", "總人數多少", "總人數",
        "總共幾人", "總共多少人", "每班幾人", "每班人數",
        "班級人數", "有哪些班", "哪幾班"
    ]
    return any(word in text for word in words)


def handle_teacher_lookup(user_id, text):
    teacher, school = extract_teacher_and_school(text)
    school = school or DEFAULT_SCHOOL

    if not teacher:
        return "⚠️ 找不到老師姓名。"

    classes = get_teacher_classes(school, teacher)
    if not classes:
        return (
            "⚠️ 查不到老師資料\n\n"
            f"學校：{school}\n"
            f"老師：{teacher}"
        )

    context = {
        "school": school,
        "teacher": teacher,
        "classes": copy_classes(classes)
    }

    teacher_lookup_context[user_id] = context
    conversation_context[user_id] = context

    return make_teacher_reply(school, teacher, classes)


def handle_teacher_followup(user_id):
    context = teacher_lookup_context.get(user_id)
    if not context:
        return None

    classes = get_teacher_classes(context["school"], context["teacher"])
    if not classes:
        return "⚠️ 老師資料庫暫時查詢失敗。"

    context = {
        "school": context["school"],
        "teacher": context["teacher"],
        "classes": copy_classes(classes)
    }

    teacher_lookup_context[user_id] = context
    conversation_context[user_id] = context

    return make_teacher_reply(
        context["school"],
        context["teacher"],
        classes
    )


def make_teacher_reply(school, teacher, classes):
    total = calculate_total(classes)
    lines = [
        f"• {item['class_name']}班：{int(item['students'])}人"
        for item in classes
    ]

    return (
        ""
        "👨‍🏫 老師資料庫\n"
        f"學校：{school}\n"
        f"老師：{teacher}\n\n"
        f"📚 班級總數：{len(classes)}個班\n\n"
        "各班人數：\n"
        + "\n".join(lines)
        + f"\n\n👥 總學生人數：{total}人\n\n"
        "以上是目前 Google「老師班級資料」中的完整資料。"
    )


# =========================================================
# 歷史訂單
# =========================================================
def extract_order_lookup_number(text):
    patterns = [
        r"^查\s*(\d+)$",
        r"^查詢\s*(\d+)$",
        r"^查\s*訂單\s*(\d+)$",
        r"^查詢\s*訂單\s*(\d+)$",
        r"^查訂單\s*(\d+)$",
        r"^訂單\s*(\d+)$",
        r"^(\d+)\s*訂單(?:內容)?(?:是什麼|內容是什麼|呢|？|\?)?$"
    ]

    for pattern in patterns:
        m = re.fullmatch(pattern, text.strip())
        if m:
            return normalize_order_number(m.group(1))
    return None


def parse_direct_history_adjustment(text):
    m = re.fullmatch(r"(?:訂單)?(\d+)\s*的\s*(.+)", text.strip())
    if not m:
        return None
    if not looks_like_history_edit(m.group(2)):
        return None

    return {
        "order_number": normalize_order_number(m.group(1)),
        "edit_text": m.group(2).strip()
    }


def looks_like_history_edit(text):
    quantity = re.search(
        r"\d{3}\s*(?:改成|改為|改|多|少)\s*\d+\s*(?:人|本)?",
        text
    )
    remove = re.search(
        r"\d{3}\s*(?:取消|不要|移除|刪除|拿掉)",
        text
    )
    return bool(quantity or remove)


def prepare_history_adjustment(user_id, original_order, text):
    if str(original_order.get("status", "")).strip() == "已取消":
        pending_history_updates.pop(user_id, None)
        return (
            "⚠️ 此訂單已取消，無法修改。\n\n"
            f"訂單編號：{original_order.get('order_number', '')}"
        )

    order = copy_order(original_order)
    changes = []

    quantity_matches = list(re.finditer(
        r"(?<!\d)(\d{3})(?!\d)\s*"
        r"(改成|改為|改|多|少)\s*"
        r"(\d+)\s*(?:人|本)?",
        text
    ))

    remove_matches = list(re.finditer(
        r"(?<!\d)(\d{3})(?!\d)\s*"
        r"(?:取消|不要|移除|刪除|拿掉)",
        text
    ))

    if not quantity_matches and not remove_matches:
        return "⚠️ 我看不懂要修改哪個班級。"

    for match in quantity_matches:
        class_name, action, raw_value = match.groups()
        target = find_order_class(order, class_name)
        if not target:
            return f"⚠️ 訂單 {order['order_number']} 裡沒有 {class_name}。"

        old_value = int(target["students"])
        value = int(raw_value)

        if action in ["改", "改成", "改為"]:
            new_value = value
        elif action == "多":
            new_value = old_value + value
        else:
            new_value = old_value - value

        if new_value < 0:
            return "⚠️ 數量不能小於 0。"

        target["students"] = new_value
        changes.append(f"{class_name}：{old_value}→{new_value}本")

    for match in remove_matches:
        class_name = match.group(1)
        target = find_order_class(order, class_name)
        if not target:
            return f"⚠️ 訂單 {order['order_number']} 裡沒有 {class_name}。"
        if len(order["classes"]) <= 1:
            return "⚠️ 不能取消這張訂單最後一個班級。"

        old_value = int(target["students"])
        order["classes"] = [
            item for item in order["classes"]
            if str(item["class_name"]) != class_name
        ]
        changes.append(f"{class_name}：{old_value}本→取消")

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


def confirm_history_update(user_id):
    update_data = pending_history_updates.get(user_id)
    if not update_data:
        return "⚠️ 找不到等待確認的歷史訂單修改。"

    order_number = update_data["order"].get("order_number", "")
    latest = lookup_google_order(order_number)
    if latest and str(latest.get("status", "")).strip() == "已取消":
        pending_history_updates.pop(user_id, None)
        historical_order_context[user_id] = latest
        return (
            "⚠️ 此訂單已取消，無法修改。\n\n"
            f"訂單編號：{order_number}"
        )

    success, _ = update_google_order(
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
        "Google 試算表已更新。"
    )


def make_history_update_confirmation(original_order, new_order, changes):
    lines = [
        f"{item['class_name']}：{int(item['students'])}本"
        for item in new_order.get("classes", [])
    ]

    return (
        "🔄 訂單修改確認\n\n"
        f"訂單編號：{new_order['order_number']}\n"
        f"老師：{new_order['teacher']}\n"
        f"書名：{new_order['book']}\n\n"
        "修改內容：\n"
        + "\n".join(changes)
        + "\n\n修改後：\n"
        + "\n".join(lines)
        + f"\n\n原總數：{original_order['quantity']}本"
        + f"\n新總數：{new_order['quantity']}本\n\n"
        "如果正確，請回覆「確認」或「確認修改」\n"
        "不要修改請回覆「取消修改」"
    )


def make_historical_order_reply(order):
    lines = [
        f"{item['class_name']}：{int(item['students'])}本"
        for item in order.get("classes", [])
    ]

    message = (
        "📋 歷史訂單\n\n"
        f"訂單編號：{order['order_number']}\n"
        f"老師：{order.get('teacher', '')}\n"
        f"學校：{order.get('school', '')}\n"
        f"書名：{order.get('book', '')}\n"
        f"出版社：{order.get('publisher', '')}\n\n"
        + "\n".join(lines)
        + f"\n\n總數量：{int(order.get('quantity', 0))}本\n"
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
        "701改28本\n"
        "如果整張不要，可以說：取消這張"
    )
    return message


def parse_teacher_book_order_query(text):
    patterns = [
        r"^(?:查|查詢)\s*([\u4e00-\u9fff]{1,4}老師)\s*訂書進度$",
        r"^(?:查|查詢)\s*([\u4e00-\u9fff]{1,4}老師)\s*訂書訂單$",
        r"^(?:查|查詢)\s*([\u4e00-\u9fff]{1,4}老師)\s*訂書紀錄$",
        r"^(?:查|查詢)\s*([\u4e00-\u9fff]{1,4}老師)\s*歷史訂單$",
        r"^(?:查|查詢)\s*([\u4e00-\u9fff]{1,4}老師)\s*歷史訂書$",
        r"^([\u4e00-\u9fff]{1,4}老師)\s*訂書進度$",
        r"^([\u4e00-\u9fff]{1,4}老師)\s*訂書紀錄$"
    ]

    for pattern in patterns:
        m = re.fullmatch(pattern, text.strip())
        if m:
            return {"teacher": m.group(1)}
    return None


def make_teacher_book_orders_reply(teacher, orders):
    lines = [
        "📚 老師歷史訂單",
        "",
        f"老師：{teacher}",
        f"共找到 {len(orders)} 張訂書訂單",
        ""
    ]

    for order in orders[:10]:
        lines.extend([
            f"📘 訂單 {order.get('order_number', '')}",
            f"學校：{order.get('school', '')}",
            f"書名：{order.get('book', '')}",
            f"出版社：{order.get('publisher', '')}"
        ])

        for item in order.get("classes", []):
            lines.append(
                f"• {item['class_name']}班：{int(item['students'])}本"
            )

        lines.extend([
            f"總數量：{int(order.get('quantity', 0))}本",
            f"狀態：{order.get('status', '')}",
            ""
        ])

    lines.append("要看某一張詳細內容，可以直接說「查002」。")
    return "\n".join(lines)


# =========================================================
# 查詢單日訂單（新功能）
# =========================================================
def parse_daily_order_query(text):
    clean = re.sub(r"\s+", "", text.strip())

    if clean in ["查今天訂單", "今天訂單", "查今日訂單", "今日訂單"]:
        return datetime.now().strftime("%Y-%m-%d")

    if clean in ["查昨天訂單", "昨天訂單"]:
        from datetime import timedelta
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    # 0829訂單 / 查0829訂單
    m = re.fullmatch(r"(?:查|查詢)?(\d{2})(\d{2})(?:的)?訂單", clean)
    if m:
        year = datetime.now().year
        month = int(m.group(1))
        day = int(m.group(2))
        try:
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # 8/29、2026/8/29、8-29
    m = re.fullmatch(
        r"(?:查|查詢)?(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:的)?訂單",
        clean
    )
    if not m:
        return None

    year = int(m.group(1)) if m.group(1) else datetime.now().year
    month = int(m.group(2))
    day = int(m.group(3))

    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None


def make_daily_orders_reply(date_text, orders):
    total_quantity = sum(int(o.get("quantity", 0) or 0) for o in orders)

    lines = [
        f"📅 {date_text} 訂單",
        "",
        f"共 {len(orders)} 張｜合計 {total_quantity}本",
        ""
    ]

    for order in orders[:20]:
        lines.extend([
            f"📘 訂單 {order.get('order_number', '')}",
            f"老師：{order.get('teacher', '')}",
            f"書名：{order.get('book', '')}"
        ])

        for item in order.get("classes", []):
            lines.append(
                f"{item.get('class_name', '')}：{int(item.get('students', 0) or 0)}本"
            )

        lines.extend([
            f"小計：{int(order.get('quantity', 0) or 0)}本",
            f"狀態：{order.get('status', '')}",
            ""
        ])

    if len(orders) > 20:
        lines.append(f"另有 {len(orders) - 20} 張未顯示。")

    return "\n".join(lines).rstrip()


# =========================================================
# 取消歷史訂單（新功能）
# =========================================================
def parse_history_cancel_request(text):
    patterns = [
        r"^取消\s*訂單\s*(\d+)$",
        r"^訂單\s*(\d+)\s*取消$",
        r"^取消\s*(\d+)$"
    ]
    for pattern in patterns:
        m = re.fullmatch(pattern, text.strip())
        if m:
            return normalize_order_number(m.group(1))
    return None


def make_history_cancel_confirmation(order):
    return (
        "⚠️ 歷史訂單取消確認\n\n"
        f"訂單編號：{order.get('order_number', '')}\n"
        f"老師：{order.get('teacher', '')}\n"
        f"書名：{order.get('book', '')}\n"
        f"總數量：{int(order.get('quantity', 0) or 0)}本\n\n"
        "如果確定整張取消，請回覆「確認取消」\n"
        "不要取消請回覆「取消修改」"
    )


def confirm_history_cancel(user_id):
    order = pending_history_cancels.get(user_id)
    if not order:
        return "⚠️ 找不到等待確認取消的歷史訂單。"

    success, result = cancel_google_order(order["order_number"])
    if not success:
        return "❌ 訂單取消失敗，Google 原訂單沒有變動。"

    order_number = order["order_number"]
    pending_history_cancels.pop(user_id, None)

    refreshed = lookup_google_order(order_number)
    if refreshed:
        historical_order_context[user_id] = refreshed

    return (
        "✅ 歷史訂單已取消\n\n"
        f"訂單編號：{order_number}\n"
        "Google 試算表已更新為取消狀態。"
    )


# =========================================================
# 學校學生資料
# =========================================================
def parse_school_stats_query(user_id, text):
    clean = text.strip()

    query_words = [
        "多少人", "幾人", "幾個人", "學生人數", "總人數",
        "幾個班", "多少班", "有哪些班", "哪幾班", "哪幾個班"
    ]
    if not any(word in clean for word in query_words):
        return None
    if "老師" in clean:
        return None

    school = extract_school_name(clean) or get_context_school(user_id)
    if not school:
        return None

    class_match = re.search(r"(?<!\d)([789]\d{2})(?!\d)", clean)
    class_name = class_match.group(1) if class_match else ""
    grade = extract_grade_text(clean)

    intent = "summary"
    if any(word in clean for word in ["有哪些班", "哪幾班", "哪幾個班"]):
        intent = "classes"
    elif any(word in clean for word in ["幾個班", "多少班"]):
        intent = "class_count"
    elif any(word in clean for word in ["多少人", "幾人", "幾個人", "學生人數", "總人數"]):
        intent = "students"

    return {
        "school": school,
        "grade": grade,
        "class_name": class_name,
        "intent": intent
    }


def handle_school_stats_query(query):
    result = lookup_school_classes(
        query["school"],
        query.get("grade", ""),
        query.get("class_name", "")
    )

    if result is None:
        return "⚠️ 學校資料庫暫時查詢失敗，請稍後再試。"

    classes = result["classes"]
    if not classes:
        return "⚠️ 查不到學生資料。"

    if query.get("class_name"):
        item = classes[0]
        return (
            ""
            "🏫 班級資料\n"
            f"學校：{query['school']}\n"
            f"班級：{item['class_name']}班\n"
            f"學生人數：{item['students']}人"
        )

    title = query["school"]
    if query.get("grade"):
        title += f" {query['grade']}"

    class_lines = [
        f"• {item['class_name']}班：{item['students']}人"
        for item in classes
    ]

    return (
        ""
        f"🏫 {title}\n"
        f"班級總數：{result['class_count']}個班\n"
        f"學生總人數：{result['total_students']}人\n\n"
        "各班人數：\n"
        + "\n".join(class_lines)
    )


# =========================================================
# 教科書版本
# =========================================================
def parse_school_version_query(user_id, text):
    clean = text.strip()

    if not any(
        word in clean
        for word in ["版本", "哪一版", "哪個版本", "什麼版本", "教科書"]
    ):
        return None

    school = extract_school_name(clean) or get_context_school(user_id)
    if not school:
        return None

    grade = extract_grade_text(clean)

    subjects = [
        "國文", "英文", "數學", "自然", "生物", "理化",
        "地科", "社會", "歷史", "地理", "公民"
    ]
    subject = next((s for s in subjects if s in clean), "")

    period_match = re.search(r"(?<!\d)(\d{2,3}\s*(?:上|下))(?!\d)", clean)
    academic_period = ""
    if period_match:
        academic_period = re.sub(r"\s+", "", period_match.group(1))

    return {
        "school": school,
        "grade": grade,
        "subject": subject,
        "academic_period": academic_period
    }


def handle_school_version_query(query):
    result = lookup_school_versions(
        query["school"],
        query.get("grade", ""),
        query.get("subject", ""),
        query.get("academic_period", "")
    )

    if result is None:
        return "⚠️ 教科書版本資料庫暫時查詢失敗，請稍後再試。"

    versions = result["versions"]
    if not versions:
        return "⚠️ 查不到教科書版本資料。"

    if len(versions) == 1:
        item = versions[0]
        return (
            ""
            "📚 教科書版本\n"
            f"學校：{item['school']}\n"
            f"年級：{item['grade']}\n"
            f"科目：{item['subject']}\n"
            f"版本：{item['version']}"
            + (
                f"\n學年度：{item['academic_period']}"
                if item.get("academic_period") else ""
            )
        )

    lines = []
    for item in versions:
        prefix = ""
        if not query.get("grade") and item.get("grade"):
            prefix = f"{item['grade']} "
        lines.append(f"• {prefix}{item['subject']}：{item['version']}")

    return (
        ""
        f"📚 {query['school']}"
        + (f" {query['grade']}" if query.get("grade") else "")
        + "\n\n"
        + "\n".join(lines)
    )


def extract_school_name(text):
    m = re.search(
        r"([\u4e00-\u9fff]{2,16}(?:國中|高中|國小))",
        text
    )
    return m.group(1).strip() if m else ""


def extract_grade_text(text):
    grade_map = {
        "七年級": "七年級", "八年級": "八年級", "九年級": "九年級",
        "國一": "七年級", "國二": "八年級", "國三": "九年級",
        "7年級": "七年級", "8年級": "八年級", "9年級": "九年級"
    }
    for key, value in grade_map.items():
        if key in text:
            return value
    return ""


def get_context_school(user_id):
    for source in [
        teacher_lookup_context.get(user_id, {}),
        conversation_context.get(user_id, {})
    ]:
        if source.get("school"):
            return source["school"]
    return ""


# =========================================================
# 其他訂單
# =========================================================
def parse_other_order(user_id, text):
    clean = text.strip()

    m = re.fullmatch(
        r"(.+?(?:國中|高中|國小))"
        r"([\u4e00-\u9fff]{1,4})老師"
        r"\s*(?:買|購買|要買)\s*(.+)",
        clean
    )

    if m:
        school = m.group(1).strip()
        teacher = m.group(2).strip() + "老師"
        item = m.group(3).strip()
    else:
        m = re.fullmatch(
            r"([\u4e00-\u9fff]{1,4})老師"
            r"\s*(?:買|購買|要買)\s*(.+)",
            clean
        )

        if not m:
            return None

        teacher = m.group(1).strip() + "老師"
        item = m.group(2).strip()

        context = (
            teacher_lookup_context.get(user_id)
            or conversation_context.get(user_id, {})
        )
        if context.get("teacher") != teacher:
            return None

        school = context.get("school", "")

    if not school:
        return None
    if not get_teacher_classes(school, teacher):
        return None

    return {
        "school": school,
        "teacher": teacher,
        "item": item
    }


def make_other_order_confirmation(order):
    return (
        "🧾 其他訂單確認\n\n"
        f"學校：{order['school']}\n"
        f"老師：{order['teacher']}\n"
        f"項目：{order['item']}\n"
        "進度：（空白）\n"
        "備註：（空白）\n\n"
        "如果正確，請回覆「確認」\n"
        "不要這筆請回覆「取消」"
    )


def confirm_other_order(user_id):
    order = pending_other_orders.get(user_id)
    if not order:
        return "⚠️ 找不到等待確認的其他訂單。"

    success, result = write_other_order_to_google_sheet(order)
    if not success:
        return "❌ 其他訂單寫入失敗，請稍後再試。"

    pending_other_orders.pop(user_id, None)

    return (
        "✅ 已寫入 Google「其他訂單」\n\n"
        f"學校：{order['school']}\n"
        f"老師：{order['teacher']}\n"
        f"項目：{order['item']}"
    )


def parse_other_order_query(text):
    patterns = [
        r"^(?:查|查詢)?\s*([\u4e00-\u9fff]{1,4}老師)\s*其他訂單$",
        r"^(?:查|查詢)\s*其他訂單\s*([\u4e00-\u9fff]{1,4}老師)$"
    ]
    for pattern in patterns:
        m = re.fullmatch(pattern, text.strip())
        if m:
            return {
                "teacher": m.group(1).strip(),
                "item_keyword": ""
            }
    return None


def parse_other_order_update(user_id, text):
    clean = text.strip()

    m = re.fullmatch(
        r"(?:其他訂單|其他單)\s*#?\s*(\d+)\s*"
        r"(進度|備註)\s*(?:改成|改為|改|設成|設為)\s*(.+)",
        clean
    )
    if m:
        return {
            "row_number": int(m.group(1)),
            "field": "progress" if m.group(2) == "進度" else "note",
            "value": m.group(3).strip()
        }

    m = re.fullmatch(
        r"(進度|備註)\s*(?:改成|改為|改|設成|設為)\s*(.+)",
        clean
    )
    if m and user_id in other_order_context:
        return {
            "use_context": True,
            "field": "progress" if m.group(1) == "進度" else "note",
            "value": m.group(2).strip()
        }

    return None


def resolve_other_order_target(user_id, update_data):
    if update_data.get("use_context"):
        return other_order_context.get(user_id)

    if update_data.get("row_number"):
        orders = lookup_other_orders(row_number=update_data["row_number"])
        return orders[0] if orders else None

    return None


def make_other_order_update_confirmation(target, field, value):
    field_name = "進度" if field == "progress" else "備註"
    return (
        "🔄 其他訂單修改確認\n\n"
        f"其他訂單 #{target['row_number']}\n"
        f"老師：{target.get('teacher', '')}\n"
        f"項目：{target.get('item', '')}\n"
        f"{field_name}改成：{value}\n\n"
        "正確請回覆「確認」\n"
        "取消請回覆「取消修改」"
    )


def confirm_other_order_update(user_id):
    update_data = pending_other_updates.get(user_id)
    if not update_data:
        return "⚠️ 找不到等待確認的其他訂單修改。"

    success, _ = update_other_order_in_google_sheet(
        update_data["row_number"],
        update_data["field"],
        update_data["value"]
    )

    if not success:
        return "❌ 其他訂單修改失敗，Google 資料沒有變動。"

    pending_other_updates.pop(user_id, None)

    return (
        "✅ 其他訂單已更新\n\n"
        f"其他訂單 #{update_data['row_number']}"
    )


def make_other_orders_reply(orders):
    lines = ["📦 其他訂單", ""]

    for item in orders[:10]:
        lines.extend([
            f"#{item.get('row_number', '')} {item.get('teacher', '')}",
            f"項目：{item.get('item', '')}",
            f"進度：{item.get('progress', '')}",
            f"備註：{item.get('note', '')}",
            ""
        ])

    return "\n".join(lines)


# =========================================================
# AI
# =========================================================
def is_ai_writing_request(text):
    words = [
        "幫我寫", "幫我整理", "幫我修飾", "幫我回覆",
        "幫我回報", "寫一段", "寫訊息", "回報老師",
        "傳給老師", "給老師一段", "草擬"
    ]
    return any(word in text for word in words)


def extract_referenced_order_number(text):
    m = re.search(r"(?:訂單)?\s*(\d{3})", text)
    return normalize_order_number(m.group(1)) if m else None


def ask_ai_with_order(user_id, user_text, order):
    class_lines = [
        f"{item['class_name']}班：{int(item['students'])}本"
        for item in order.get("classes", [])
    ]

    context = (
        "以下是 Google 試算表查到的真實訂單資料：\n"
        f"訂單編號：{order.get('order_number', '')}\n"
        f"老師：{order.get('teacher', '')}\n"
        f"學校：{order.get('school', '')}\n"
        f"書名：{order.get('book', '')}\n"
        f"出版社：{order.get('publisher', '')}\n"
        + "\n".join(class_lines)
        + f"\n總數量：{order.get('quantity', 0)}本\n"
        f"狀態：{order.get('status', '')}"
    )

    return ask_ai(user_id, user_text, extra_context=context)


def ask_ai(user_id, user_text, extra_context=""):
    if not OPENAI_API_KEY:
        print("OpenAI API key missing")
        return AI_FALLBACK_MESSAGE

    history = ai_conversation_context.get(user_id, [])
    recent = history[-8:]
    conversation_text = ""

    for item in recent:
        role_name = "使用者" if item["role"] == "user" else "助理"
        conversation_text += f"{role_name}：{item['text']}\n"

    if extra_context:
        conversation_text += (
            "\n【系統提供的真實資料】\n"
            + extra_context
            + "\n【資料結束】\n"
        )

    conversation_text += f"使用者：{user_text}\n助理："

    instructions = (
        "你是『大漢訂書小幫手』，使用者是公司內部工作人員，不是老師本人。"
        "請用繁體中文，口吻自然、簡潔、實用。"
        "不要把使用者說的『我』推論成王老師或任何老師。"
        "只有使用者明確說出『X老師』時，才能把該名稱視為老師。"
        "訂單建立、修改、查詢由外層固定程式處理；"
        "你不能聲稱自己已修改 Google 或已完成訂單。"
        "若系統提供真實訂單資料，可以引用資料幫使用者寫 LINE 訊息。"
        "不要捏造班級、數量、書名、出版社、到貨或出貨進度。"
        "若是在寫給老師的訊息，直接給可複製貼上的成品。"
        "回答適合直接顯示在 LINE，不使用 Markdown 表格。"
    )

    payload = {
        "model": OPENAI_MODEL,
        "instructions": instructions,
        "input": conversation_text,
        "max_output_tokens": 600
    }

    headers = {
        "Authorization": "Bearer " + OPENAI_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=30
        )

        print("OpenAI status:", response.status_code)

        if response.status_code != 200:
            print("OpenAI error:", response.text)
            return AI_FALLBACK_MESSAGE

        answer = extract_openai_text(response.json())
        if not answer:
            return AI_FALLBACK_MESSAGE

        answer = answer.strip()[:4500]

        history.append({"role": "user", "text": user_text})
        history.append({"role": "assistant", "text": answer})
        ai_conversation_context[user_id] = history[-10:]

        return answer

    except Exception as error:
        print("OpenAI request error:", error)
        return AI_FALLBACK_MESSAGE


def extract_openai_text(data):
    texts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if (
                content.get("type") == "output_text"
                and content.get("text")
            ):
                texts.append(content["text"])

    if texts:
        return "\n".join(texts)

    if data.get("output_text"):
        return str(data["output_text"])

    return ""


# =========================================================
# Google Apps Script
# =========================================================
def google_post(payload, timeout=15, retries=1):
    if not GOOGLE_SCRIPT_URL:
        print("GOOGLE_SCRIPT_URL missing")
        return None

    for attempt in range(retries):
        try:
            response = requests.post(
                GOOGLE_SCRIPT_URL,
                json=payload,
                timeout=timeout
            )

            print(
                "Google action:",
                payload.get("action"),
                "status:",
                response.status_code,
                "response:",
                response.text[:1000]
            )

            if response.status_code != 200:
                if attempt < retries - 1:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                return None

            return response.json()

        except Exception as error:
            print("Google request error:", payload.get("action"), error)

            if attempt < retries - 1:
                time.sleep(0.4 * (attempt + 1))
                continue

            return None

    return None


def get_teacher_classes(school, teacher):
    result = google_post({
        "action": "lookup_teacher",
        "school": school,
        "teacher": teacher
    })

    if not result:
        return None

    classes = []
    for item in result.get("classes", []):
        try:
            classes.append({
                "class_name": str(item.get("class_name", "")),
                "students": int(item.get("students", 0) or 0)
            })
        except Exception:
            continue

    return classes


def get_book_publisher(book):
    result = google_post({
        "action": "lookup_book",
        "book": book
    })

    if result and result.get("found"):
        return str(result.get("publisher", ""))

    return None


def write_to_google_sheet(order):
    result = google_post({
        "action": "create_order",
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get("classes", [])
    })

    if result and result.get("success") is True:
        return True, result.get("order_number")

    return False, None


def lookup_google_order(order_number):
    result = google_post({
        "action": "lookup_order",
        "order_number": normalize_order_number(order_number)
    }, retries=3)

    if not result or not result.get("success") or not result.get("found"):
        return None

    order = result.get("order", {})
    order["order_number"] = normalize_order_number(
        order.get("order_number", order_number)
    )
    order["classes"] = copy_classes(order.get("classes", []))
    order["quantity"] = calculate_total(order["classes"])
    return order


def update_google_order(order, modification_text):
    result = google_post({
        "action": "update_order",
        "order_number": order["order_number"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get("classes", []),
        "modification_text": modification_text
    })

    if result and result.get("success") is True:
        return True, result

    time.sleep(0.4)
    verified = lookup_google_order(order["order_number"])

    if verified and orders_have_same_core_data(verified, order):
        return True, {"success": True, "verified_after_write": True}

    return False, result


def lookup_book_orders_by_teacher(teacher):
    result = google_post({
        "action": "lookup_orders_by_teacher",
        "teacher": teacher
    })

    if not result or not result.get("success"):
        return []

    orders = result.get("orders", [])

    for order in orders:
        order["order_number"] = normalize_order_number(
            order.get("order_number", "")
        )
        order["classes"] = copy_classes(order.get("classes", []))
        order["quantity"] = calculate_total(order["classes"])

    return orders


def lookup_orders_by_date(date_text):
    result = google_post({
        "action": "lookup_orders_by_date",
        "date": date_text
    }, timeout=20, retries=3)

    if not result:
        return None

    if not result.get("success"):
        return None

    orders = result.get("orders", [])

    for order in orders:
        order["order_number"] = normalize_order_number(
            order.get("order_number", "")
        )
        order["classes"] = copy_classes(order.get("classes", []))
        if order["classes"]:
            order["quantity"] = calculate_total(order["classes"])
        else:
            order["quantity"] = int(order.get("quantity", 0) or 0)

    return orders


def cancel_google_order(order_number):
    result = google_post({
        "action": "cancel_order",
        "order_number": normalize_order_number(order_number)
    }, timeout=20, retries=3)

    return bool(result and result.get("success") is True), result or {}


def lookup_school_classes(school, grade="", class_name=""):
    result = google_post({
        "action": "lookup_school_classes",
        "school": school,
        "grade": grade,
        "class_name": class_name
    }, timeout=20, retries=3)

    if not result or not result.get("success"):
        return None

    classes = []
    for item in result.get("classes", []):
        classes.append({
            "school": str(item.get("school", school)),
            "class_name": str(item.get("class_name", "")),
            "students": int(item.get("students", 0) or 0)
        })

    return {
        "classes": classes,
        "class_count": int(result.get("class_count", len(classes)) or 0),
        "total_students": int(
            result.get(
                "total_students",
                sum(item["students"] for item in classes)
            ) or 0
        )
    }


def lookup_school_versions(
    school,
    grade="",
    subject="",
    academic_period=""
):
    result = google_post({
        "action": "lookup_versions",
        "school": school,
        "grade": grade,
        "subject": subject,
        "academic_period": academic_period
    }, timeout=20, retries=3)

    if not result or not result.get("success"):
        return None

    versions = []
    for item in result.get("versions", []):
        versions.append({
            "school": str(item.get("school", school)),
            "grade": str(item.get("grade", "")),
            "subject": str(item.get("subject", "")),
            "version": str(item.get("version", "")),
            "academic_period": str(item.get("academic_period", ""))
        })

    return {
        "latest_period": str(result.get("latest_period", "")),
        "versions": versions
    }


def write_other_order_to_google_sheet(order):
    result = google_post({
        "action": "create_other_order",
        "school": order["school"],
        "teacher": order["teacher"],
        "item": order["item"]
    })

    return bool(result and result.get("success")), result or {}


def lookup_other_orders(
    teacher="",
    item_keyword="",
    row_number=None
):
    payload = {
        "action": "lookup_other_orders",
        "teacher": teacher,
        "item_keyword": item_keyword
    }

    if row_number is not None:
        payload["row_number"] = row_number

    result = google_post(payload)

    if not result or not result.get("success"):
        return []

    return result.get("orders", [])


def update_other_order_in_google_sheet(row_number, field, value):
    result = google_post({
        "action": "update_other_order",
        "row_number": row_number,
        "field": field,
        "value": value
    })

    return bool(result and result.get("success")), result


# =========================================================
# 小工具
# =========================================================
def normalize_text(text):
    return str(text or "").replace("　", " ").strip()


def normalize_order_number(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return str(int(digits)).zfill(3)


def clean_book_name(book):
    book = str(book or "").strip()
    book = re.sub(r"^[、,，。:：\s]+", "", book)
    book = re.sub(r"^[跟和與]+", "", book)
    book = re.sub(r"[、,，。:：\s]+$", "", book)
    return book.strip()


def copy_classes(classes):
    result = []
    for item in classes or []:
        try:
            result.append({
                "class_name": str(item.get("class_name", "")),
                "students": int(item.get("students", 0) or 0)
            })
        except Exception:
            continue
    return result


def calculate_total(classes):
    return sum(
        int(item.get("students", 0) or 0)
        for item in classes or []
    )


def refresh_order_total(order):
    order["quantity"] = calculate_total(order.get("classes", []))


def find_order_class(order, class_name):
    for item in order.get("classes", []):
        if str(item.get("class_name")) == str(class_name):
            return item
    return None


def copy_order(order):
    result = dict(order)
    result["classes"] = copy_classes(order.get("classes", []))
    result["quantity"] = calculate_total(result["classes"])
    return result


def orders_have_same_core_data(actual_order, expected_order):
    if not actual_order or not expected_order:
        return False

    if str(actual_order.get("book", "")) != str(expected_order.get("book", "")):
        return False

    if str(actual_order.get("publisher", "")) != str(expected_order.get("publisher", "")):
        return False

    actual_classes = {
        str(item.get("class_name")): int(item.get("students", 0))
        for item in actual_order.get("classes", [])
    }

    expected_classes = {
        str(item.get("class_name")): int(item.get("students", 0))
        for item in expected_order.get("classes", [])
    }

    return actual_classes == expected_classes


# =========================================================
# LINE 回覆
# =========================================================
def reply_to_line(reply_token, message):
    if not reply_token:
        print("reply_token missing")
        return

    if not CHANNEL_ACCESS_TOKEN:
        print("❌ LINE access token 沒有讀到")
        return

    url = "https://api.line.me/v2/bot/message/reply"

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + CHANNEL_ACCESS_TOKEN
    }

    data = {
        "replyToken": reply_token,
        "messages": [
            {
                "type": "text",
                "text": str(message or "")[:4900]
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

        print("LINE reply status:", response.status_code)
        print("LINE reply response:", response.text)

    except Exception as error:
        print("LINE reply error:", error)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port
    )
