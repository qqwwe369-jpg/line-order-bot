from flask import Flask, request
import os
import re
import time
import requests
import json
import copy
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher

app = Flask(__name__)
APP_VERSION = "2026-09-02-candidate-replace-v7"

# =========================================================
# 速度優化：共用 HTTP Session + 讀取快取
# =========================================================
HTTP = requests.Session()
_google_read_cache = {}

# 這些都是相對穩定的資料庫讀取，可安全短時間快取。
_GOOGLE_CACHE_TTLS = {
    "list_schools": 600,
    "lookup_teacher_matches": 1800,
    "lookup_teacher": 1800,
    "lookup_school_classes": 300,
    "lookup_versions": 300,
    "lookup_book": 300,
    "lookup_fuzzy_candidates": 180,
}

def _cache_key(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(payload)

def clear_google_read_cache():
    _google_read_cache.clear()


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

FIXED_FALLBACK_MESSAGE = "👑 LeBron James 正在想辦法處理中，請稍後再試一次。"

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

pending_name_confirmations = {}

# 老師查詢失敗後，允許使用者下一句直接重打正確姓名。
# 例如：
# 王昱鈞教哪幾個班 -> 查不到
# 王毓君 -> 直接重新查老師資料庫，不掉到一般 AI。
pending_teacher_corrections = {}

# 引導式功能模式
guided_mode = {}

# =========================================================
# 訂書關鍵狀態：SQLite 暫存
# =========================================================
# Gunicorn/Render 可能由不同 worker 接收前後兩則 LINE 訊息。
# 只用 Python 全域 dict 時，上一句找到的候選可能在下一個 worker 看不到，
# 造成「確認」被誤當成新的老師姓名或書名。
_STATE_DB_PATH = os.environ.get("ORDER_STATE_DB_PATH", "/tmp/line_order_bot_state.sqlite3")

def _state_db():
    conn = sqlite3.connect(_STATE_DB_PATH, timeout=2)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS order_state ("
        "user_id TEXT PRIMARY KEY, draft_json TEXT, pending_json TEXT, updated_at REAL)"
    )
    return conn

def _persist_order_state(user_id):
    draft = order_flow_context.get(user_id)
    pending = pending_name_confirmations.get(user_id)
    if draft is None and pending is None:
        try:
            with _state_db() as conn:
                conn.execute("DELETE FROM order_state WHERE user_id=?", (user_id,))
        except Exception as e:
            print("state persist delete error:", e)
        return
    try:
        with _state_db() as conn:
            conn.execute(
                "INSERT INTO order_state(user_id,draft_json,pending_json,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET draft_json=excluded.draft_json, "
                "pending_json=excluded.pending_json, updated_at=excluded.updated_at",
                (
                    user_id,
                    json.dumps(draft, ensure_ascii=False) if draft is not None else None,
                    json.dumps(pending, ensure_ascii=False) if pending is not None else None,
                    time.time(),
                ),
            )
    except Exception as e:
        print("state persist error:", e)

def _hydrate_order_state(user_id):
    # 每一則訊息都從共享 SQLite 補回關鍵訂書狀態，避免跨 worker 遺失。
    try:
        with _state_db() as conn:
            row = conn.execute(
                "SELECT draft_json,pending_json FROM order_state WHERE user_id=?", (user_id,)
            ).fetchone()
        if not row:
            return
        draft_json, pending_json = row
        if draft_json:
            restored_draft = json.loads(draft_json)
            order_flow_context[user_id] = restored_draft
            # 最終訂購確認也要跨 worker 保存。否則使用者下一句「確認」
            # 可能只恢復訂書草稿，卻遺失 pending_orders，導致再次產生確認單。
            final_order = restored_draft.get("_pending_final_order")
            if isinstance(final_order, dict) and final_order:
                pending_orders[user_id] = final_order
        if pending_json:
            pending_name_confirmations[user_id] = json.loads(pending_json)
    except Exception as e:
        print("state hydrate error:", e)

def _clear_persisted_order_state(user_id):
    try:
        with _state_db() as conn:
            conn.execute("DELETE FROM order_state WHERE user_id=?", (user_id,))
    except Exception as e:
        print("state clear error:", e)

# 學校清單快取：學校名稱直接由 Google 資料庫取得。
# 未來新增學校，只要新增到「老師班級資料」或「學校版本資料」，
# 不需要再修改 app.py。
school_catalog_cache = {
    "schools": [],
    "expires_at": 0
}

# =========================================================
# Flask
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return f"LINE Order Bot is running! {APP_VERSION}"


@app.route("/callback", methods=["POST"])
def callback():
    body = request.get_json(silent=True) or {}

    print("Webhook received", APP_VERSION)
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

        request_started = time.perf_counter()
        try:
            reply_message = add_lebron_flavor(handle_message(user_id, user_text))
        except Exception as error:
            print("handle_message error:", error)
            reply_message = FIXED_FALLBACK_MESSAGE

        handle_elapsed = time.perf_counter() - request_started
        line_started = time.perf_counter()
        reply_to_line(reply_token, reply_message)
        line_elapsed = time.perf_counter() - line_started
        total_elapsed = time.perf_counter() - request_started
        print(
            f"PERF user={user_id} handle={handle_elapsed:.3f}s "
            f"line={line_elapsed:.3f}s total={total_elapsed:.3f}s text={user_text[:40]}"
        )

    return "OK", 200


# =========================================================
# 主流程
# =========================================================
def handle_message(user_id, user_text):
    text = normalize_text(user_text)
    _hydrate_order_state(user_id)
    if text in {"確認", "是", "對", "對的", "沒錯", "正確", "可以", "好", "就是"}:
        _d = order_flow_context.get(user_id) or {}
        _p = pending_name_confirmations.get(user_id) or _d.get("_pending_name_confirmation")
        print(f"STATE confirm user={user_id} draft_teacher={_d.get('teacher','')} pending={_p}")

    # 0. 重來：一定最優先
    if (
        text in ["重來", "重新開始", "全部重來", "全部重設"]
        or re.fullmatch(r"(?:重來|重新開始|全部重來|全部重設)[喔哦唷啦吧啊呀]*[！!。.]?", text)
    ):
        clear_all_user_state(user_id)
        return get_main_menu_reply()

    # 0.1 純本地固定指令：絕對不能碰 Google / AI
    # 招呼與功能查詢必須在所有資料庫查詢之前直接 return。
    if is_greeting_request(text):
        return get_greeting_reply()

    if is_help_request(text):
        return get_help_reply()

    # 本地版本查詢：方便確認 Render 是否真的部署到最新 app.py。
    if text in {"版本", "版本號", "目前版本", "程式版本"}:
        return f"目前機器人版本：{APP_VERSION}"

    # 0.2 「確認」硬性優先：只要上一句有名稱候選，絕不能再把「確認」當姓名/書名搜尋。
    if text in {"確認", "是", "對", "對的", "沒錯", "正確", "可以", "好", "就是"}:
        draft_for_confirm = order_flow_context.get(user_id) or {}
        if user_id in pending_name_confirmations or draft_for_confirm.get("_pending_name_confirmation"):
            confirmed_reply = handle_name_confirmation(user_id, text)
            if confirmed_reply is not None:
                return confirmed_reply

        # 名稱確認之後，第二優先就是「最終訂單確認」。
        # 這一步一定要在訂書流程鎖定之前處理，否則「確認」會重新走
        # handle_order_flow() 並再次產生同一張訂購確認。
        final_order = pending_orders.get(user_id) or draft_for_confirm.get("_pending_final_order")
        if isinstance(final_order, dict) and final_order:
            pending_orders[user_id] = final_order
            return confirm_new_order(user_id)

    # 0.5 引導式功能入口：主選單五個指令都一定有下一步
    if is_teacher_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "teacher_lookup"
        return (
            "👨‍🏫 老師查詢\n\n"
            "請直接輸入老師姓名。\n"
            "如果有同音字或打錯一個字，我會先幫你找最接近的老師。"
        )

    if is_order_mode_start(text):
        guided_mode.pop(user_id, None)
        pending_teacher_corrections.pop(user_id, None)
        pending_name_confirmations.pop(user_id, None)
        return handle_order_flow(user_id, text)

    if is_version_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "version_lookup"
        return (
            "📖 教科書版本查詢\n\n"
            "請輸入學校名稱。\n"
            "例如：天母國中、衛理女中、華興中學\n\n"
            "也可以直接輸入「天母國中八年級」或「華興中學國一數學」。"
        )

    if is_history_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "history_lookup"
        return get_history_lookup_guide_reply()

    if is_other_order_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "other_order"
        return (
            "📦 其他訂單\n\n"
            "請直接輸入：學校＋老師＋品項。\n"
            "例如：天母國中王老師買書面紙20張\n\n"
            "我會先整理成確認畫面，等你回覆「確認」後才寫入 Google。"
        )

    if guided_mode.get(user_id) == "teacher_lookup":
        if text in ["取消", "回主選單", "主選單", "離開"]:
            guided_mode.pop(user_id, None)
            pending_teacher_corrections.pop(user_id, None)
            pending_name_confirmations.pop(user_id, None)
            return get_main_menu_reply()
        if user_id in pending_name_confirmations:
            fuzzy_reply = handle_name_confirmation(user_id, text)
            if fuzzy_reply is not None:
                return fuzzy_reply
        return handle_guided_teacher_lookup(user_id, text)

    if guided_mode.get(user_id) == "version_lookup":
        if text in ["取消", "回主選單", "主選單", "離開"]:
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        return handle_guided_version_lookup(user_id, text)

    if guided_mode.get(user_id) == "history_lookup":
        if text in ["取消", "回主選單", "主選單", "離開"]:
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        return handle_guided_history_lookup(user_id, text)

    if guided_mode.get(user_id) == "other_order":
        if text in ["回主選單", "主選單", "離開"]:
            pending_other_orders.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()

        if text == "確認" and user_id in pending_other_orders:
            reply = confirm_other_order(user_id)
            if not reply.startswith("❌") and not reply.startswith("⚠️"):
                guided_mode.pop(user_id, None)
            return reply

        if text in ["取消", "不要了", "這筆不要"] and user_id in pending_other_orders:
            pending_other_orders.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return "❌ 已取消這筆「其他訂單」，Google 沒有寫入。"

        return handle_guided_other_order(user_id, text)

    # 0.9 名稱候選確認一定要早於訂書流程。
    # 例如：蔡書懸 -> 候選蔡書玄 -> 使用者回「確認」時，
    # 必須先吃掉這個確認，不能把「確認」當成新的老師姓名/書名去查。
    order_draft_for_confirm = order_flow_context.get(user_id) or {}
    if user_id in pending_name_confirmations or order_draft_for_confirm.get("_pending_name_confirmation"):
        fuzzy_reply = handle_name_confirmation(user_id, text)
        if fuzzy_reply is not None:
            return fuzzy_reply

    # 訂書模式鎖定：進入後不允許一般老師查詢把話題搶走。
    if user_id in order_flow_context:
        order_reply = handle_order_flow(user_id, text)
        if order_reply is not None:
            return order_reply

    # 1. 名稱容錯確認（老師／書名／學校）
    fuzzy_reply = handle_name_confirmation(user_id, text)
    if fuzzy_reply is not None:
        return fuzzy_reply

    # 1.5 老師查詢失敗後，下一句若只是 2～4 個中文字姓名，
    # 直接視為更正老師姓名，重新查 Google 老師資料庫。
    correction_reply = handle_teacher_name_correction(user_id, text)
    if correction_reply is not None:
        return correction_reply

    # 1.6 單獨輸入正確老師姓名，也能直接查。
    # 僅做 Google exact 唯一命中，不做模糊猜測，避免誤判一般短句。
    bare_teacher_reply = handle_bare_teacher_exact_lookup(user_id, text)
    if bare_teacher_reply is not None:
        return bare_teacher_reply

    # 2. 固定功能選單
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

    # 14. AI 功能已停用：草擬／自由問答不再呼叫 OpenAI
    if is_ai_writing_request(text):
        return FIXED_FALLBACK_MESSAGE

    # 15. 老師資料庫：優先於學校統計
    # 例如「造德芳教哪幾個班」不能被誤判成「華興有哪些班」。
    if looks_like_teacher_lookup(text):
        return handle_teacher_lookup(user_id, text)

    if user_id in teacher_lookup_context and looks_like_teacher_followup(text):
        return handle_teacher_followup(user_id)

    # 16. 學校教科書版本
    version_query = parse_school_version_query(user_id, text)
    if version_query:
        return handle_school_version_query(version_query)

    # 17. 學校／年級／班級學生人數
    stats_query = parse_school_stats_query(user_id, text)
    if stats_query:
        return handle_school_stats_query(stats_query)

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

    # 20. 其他內容：固定卡關訊息，不再交給 AI
    return FIXED_FALLBACK_MESSAGE


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
    pending_name_confirmations.pop(user_id, None)
    pending_teacher_corrections.pop(user_id, None)
    guided_mode.pop(user_id, None)
    _clear_persisted_order_state(user_id)


# =========================================================
# 引導式主選單／查老師模式
# =========================================================
def get_main_menu_reply():
    return (
        "🔄 已重新開始\n\n"
        "請告訴我你要使用哪一個功能：\n\n"
        "📚 要訂書 → 輸入「我要訂書」\n"
        "👨‍🏫 要查老師 → 輸入「查老師」\n"
        "📖 要查版本 → 輸入「查版本」\n"
        "📅 要查訂單 → 輸入「查訂單」\n"
        "📦 其他訂單 → 輸入「其他訂單」\n\n"
        "完成一個查詢後，下一句會重新當成新的對話。"
    )

def is_teacher_mode_start(text):
    compact=re.sub(r"[\s，,。.!！?？]+","",str(text or ""))
    return compact in {"查老師","我要查老師","查詢老師","老師查詢","找老師","我要找老師","查老師資料"}

def is_order_mode_start(text):
    compact=re.sub(r"[\s，,。.!！?？]+","",str(text or ""))
    return compact in {"訂書","我要訂書","我訂書","開始訂書","幫我訂書","我要下單","幫我下單","要訂書"}

def is_version_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "查版本", "我要查版本", "查教科書版本", "版本查詢",
        "我要查教科書版本", "教科書版本"
    }


def is_history_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "查訂單", "我要查訂單", "訂單查詢", "查訂書",
        "查訂書訂單", "我要查訂書訂單"
    }


def is_other_order_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "其他訂單", "我要其他訂單", "新增其他訂單",
        "登記其他訂單", "我要登記其他訂單"
    }


def get_history_lookup_guide_reply():
    return (
        "📅 訂單查詢\n\n"
        "請直接告訴我要怎麼查：\n"
        "• 今天 → 輸入「今天」\n"
        "• 昨天 → 輸入「昨天」\n"
        "• 指定日期 → 例如「8月30日」\n"
        "• 訂單編號 → 例如「001」\n"
        "• 老師 → 例如「王老師」\n\n"
        "查不到時我會繼續留在「查訂單」模式。"
    )


def _finish_guided_mode(user_id, reply):
    guided_mode.pop(user_id, None)
    return reply


def handle_guided_version_lookup(user_id, text):
    clean = str(text or "").strip()
    school = extract_school_name(clean)
    if not school:
        return (
            "📖 教科書版本查詢\n\n"
            "我還在「查版本」模式。\n"
            "請輸入學校名稱，例如：天母國中、衛理女中、華興中學。"
        )

    grade = extract_grade_text(clean)
    subjects = [
        "國文", "英文", "數學", "自然", "生物", "理化",
        "地科", "地球科學", "社會", "歷史", "地理", "公民"
    ]
    subject = next((s for s in subjects if s in clean), "")
    if subject == "地球科學":
        subject = "地科"

    query = {
        "school": school,
        "grade": grade,
        "subject": subject,
        "academic_period": ""
    }
    result = lookup_school_versions(
        query["school"], query["grade"], query["subject"], query["academic_period"]
    )

    if result is None:
        return (
            "⚠️ 教科書版本資料庫暫時查詢失敗。\n\n"
            "我還在「查版本」模式，請稍後直接再輸入學校名稱。"
        )
    if not result.get("versions"):
        return (
            "⚠️ 查不到這個條件的教科書版本資料。\n\n"
            f"學校：{school}"
            + (f"\n年級：{grade}" if grade else "")
            + (f"\n科目：{subject}" if subject else "")
            + "\n\n我還在「查版本」模式，可以直接換一個學校、年級或科目。"
        )
    return _finish_guided_mode(user_id, handle_school_version_query(query))


def parse_guided_date(text):
    clean = re.sub(r"\s+", "", str(text or "").strip())
    if clean in {"今天", "今日", "今天的", "今日的"}:
        return datetime.now().strftime("%Y-%m-%d")
    if clean in {"昨天", "昨日", "昨天的", "昨日的"}:
        from datetime import timedelta
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    m = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?", clean)
    if m:
        year = int(m.group(1)) if m.group(1) else datetime.now().year
        try:
            return datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None

    m = re.fullmatch(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})", clean)
    if m:
        year = int(m.group(1)) if m.group(1) else datetime.now().year
        try:
            return datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{2})(\d{2})", clean)
    if m:
        try:
            return datetime(datetime.now().year, int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def handle_guided_history_lookup(user_id, text):
    clean = re.sub(r"\s+", "", str(text or "").strip())

    date_text = parse_guided_date(clean)
    if date_text:
        orders = lookup_orders_by_date(date_text)
        if orders is None:
            return "⚠️ 訂單查詢暫時失敗。\n\n我還在「查訂單」模式，可以直接再試一次。"
        if not orders:
            return (
                f"📅 {date_text} 目前查不到訂書訂單。\n\n"
                "我還在「查訂單」模式，可以改查其他日期、訂單編號或老師。"
            )
        return _finish_guided_mode(user_id, make_daily_orders_reply(date_text, orders))

    m = re.fullmatch(r"(?:查)?(?:訂單)?(\d{1,6})", clean)
    if m:
        number = normalize_order_number(m.group(1))
        order = lookup_google_order(number)
        if not order:
            return (
                f"⚠️ 查不到訂單 {number}。\n\n"
                "我還在「查訂單」模式，可以直接輸入另一個編號。"
            )
        historical_order_context[user_id] = order
        return _finish_guided_mode(user_id, make_historical_order_reply(order))

    teacher_name = re.sub(r"(?:老師)?(?:訂單|訂書|進度|紀錄)$", "", clean)
    teacher_name = re.sub(r"老師$", "", teacher_name)
    if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", teacher_name):
        teacher_matches = lookup_teacher_matches(teacher_name + "老師", school="")
        canonical = ""
        if len(teacher_matches) == 1:
            canonical = str(teacher_matches[0].get("teacher", "") or "").strip()
        else:
            fuzzy = resolve_fuzzy_name("teacher", teacher_name + "老師", school="")
            if fuzzy.get("status") == "auto":
                canonical = str(fuzzy.get("value", "") or "").strip()

        if not canonical:
            canonical = teacher_name + "老師"

        orders = lookup_book_orders_by_teacher(canonical)
        if not orders:
            return (
                "⚠️ 查不到這位老師的訂書紀錄。\n\n"
                f"老師：{canonical}\n\n"
                "我還在「查訂單」模式，可以直接改輸入其他老師、日期或訂單編號。"
            )
        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]
        return _finish_guided_mode(user_id, make_teacher_book_orders_reply(canonical, orders))

    return get_history_lookup_guide_reply()


def handle_guided_other_order(user_id, text):
    parsed = parse_other_order(user_id, str(text or "").strip())
    if parsed:
        pending_other_orders[user_id] = parsed
        return make_other_order_confirmation(parsed)

    return (
        "📦 其他訂單\n\n"
        "我還在「其他訂單」模式。\n"
        "請輸入：學校＋老師＋品項。\n"
        "例如：天母國中王老師買書面紙20張\n\n"
        "如果要離開，輸入「主選單」。"
    )


def clear_task_states_for_new_mode(user_id):
    order_flow_context.pop(user_id,None); pending_orders.pop(user_id,None)
    pending_name_confirmations.pop(user_id,None); pending_teacher_corrections.pop(user_id,None)
    teacher_lookup_context.pop(user_id,None)
    _clear_persisted_order_state(user_id)

def normalize_teacher_name_input(text):
    clean=re.sub(r"[，,。.!！?？\s]+","",str(text or ""))
    clean=re.sub(r"^(?:我要)?(?:查|找)(?:一下)?(?:老師)?","",clean)
    clean=re.sub(r"(?:老師)?(?:教哪個班|教哪歌班|教哪幾個班|教哪幾班|教哪些班|有哪些班|有幾個班|教什麼科|教哪科)$","",clean)
    clean=re.sub(r"老師$","",clean)
    return clean

def finish_teacher_lookup(user_id,item):
    reply=make_teacher_reply(item["school"],item["teacher"],item["classes"])
    guided_mode.pop(user_id,None); pending_teacher_corrections.pop(user_id,None)
    pending_name_confirmations.pop(user_id,None); teacher_lookup_context.pop(user_id,None)
    conversation_context.pop(user_id,None)
    return reply

def handle_guided_teacher_lookup(user_id,text):
    name=normalize_teacher_name_input(text)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}",name):
        return "👨‍🏫 老師查詢\n\n我還在查老師模式。\n請直接輸入 2～4 個中文字的老師姓名。"
    matches=lookup_teacher_matches(name+"老師",school="")
    if len(matches)==1: return finish_teacher_lookup(user_id,matches[0])
    if len(matches)>1:
        schools=unique_list([m.get("school","") for m in matches if m.get("school")])
        return f"🔎 找到同名老師。\n\n老師：{name}\n學校：{'、'.join(schools)}\n\n請輸入「學校＋老師姓名」，我會繼續留在查老師模式。"
    fuzzy=resolve_fuzzy_name("teacher",name+"老師",school="")
    if fuzzy.get("status")=="auto":
        cm=lookup_teacher_matches(str(fuzzy.get("value","") or "").strip(),school=str(fuzzy.get("school","") or "").strip())
        if len(cm)==1: return finish_teacher_lookup(user_id,cm[0])
    if fuzzy.get("status")=="confirm":
        pending_name_confirmations[user_id]={"field":"teacher","purpose":"guided_teacher_lookup","value":fuzzy.get("value",""),"school":fuzzy.get("school",""),"original":name}
        return ("🔎 我猜你可能打到同音字或錯字。\n\n"+f"你輸入：{name}\n你是指：{fuzzy.get('value','')}"+(f"（{fuzzy.get('school')}）" if fuzzy.get("school") else "")+" 嗎？\n\n請回覆「是」或「不是」。")
    return "⚠️ 目前找不到這位老師。\n\n"+f"你輸入：{name}\n\n"+"我還在「查老師」模式。\n請直接重新輸入老師姓名，不用再打一次「查老師」。"

# =========================================================
# 招呼／功能選單（純本地，不查 Google）
# =========================================================
def is_greeting_request(text):
    compact = re.sub(r"[，,。.!！?？\s]+", "", str(text or "").lower())
    greetings = ["你好", "妳好", "您好", "哈囉", "哈啰", "嗨", "早安", "午安", "晚安", "在嗎"]
    return any(compact == g or compact.startswith(g + "我是") for g in greetings)


def get_greeting_reply():
    return (
        "你好！我是大漢訂書小幫手 👑 LeBron James 還差一個助攻\n\n"
        "很高興為你服務！\n\n"
        "你可以直接輸入：\n"
        "📚 我要訂書\n"
        "👨‍🏫 查老師\n"
        "📖 查版本\n"
        "📅 查訂單\n"
        "📦 其他訂單\n\n"
        "也可以輸入「有什麼功能」查看使用方式。"
    )


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
        "請告訴我你要使用哪一個功能：\n\n"
        "📚 要訂書 → 輸入「我要訂書」\n"
        "👨‍🏫 要查老師 → 輸入「查老師」\n"
        "📖 要查版本 → 輸入「查版本」\n"
        "📅 要查訂單 → 輸入「查訂單」\n"
        "📦 其他訂單 → 輸入「其他訂單」\n\n"
        "進入功能後，我會一步一步引導你完成。"
    )


# =========================================================
# 訂書流程
# =========================================================
def normalize_person_name(value):
    return re.sub(r"老師$", "", re.sub(r"[\s，,。.!！?？]", "", str(value or ""))).strip()


def validate_order_teacher_input(user_id, raw_text, draft):
    """訂書老師關卡：一次資料庫搜尋完成精確/錯字候選，避免連打 Google。"""
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    clean = re.sub(r"老師$", "", clean).strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        return "請輸入老師姓名，例如：蔡書玄"

    school = str(draft.get("school") or "").strip()
    # 只打一個 fuzzy endpoint；它本身就是從老師資料庫回候選。
    candidates = lookup_fuzzy_candidates("teacher", clean, school=school)
    if not candidates and school:
        candidates = lookup_fuzzy_candidates("teacher", clean, school="")

    if not candidates:
        return ("⚠️ 老師資料庫目前找不到符合資料。\n\n"
                f"你輸入：{clean}\n\n"
                "我不會先把這個名字存進訂單。請重新輸入老師姓名。")

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    candidate_school = str(first.get("school", "") or school).strip()
    score = float(first.get("score", 0) or 0)

    # 資料庫正式姓名與輸入完全一致：直接通過老師關卡。
    if normalize_person_name(value) == normalize_person_name(clean):
        draft["teacher"] = value
        draft["school"] = candidate_school
        draft["classes"] = []
        draft.pop("_pending_name_confirmation", None)
        pending_name_confirmations.pop(user_id, None)
        order_flow_context[user_id] = draft
        _persist_order_state(user_id)
        return make_order_guide_reply(draft)

    # 錯一字/同音/近似：一定先讓使用者確認，不能自動把錯字存進訂單。
    if value and score >= 0.52:
        pending = {
            "field": "teacher", "purpose": "order_teacher",
            "value": value, "school": candidate_school,
            "original": clean
        }
        pending_name_confirmations[user_id] = pending
        # 同步寫進訂書 draft，避免 Render 多 worker / 狀態切換時只剩訂書狀態卻遺失候選確認。
        draft["_pending_name_confirmation"] = dict(pending)
        order_flow_context[user_id] = draft
        _persist_order_state(user_id)
        print(f"STATE saved teacher candidate user={user_id} value={value} school={candidate_school}")
        # 候選不一定就是使用者真正想找的人。清楚告知：確認只代表採用這一位；
        # 若不是，直接輸入另一個老師姓名，系統會取消舊候選並重新查資料庫。
        other_candidates = []
        for c in candidates[1:4]:
            other_value = str(c.get("value", "") or "").strip()
            other_school = str(c.get("school", "") or "").strip()
            other_score = float(c.get("score", 0) or 0)
            if other_value and other_score >= 0.52 and other_value != value:
                other_candidates.append(other_value + (f"（{other_school}）" if other_school else ""))

        reply = ("🔎 老師姓名可能有錯字。\n\n"
                 f"你輸入：{clean}\n"
                 f"資料庫找到：{value}" +
                 (f"（{candidate_school}）" if candidate_school else "") +
                 "\n\n請問你是指這位老師嗎？\n"
                 "是的請回覆「確認」。\n"
                 "如果不是，請直接輸入正確老師姓名，我會取消這個候選並重新查資料庫。")
        if other_candidates:
            reply += "\n\n其他接近的老師：" + "、".join(other_candidates)
        return reply

    return ("⚠️ 老師資料庫目前無法確認這個姓名。\n\n"
            f"你輸入：{clean}\n\n"
            "我不會往下一步。請重新輸入老師姓名。")


def validate_order_book_input(user_id, raw_text, draft):
    """訂書書名關卡：一次關鍵字搜尋，候選一定來自書籍資料庫。"""
    query = clean_book_name(str(raw_text or "").strip())
    query = re.sub(r"^(?:我要訂|要訂|訂)", "", query).strip()
    if not query:
        return "請輸入書名或書名關鍵字。"

    candidates = lookup_book_candidates_enhanced(query)
    if not candidates:
        return ("⚠️ 書籍資料庫找不到符合的書名。\n\n"
                f"你輸入：{query}\n\n"
                "老師資料已保留，我不會往下一步。請重新輸入書名或更明確的關鍵字。")

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    score = float(first.get("score", 0) or 0)

    # 即使很像也先確認正式書名，避免同系列不同科目/冊次誤訂。
    if value and score >= 0.52:
        pending = {
            "field": "book", "purpose": "order_book",
            "value": value,
            "publisher": str(first.get("publisher", "") or ""),
            "original": query
        }
        pending_name_confirmations[user_id] = pending
        # 書名候選也同步保存在訂書 draft，確保下一句「確認」一定能找到剛才的正式書名。
        draft["_pending_name_confirmation"] = dict(pending)
        order_flow_context[user_id] = draft
        _persist_order_state(user_id)
        extra = [
            str(c.get("value", "") or "").strip()
            for c in candidates[1:3]
            if c.get("value") and float(c.get("score", 0) or 0) >= max(0.52, score - 0.08)
        ]
        msg = ("🔎 我從書籍資料庫找到最接近的書。\n\n"
               f"你輸入：{query}\n"
               f"資料庫找到：{value}\n")
        if extra:
            msg += "其他接近結果：" + "、".join(extra) + "\n"
        return msg + "\n請問是這一本嗎？\n請回覆「確認」；不是的話請直接重新輸入書名。"

    return ("⚠️ 我目前無法確認書名。\n\n"
            f"你輸入：{query}\n\n"
            "老師資料已保留，我不會往下一步。請再輸入一次完整書名或更明確的關鍵字。")

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
        _persist_order_state(user_id)
        return make_order_guide_reply(order_flow_context[user_id])

    draft = order_flow_context.get(user_id, {
        "teacher": "",
        "school": "",
        "classes": [],
        "book": ""
    })

    # 第二層保險：就算主流程的確認分流沒有吃到，訂書流程本身也絕不允許
    # 把「確認」當成老師姓名或書名重新丟去 Google。
    confirm_words = {"確認", "是", "對", "對的", "沒錯", "正確", "可以", "好", "就是"}
    if clean in confirm_words:
        pending = pending_name_confirmations.get(user_id) or draft.get("_pending_name_confirmation")
        if pending:
            pending_name_confirmations[user_id] = dict(pending)
            reply = handle_name_confirmation(user_id, clean)
            if reply is not None:
                return reply
        # 沒有候選狀態時也禁止搜尋「確認」這個名字。
        if not draft.get("teacher"):
            return ("⚠️ 我沒有讀到上一個老師候選。\n\n"
                    "請重新輸入老師姓名，我會重新找一次；找到後再回覆「確認」。")
        if draft.get("teacher") and not draft.get("book"):
            return "請告訴我要訂哪一本書？"

    # 已進入訂書流程時，每一關都先查資料庫驗證；成功前不准往下一關。
    if user_id in order_flow_context and not draft.get("teacher"):
        return validate_order_teacher_input(user_id, clean, draft)

    if user_id in order_flow_context and draft.get("teacher") and not draft.get("book"):
        return validate_order_book_input(user_id, clean, draft)

    parsed = parse_order_message(clean)

    if not parsed["has_order_intent"] and user_id not in order_flow_context:
        recent = conversation_context.get(user_id)
        if recent and looks_like_contextual_class_book(clean, recent):
            parsed = parse_contextual_class_book(clean, recent)
        else:
            return None

    if user_id in order_flow_context:
        parsed = merge_followup_into_parsed(clean, parsed, draft)
        if draft.get("teacher") and not draft.get("book") and not parsed.get("book"):
            candidate=re.sub(r"^(?:我要訂|要訂|訂)","",clean).strip()
            if candidate and any(w in candidate for w in ["講義","評量","教材","複習","測驗","題本","自修","課本","習作","學習單"]):
                parsed["book"]=clean_book_name(candidate); parsed["has_order_intent"]=True

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


    order_flow_context[user_id] = draft
    _persist_order_state(user_id)

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
        teacher_display = str(draft.get("teacher") or "").strip()
        school_display = str(draft.get("school") or "").strip()
        if school_display:
            lines.append(f"老師：{teacher_display}（{school_display}）")
        else:
            lines.append(f"老師：{teacher_display}")
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

    # -----------------------------------------------------
    # 對話狀態優先：上一輪正在等老師，這一輪就先當老師處理。
    # 例如：
    #   使用者：我要訂書
    #   機器人：請告訴我是哪一位老師？
    #   使用者：蔡志強
    # 這時必須得到「蔡志強老師」，不能誤判成書名。
    # -----------------------------------------------------
    if not draft.get("teacher"):
        teacher, school = extract_teacher_and_school(clean)

        # 支援使用者只輸入姓名、不加「老師」。
        # 僅在「目前確定正在等老師」時啟用，避免一般訊息被亂抓成老師。
        if not teacher:
            bare_name = re.sub(r"[，,。.!！?？\s]+", "", str(clean or ""))
            obvious_non_teacher_words = [
                "講義", "評量", "教材", "複習", "測驗", "題本",
                "自修", "課本", "習作", "學習單",
                "國文", "英文", "數學", "自然", "社會",
                "國一", "國二", "國三", "七年級", "八年級", "九年級"
            ]

            if (
                re.fullmatch(r"[\u4e00-\u9fff]{2,4}", bare_name)
                and not any(word in bare_name for word in obvious_non_teacher_words)
            ):
                teacher = bare_name + "老師"

        if teacher:
            result["teacher"] = teacher
            if school:
                result["school"] = school

            # 這一輪的目標欄位就是老師。
            # 即使通用解析器先猜到 book，也要清掉，避免「蔡志強」變成書名。
            result["book"] = ""
            result["has_order_intent"] = True
            return result

        # 目前還在等老師，而且這句又不像老師姓名。
        # 不把它硬塞進書名，維持原狀並繼續詢問老師。
        result["book"] = ""
        result["has_order_intent"] = True
        return result

    # 已經有老師後，才處理班級與書名。
    if not result.get("teacher"):
        teacher, school = extract_teacher_and_school(clean)
        if teacher:
            result["teacher"] = teacher
            result["school"] = school

    classes = extract_classes(clean)
    if classes and not result.get("classes"):
        result["classes"] = classes

    # 對話進行中，只要書名還缺，就允許直接口語補書名。
    # 但只有「老師已經確定」後，才允許這段邏輯。
    if draft.get("teacher") and not draft.get("book") and not result.get("book"):
        teacher_in_text = result.get("teacher", "")
        classes_in_text = result.get("classes", [])
        candidate = extract_book_candidate(
            clean,
            teacher_in_text,
            classes_in_text
        )

        # 單獨回「王老師」或只有班級號碼時，不誤判成書名。
        if candidate and "老師" not in candidate and not re.fullmatch(
            r"[\d、,，跟和與\s]+", candidate
        ):
            result["book"] = candidate

    result["has_order_intent"] = True
    return result


def build_order_from_draft(user_id, draft):
    teacher = draft["teacher"]
    school = str(draft.get("school") or "").strip()
    requested_classes = unique_list(draft.get("classes", []))
    book = clean_book_name(draft["book"])

    # 老師先做跨校 exact lookup。若只有一筆，直接採用資料庫正式學校與姓名。
    teacher_classes = []
    exact_matches = lookup_teacher_matches(teacher, school=school)
    if len(exact_matches) == 1:
        exact = exact_matches[0]
        teacher = exact["teacher"]
        school = exact["school"]
        teacher_classes = copy_classes(exact.get("classes", []))
        draft["teacher"] = teacher
        draft["school"] = school
    elif school:
        teacher_classes = get_teacher_classes(school, teacher) or []

    # exact 找不到才啟用模糊／同音錯字比對，而且不鎖死錯誤預設學校。
    if not teacher_classes:
        match = resolve_fuzzy_name("teacher", teacher, school=school)

        if match.get("status") == "auto":
            teacher = match["value"]
            school = match.get("school") or school
            draft["teacher"] = teacher
            draft["school"] = school
            teacher_classes = get_teacher_classes(school, teacher)

        elif match.get("status") == "confirm":
            pending_name_confirmations[user_id] = {
                "field": "teacher",
                "value": match["value"],
                "school": match.get("school") or school,
                "original": teacher
            }
            return (
                "🔎 我猜你可能打到同音字或錯字。\n\n"
                f"你輸入：{teacher}\n"
                f"你是指：{match['value']} 嗎？\n\n"
                "請回覆「是」或「不是」。"
            )

    if not teacher_classes:
        return FIXED_FALLBACK_MESSAGE

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

    # 書名精確查不到時，再查最接近的正式書名。
    if not publisher:
        match = resolve_fuzzy_name("book", book)

        if match.get("status") == "auto":
            book = match["value"]
            draft["book"] = book
            publisher = match.get("publisher") or get_book_publisher(book)

        elif match.get("status") == "confirm":
            pending_name_confirmations[user_id] = {
                "field": "book",
                "value": match["value"],
                "publisher": match.get("publisher", ""),
                "original": book
            }
            # 候選尚未確認前，不把錯的舊書名鎖在草稿裡。
            # 使用者若直接輸入另一個書名，下一句可以重新辨識。
            draft["book"] = ""
            order_flow_context[user_id] = draft
            return (
                "🔎 我猜你可能打到同音字或錯字。\n\n"
                f"你輸入：{book}\n"
                f"你是指：{match['value']} 嗎？\n\n"
                "請回覆「是」或「不是」；也可以直接重新輸入書名。"
            )

    if not publisher:
        # 查不到時只清掉書名，老師／學校／班級全部保留。
        # 下一句會被當成新的書名重新查詢，而不是卡在第一次錯誤值。
        draft["book"] = ""
        order_flow_context[user_id] = draft
        pending_name_confirmations.pop(user_id, None)
        return FIXED_FALLBACK_MESSAGE

    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "publisher": publisher,
        "classes": selected,
        "quantity": calculate_total(selected)
    }

    pending_orders[user_id] = order

    # 把最終待確認訂單一起存在訂書狀態，讓 Render/Gunicorn 不同 worker
    # 也能在下一句「確認」時直接寫入 Google，而不是重新產生確認單。
    draft["_pending_final_order"] = copy.deepcopy(order)
    order_flow_context[user_id] = draft
    _persist_order_state(user_id)

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

    # 常見出版社／版本同音錯字先直接標準化。
    # 其他老師、學校、書名錯字會再走 Google 資料庫模糊比對。
    common_aliases = {
        "康宣": "康軒",
        "康玄": "康軒",
        "韓林": "翰林",
        "寒林": "翰林",
        "難一": "南一",
        "南壹": "南一",
        # 常見書名語音／同音輸入
        "超級悍將": "超級翰將",
        "超級漢將": "超級翰將",
        "超級瀚將": "超級翰將",
    }
    for wrong, correct in common_aliases.items():
        clean = clean.replace(wrong, correct)

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

    # 訂書口語：蔡志強要訂書 / 蔡志強老師要訂書
    # 必須只抓人名，不能把「要」吃進老師姓名。
    m = re.fullmatch(
        r"([\u4e00-\u9fff]{2,4}?)(?:老師)?(?:要|想要|想|準備|打算)?(?:訂書|下單)",
        clean
    )
    if m:
        return m.group(1) + "老師", ""

    m = re.search(
        r"([\u4e00-\u9fff]{2,16}(?:國中|高中|國小|中學))"
        r"([\u4e00-\u9fff]{1,4})老師",
        clean
    )
    if m:
        return m.group(2) + "老師", m.group(1)

    m = re.search(r"([\u4e00-\u9fff]{1,4})老師", clean)
    if m:
        name = m.group(1)
        if name in ["哪位", "這位", "那位", "一位", "我的", "我們"]:
            return "", ""
        return name + "老師", ""

    # 口語：趙德芳教哪幾個班 / 造德芳教哪些班 / 王小明教什麼科
    m = re.match(
        r"^([\u4e00-\u9fff]{2,4})"
        r"(?=教(?:哪幾個班|哪幾班|哪些班|幾個班|幾班|什麼科|哪一科|哪科|哪些科))",
        clean
    )
    if m:
        return m.group(1) + "老師", ""

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
    order_flow_context.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
    guided_mode.pop(user_id, None)
    _clear_persisted_order_state(user_id)

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
    if "訂單" in text or "訂書進度" in text or is_ai_writing_request(text):
        return False

    teacher, _ = extract_teacher_and_school(text)
    if not teacher:
        return False

    words = [
        "教幾個班", "教幾班", "教哪個班", "教哪歌班", "教哪幾班", "教哪幾個班", "教哪些班",
        "有幾個班", "有哪些班", "哪幾班", "哪幾個班",
        "班級資料", "班級人數", "每班幾人", "每班人數",
        "學生人數", "總人數", "幾個學生", "多少學生",
        "教什麼科", "教哪一科", "教哪科", "教哪些科"
    ]
    return any(word in text for word in words)


def handle_bare_teacher_exact_lookup(user_id, text):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))

    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        return None

    blocked_words = {
        "重來", "取消", "確認", "修改", "查詢", "訂書",
        "數學", "英文", "國文", "自然", "社會", "理化",
        "生物", "歷史", "地理", "公民", "版本", "人數"
    }
    if clean in blocked_words:
        return None

    # 只查 exact；若唯一命中才把它視為老師。
    matches = lookup_teacher_matches(clean + "老師", school="")

    if len(matches) != 1:
        return None

    item = matches[0]
    context = {
        "school": item["school"],
        "teacher": item["teacher"],
        "classes": copy_classes(item["classes"])
    }

    pending_teacher_corrections.pop(user_id, None)
    teacher_lookup_context[user_id] = context
    conversation_context[user_id] = context

    return make_teacher_reply(
        context["school"],
        context["teacher"],
        context["classes"]
    )


def handle_teacher_name_correction(user_id, text):
    pending = pending_teacher_corrections.get(user_id)
    if not pending:
        return None

    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))

    # 只有單純 2～4 個中文字才當成「重新輸入老師姓名」，
    # 避免把書名、訂單內容或一般句子誤判成老師。
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        return None

    school = str(pending.get("school", "") or "").strip()

    # 先做精確資料庫查詢，不做 AI 猜測。
    matches = lookup_teacher_matches(clean + "老師", school=school)

    # 若前一次帶到的學校上下文不正確，精確校內查不到時，
    # 再跨所有學校查一次正確姓名。
    if not matches and school:
        matches = lookup_teacher_matches(clean + "老師", school="")

    if len(matches) == 1:
        item = matches[0]
        context = {
            "school": item["school"],
            "teacher": item["teacher"],
            "classes": copy_classes(item["classes"])
        }

        pending_teacher_corrections.pop(user_id, None)
        teacher_lookup_context[user_id] = context
        conversation_context[user_id] = context

        return make_teacher_reply(
            context["school"],
            context["teacher"],
            context["classes"]
        )

    if len(matches) > 1:
        school_names = unique_list(
            [item.get("school", "") for item in matches if item.get("school")]
        )
        return (
            "🔎 找到同名老師。\n\n"
            f"老師：{clean}\n"
            f"學校：{'、'.join(school_names)}\n\n"
            "請再告訴我是哪一間學校。"
        )

    # 精確姓名仍找不到，才做既有的錯字／近似比對。
    fuzzy = resolve_fuzzy_name("teacher", clean + "老師", school=school)

    if fuzzy.get("status") == "auto":
        candidate = str(fuzzy.get("value", "") or "").strip()
        candidate_school = str(fuzzy.get("school", "") or school).strip()
        matches = lookup_teacher_matches(candidate, school=candidate_school)

        if len(matches) == 1:
            item = matches[0]
            context = {
                "school": item["school"],
                "teacher": item["teacher"],
                "classes": copy_classes(item["classes"])
            }

            pending_teacher_corrections.pop(user_id, None)
            teacher_lookup_context[user_id] = context
            conversation_context[user_id] = context

            return make_teacher_reply(
                context["school"],
                context["teacher"],
                context["classes"]
            )

    if fuzzy.get("status") == "confirm":
        pending_name_confirmations[user_id] = {
            "field": "teacher",
            "purpose": "teacher_lookup",
            "value": fuzzy.get("value", ""),
            "school": fuzzy.get("school", ""),
            "original": clean
        }
        pending_teacher_corrections.pop(user_id, None)

        return (
            "🔎 我猜你可能打到同音字或錯字。\n\n"
            f"你輸入：{clean}\n"
            f"你是指：{fuzzy.get('value', '')}"
            + (
                f"（{fuzzy.get('school')}）"
                if fuzzy.get("school")
                else ""
            )
            + " 嗎？\n\n"
            "請回覆「是」或「不是」。"
        )

    # 保留 correction 狀態，讓使用者可以再重打一次。
    return (
        "⚠️ 還是查不到這位老師。\n\n"
        f"你輸入：{clean}\n"
        "請再確認姓名；你可以直接重新打正確姓名。"
    )


def looks_like_teacher_followup(text):
    words = [
        "總共幾個班", "幾個班", "總人數多少", "總人數",
        "總共幾人", "總共多少人", "每班幾人", "每班人數",
        "班級人數", "有哪些班", "哪幾班"
    ]
    return any(word in text for word in words)


def handle_teacher_lookup(user_id, text):
    teacher, parsed_school = extract_teacher_and_school(text)
    explicit_school = parsed_school or extract_school_name(text)
    context_school = get_context_school(user_id)
    school = explicit_school or context_school

    if not teacher:
        return "⚠️ 找不到老師姓名。"

    # 先依使用者明確指定的學校／目前上下文查。
    matches = lookup_teacher_matches(teacher, school=school)

    # 如果學校只是上一輪上下文帶進來，而這次沒有明確指定學校，
    # 校內精確查不到時就自動跨全部學校再查一次。
    # 避免前一題查華興，下一題查別校老師時被錯誤鎖在華興。
    if not matches and context_school and not explicit_school:
        matches = lookup_teacher_matches(teacher, school="")
        if len(matches) == 1:
            school = matches[0].get("school", "")

    if len(matches) == 1:
        item = matches[0]
        school = item["school"]
        teacher = item["teacher"]
        classes = item["classes"]

    elif len(matches) > 1:
        school_names = unique_list(
            [item.get("school", "") for item in matches if item.get("school")]
        )
        return (
            "🔎 資料庫裡找到同名老師。\n\n"
            f"老師：{teacher}\n"
            f"學校：{'、'.join(school_names)}\n\n"
            "請把學校一起告訴我，例如「華興中學蔡志強老師教哪幾班」。"
        )

    else:
        # 精確找不到，再做錯字／同音近似比對。
        match = resolve_fuzzy_name("teacher", teacher, school=school)

        # 若學校只是舊上下文、不是這次明確指定，
        # 校內近似也找不到時再跨校做一次。
        if (
            match.get("status") == "none"
            and context_school
            and not explicit_school
        ):
            match = resolve_fuzzy_name("teacher", teacher, school="")

        if match.get("status") == "auto":
            teacher = match["value"]
            school = match.get("school") or school
            matches = lookup_teacher_matches(teacher, school=school)

            if len(matches) == 1:
                item = matches[0]
                school = item["school"]
                teacher = item["teacher"]
                classes = item["classes"]
            else:
                classes = []

        elif match.get("status") == "confirm":
            pending_name_confirmations[user_id] = {
                "field": "teacher",
                "purpose": "teacher_lookup",
                "value": match["value"],
                "school": match.get("school", ""),
                "original": teacher
            }
            return (
                "🔎 我猜你可能打到同音字或錯字。\n\n"
                f"你輸入：{teacher}\n"
                f"你是指：{match['value']}"
                + (f"（{match.get('school')}）" if match.get("school") else "")
                + " 嗎？\n\n"
                "請回覆「是」或「不是」。"
            )
        else:
            classes = []

    if not classes:
        pending_teacher_corrections[user_id] = {
            "school": school or "",
            "original_teacher": teacher
        }

        return (
            "⚠️ 查不到老師資料\n\n"
            + (f"學校：{school}\n" if school else "")
            + f"老師：{teacher}\n\n"
            "目前 Google「老師班級資料」沒有找到符合資料。\n"
            "你可以直接重打正確老師姓名，我會立刻重新查詢。"
        )

    context = {
        "school": school,
        "teacher": teacher,
        "classes": copy_classes(classes)
    }

    teacher_lookup_context[user_id] = context
    conversation_context[user_id] = context
    pending_teacher_corrections.pop(user_id, None)

    return make_teacher_reply(school, teacher, classes)


def handle_teacher_followup(user_id):
    # 同一位老師的追問直接使用上一輪已查到的班級資料。
    # 不要再次呼叫 Google Apps Script，避免原本約 6 秒的重複查詢。
    context = teacher_lookup_context.get(user_id)
    if not context:
        return None

    classes = copy_classes(context.get("classes", []))
    if not classes:
        return "⚠️ 老師資料庫暫時查詢失敗。"

    return make_teacher_reply(
        context.get("school", ""),
        context.get("teacher", ""),
        classes
    )


def make_teacher_reply(school, teacher, classes):
    total = calculate_total(classes)
    display_teacher = re.sub(r"老師$", "", str(teacher or "").strip())

    subjects = []
    for item in classes or []:
        item_subjects = item.get("subjects", [])
        if isinstance(item_subjects, str):
            item_subjects = [item_subjects]

        single_subject = str(item.get("subject", "") or "").strip()
        if single_subject:
            item_subjects = list(item_subjects or []) + [single_subject]

        for subject in item_subjects or []:
            subject = str(subject or "").strip()
            if subject and subject not in subjects:
                subjects.append(subject)

    lines = [
        f"• {item['class_name']}班：{int(item['students'])}人"
        for item in classes
    ]

    subject_line = (
        f"科目：{'、'.join(subjects)}\n"
        if subjects else ""
    )

    return (
        ""
        "👨‍🏫 老師資料庫\n"
        f"學校：{school}\n"
        f"老師：{display_teacher}\n"
        + subject_line
        + "\n"
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

    # 「造德芳教哪幾個班」是老師查詢，不是學校班級查詢。
    if re.match(
        r"^[\u4e00-\u9fff]{2,4}教(?:哪幾個班|哪幾班|哪些班|幾個班|幾班)",
        clean
    ):
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

    period = str(result.get("latest_period", "") or "").strip()
    if not period:
        periods = unique_list(
            [
                str(item.get("academic_period", "") or "").strip()
                for item in versions
                if str(item.get("academic_period", "") or "").strip()
            ]
        )
        if len(periods) == 1:
            period = periods[0]

    return (
        ""
        f"📚 {query['school']}"
        + (f" {query['grade']}" if query.get("grade") else "")
        + (f"\n學年度：{period}" if period else "")
        + "\n\n"
        + "\n".join(lines)
    )


def extract_school_name(text):
    """
    從 Google 資料庫目前存在的學校清單判讀學校。
    支援完整名稱與簡稱，例如：
    華興中學 -> 華興
    天母國中 -> 天母
    未來新增學校不需要再改程式。
    """
    clean = re.sub(r"[，,。.!！?？：:\s]+", "", str(text or ""))
    if not clean:
        return ""

    schools = get_school_catalog()

    # 先比完整校名，再比去除「國中／高中／國小／中學」後的簡稱。
    aliases = []
    for school in schools:
        canonical = str(school or "").strip()
        if not canonical:
            continue

        short = re.sub(r"(?:國中|高中|國小|中學)$", "", canonical)
        aliases.append((canonical, canonical))

        if short and short != canonical:
            aliases.append((short, canonical))

    # 長名稱優先，避免短名稱先誤吃。
    aliases.sort(key=lambda item: len(item[0]), reverse=True)

    for alias, canonical in aliases:
        if alias and alias in clean:
            return canonical

    # 即使學校清單暫時讀不到，完整校名仍可正常解析。
    m = re.search(
        r"([\u4e00-\u9fff]{2,16}(?:國中|高中|國小|中學))",
        clean
    )
    if m:
        full_name = m.group(1).strip()

        if not schools or full_name in schools:
            return full_name

        fuzzy = resolve_fuzzy_name("school", full_name)
        if fuzzy.get("status") == "auto":
            return fuzzy.get("value", "")

    # 最後嘗試把「七年級／數學／多少人」等查詢文字拿掉，
    # 只留下可能的學校簡稱，再做資料庫模糊比對。
    school_hint = clean
    school_hint = re.sub(
        r"(?:七年級|八年級|九年級|國一|國二|國三|[789]年級)",
        "",
        school_hint
    )
    school_hint = re.sub(
        r"(?:國文|英文|數學|自然|生物|理化|地科|社會|歷史|地理|公民)",
        "",
        school_hint
    )
    school_hint = re.sub(
        r"(?:有多少人|多少人|幾人|幾個人|學生人數|總人數|"
        r"有幾個班|幾個班|多少班|有哪些班|哪幾班|哪幾個班|"
        r"版本|哪一版|哪個版本|什麼版本|教科書|查詢|查)",
        "",
        school_hint
    )

    if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", school_hint or ""):
        fuzzy = resolve_fuzzy_name("school", school_hint)
        if fuzzy.get("status") == "auto":
            return fuzzy.get("value", "")

    return ""


def get_school_catalog(force_refresh=False):
    now = time.time()

    if (
        not force_refresh
        and school_catalog_cache.get("schools")
        and now < float(school_catalog_cache.get("expires_at", 0) or 0)
    ):
        return list(school_catalog_cache["schools"])

    result = google_post(
        {"action": "list_schools"},
        timeout=7,
        retries=1
    )

    schools = []
    if result and result.get("success"):
        schools = unique_list(
            [
                str(item or "").strip()
                for item in result.get("schools", [])
                if str(item or "").strip()
            ]
        )

    # 成功時快取 10 分鐘；失敗則保留舊快取，不把它清空。
    if schools:
        school_catalog_cache["schools"] = schools
        school_catalog_cache["expires_at"] = now + 600
        return list(schools)

    return list(school_catalog_cache.get("schools", []))


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
    guided_mode.pop(user_id, None)

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


# =========================================================
# 名稱容錯／同音錯字
# =========================================================
def normalize_book_match_text(value):
    text = str(value or "").strip()
    aliases = {
        "悍將": "翰將",
        "漢將": "翰將",
        "瀚將": "翰將",
        "康宣": "康軒",
        "康玄": "康軒",
        "韓林": "翰林",
        "寒林": "翰林",
    }
    for wrong, correct in aliases.items():
        text = text.replace(wrong, correct)
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text).lower()


def book_core_text(value):
    text = normalize_book_match_text(value)
    text = re.sub(r"\d+", "", text)
    for word in [
        "國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科",
        "社會", "歷史", "地理", "公民",
        "講義", "評量", "教材", "複習", "測驗", "題本", "自修", "課本", "習作"
    ]:
        text = text.replace(word, "")
    return text


def book_keyword_score(query, candidate):
    q = normalize_book_match_text(query)
    c = normalize_book_match_text(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0

    score = 0.0
    qcore = book_core_text(query)
    ccore = book_core_text(candidate)

    if qcore and ccore:
        if qcore == ccore:
            score += 0.55
        elif qcore in ccore or ccore in qcore:
            score += 0.45
        else:
            score += 0.35 * SequenceMatcher(None, qcore, ccore).ratio()

    subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    q_subjects = {x for x in subjects if x in q}
    c_subjects = {x for x in subjects if x in c}
    if q_subjects and c_subjects and q_subjects & c_subjects:
        score += 0.20

    q_nums = set(re.findall(r"\d+", q))
    c_nums = set(re.findall(r"\d+", c))
    if q_nums and c_nums and q_nums & c_nums:
        score += 0.15

    generic = ["講義", "評量", "教材", "複習", "測驗", "題本", "自修", "課本", "習作"]
    if any(x in q and x in c for x in generic):
        score += 0.05

    score += 0.05 * SequenceMatcher(None, q, c).ratio()
    return min(score, 1.0)


def lookup_book_candidates_enhanced(query):
    """書名關鍵字搜尋：優先只打 Google 一次，避免舊版十幾個 variants 逐次連線。"""
    query = str(query or "").strip()
    if not query:
        return []

    candidates = lookup_fuzzy_candidates("book", query)

    # 只有整句完全沒候選，才用核心系列名補查一次；最多兩次，不再迴圈狂打 Google。
    if not candidates:
        core = book_core_text(query)
        if core and core != query:
            candidates = lookup_fuzzy_candidates("book", core)

    merged = {}
    for item in candidates:
        value = str(item.get("value", "") or "").strip()
        if not value:
            continue
        local_score = book_keyword_score(query, value)
        # Google 自己的相似度可作輔助，但以本機關鍵字分數為主。
        remote_score = float(item.get("score", 0) or 0)
        score = max(local_score, remote_score * 0.85)
        merged[value] = {
            "value": value,
            "publisher": str(item.get("publisher", "") or ""),
            "school": "",
            "score": score
        }

    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:10]

def resolve_fuzzy_name(kind, query, school=""):
    """
    精確查不到時才使用。
    Google Apps Script 會回傳依相似度排序的候選名稱。
    高相似度直接採用；中等相似度要求使用者確認；低相似度不猜。
    """
    candidates = (
        lookup_book_candidates_enhanced(query)
        if kind == "book"
        else lookup_fuzzy_candidates(kind, query, school=school)
    )
    if not candidates:
        return {"status": "none"}

    first = candidates[0]
    score = float(first.get("score", 0) or 0)
    second_score = 0.0
    if len(candidates) > 1:
        second_score = float(candidates[1].get("score", 0) or 0)

    gap = score - second_score

    # 短名稱（老師／學校）一個字打錯，相似度仍可能只有 0.7~0.8。
    if kind == "teacher":
        # 三字中文姓名打錯一字時，SequenceMatcher 約 0.67。
        # 只有候選明顯唯一才自動修正；若有接近的第二名仍會要求確認。
        auto_threshold = 0.64
        confirm_threshold = 0.52
        required_gap = 0.16
    elif kind == "school":
        auto_threshold = 0.78
        confirm_threshold = 0.58
        required_gap = 0.10
    else:  # book：採關鍵字／冊次／科目／系列名綜合分數
        auto_threshold = 0.78
        confirm_threshold = 0.58
        required_gap = 0.08

    result = {
        "value": str(first.get("value", "")),
        "school": str(first.get("school", "")),
        "publisher": str(first.get("publisher", "")),
        "score": score
    }

    if score >= auto_threshold and (len(candidates) == 1 or gap >= required_gap):
        result["status"] = "auto"
        return result

    if score >= confirm_threshold:
        result["status"] = "confirm"
        return result

    return {"status": "none"}


def handle_name_confirmation(user_id, text):
    pending = pending_name_confirmations.get(user_id)
    # 訂書流程額外把候選存在 draft 內。即使全域 pending 因 worker/流程切換遺失，
    # 使用者下一句「確認」仍可正確接續，不會把「確認」當成新的老師或書名。
    if not pending:
        draft_for_pending = order_flow_context.get(user_id) or {}
        pending = draft_for_pending.get("_pending_name_confirmation")
        if pending:
            pending_name_confirmations[user_id] = dict(pending)
    if not pending:
        return None

    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))
    yes_words = {"是", "對", "對的", "沒錯", "正確", "可以", "好", "就是", "確認"}
    no_words = {"不是", "不對", "錯", "錯了", "不要", "否"}

    if clean in yes_words:
        pending_name_confirmations.pop(user_id, None)

        if pending.get("purpose") == "guided_teacher_lookup":
            teacher=pending.get("value",""); school=pending.get("school","")
            matches=lookup_teacher_matches(teacher,school=school)
            if len(matches)==1: return finish_teacher_lookup(user_id,matches[0])
            guided_mode[user_id]="teacher_lookup"
            return "⚠️ 這個名稱仍找不到唯一老師資料。\n\n我還在「查老師」模式，請直接重新輸入老師姓名。"

        if pending.get("purpose") == "teacher_lookup":
            teacher = pending.get("value", "")
            school = pending.get("school", "")
            matches = lookup_teacher_matches(teacher, school=school)

            if len(matches) == 1:
                item = matches[0]
                context = {
                    "school": item["school"],
                    "teacher": item["teacher"],
                    "classes": copy_classes(item["classes"])
                }
                teacher_lookup_context[user_id] = context
                conversation_context[user_id] = context
                pending_teacher_corrections.pop(user_id, None)
                return make_teacher_reply(
                    context["school"],
                    context["teacher"],
                    context["classes"]
                )

            return "⚠️ 確認名稱後仍找不到唯一老師資料，請把學校名稱一起告訴我。"

        draft = order_flow_context.get(user_id)
        if not draft:
            return "✅ 已確認名稱。請重新輸入剛才的訂書內容。"

        draft.pop("_pending_name_confirmation", None)

        if pending["field"] == "teacher":
            # 這個候選本身就是上一句由 Google 老師資料庫 fuzzy endpoint 回傳的正式資料。
            # 使用者回「確認」後直接採用暫存候選，不再重新打 Google，避免確認又卡 7~15 秒。
            draft["teacher"] = str(pending.get("value", "") or "").strip()
            draft["school"] = str(pending.get("school", "") or "").strip()
            draft["classes"] = []
        elif pending["field"] == "book":
            # 書名候選同樣已由書籍資料庫搜尋取得；確認後直接採用正式書名與出版社暫存。
            draft["book"] = str(pending.get("value", "") or "").strip()
            if pending.get("publisher"):
                draft["publisher"] = str(pending.get("publisher", "") or "").strip()
        elif pending["field"] == "school":
            draft["school"] = pending["value"]

        order_flow_context[user_id] = draft
        _persist_order_state(user_id)

        if draft.get("teacher") and draft.get("book"):
            result = build_order_from_draft(user_id, draft)
            if user_id in pending_orders:
                order_flow_context.pop(user_id, None)
            return result

        return make_order_guide_reply(draft)

    if clean in no_words:
        was_guided_teacher=pending.get("purpose")=="guided_teacher_lookup"
        pending_name_confirmations.pop(user_id,None)
        draft_for_pending = order_flow_context.get(user_id)
        if draft_for_pending:
            draft_for_pending.pop("_pending_name_confirmation", None)
            order_flow_context[user_id] = draft_for_pending
        _persist_order_state(user_id)
        if was_guided_teacher:
            guided_mode[user_id]="teacher_lookup"
            return "好，我不採用剛才的候選。\n\n我還在「查老師」模式，請直接重新輸入老師姓名。"
        field_name = {
            "teacher": "老師姓名",
            "book": "書名",
            "school": "學校名稱"
        }.get(pending.get("field"), "名稱")
        return f"好，沒有採用。請重新輸入正確的{field_name}。"

    # 使用者沒有回答是/不是，而是直接輸入新的老師／書名：
    # 明確放棄舊候選並保留目前訂書流程，讓同一則訊息立刻重新進資料庫驗證。
    old_pending = dict(pending)
    pending_name_confirmations.pop(user_id, None)
    draft_for_pending = order_flow_context.get(user_id)
    if draft_for_pending:
        draft_for_pending.pop("_pending_name_confirmation", None)
        order_flow_context[user_id] = draft_for_pending
    _persist_order_state(user_id)
    print(
        f"STATE candidate replaced user={user_id} "
        f"field={old_pending.get('field','')} old={old_pending.get('value','')} new_input={clean}"
    )
    return None


def lookup_fuzzy_candidates(kind, query, school=""):
    result = google_post({
        "action": "lookup_fuzzy_candidates",
        "kind": kind,
        "query": str(query or ""),
        "school": str(school or "")
    }, timeout=4.5, retries=1)

    if not result or not result.get("success"):
        return []

    return result.get("candidates", [])[:8]


# =========================================================
# Google Apps Script
# =========================================================
def google_post(payload, timeout=10, retries=1):
    if not GOOGLE_SCRIPT_URL:
        print("GOOGLE_SCRIPT_URL missing")
        return None

    action = str(payload.get("action", ""))
    cache_ttl = int(_GOOGLE_CACHE_TTLS.get(action, 0) or 0)
    key = _cache_key(payload) if cache_ttl > 0 else ""
    now = time.time()

    # 快取命中：完全不打 Google Apps Script，通常可把回覆縮到很短。
    if cache_ttl > 0 and key in _google_read_cache:
        item = _google_read_cache.get(key) or {}
        if now < float(item.get("expires_at", 0) or 0):
            print(f"Google cache HIT: {action}")
            return copy.deepcopy(item.get("data"))
        _google_read_cache.pop(key, None)

    attempts = max(1, int(retries or 1))
    started = time.perf_counter()

    for attempt in range(attempts):
        try:
            response = HTTP.post(
                GOOGLE_SCRIPT_URL,
                json=payload,
                timeout=timeout
            )

            elapsed = time.perf_counter() - started
            print(
                f"Google action={action} status={response.status_code} "
                f"elapsed={elapsed:.3f}s attempt={attempt + 1}/{attempts}"
            )

            if response.status_code != 200:
                if attempt < attempts - 1:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                return None

            data = response.json()

            if cache_ttl > 0 and isinstance(data, dict):
                _google_read_cache[key] = {
                    "data": copy.deepcopy(data),
                    "expires_at": time.time() + cache_ttl
                }

            # 寫入／修改／取消成功後，把舊讀取快取清掉，避免看到舊資料。
            if action in {
                "create_order", "update_order", "cancel_order",
                "create_other_order", "update_other_order"
            } and isinstance(data, dict) and data.get("success"):
                clear_google_read_cache()

            return data

        except Exception as error:
            elapsed = time.perf_counter() - started
            print(f"Google request error: {action} elapsed={elapsed:.3f}s error={error}")

            if attempt < attempts - 1:
                time.sleep(0.15 * (attempt + 1))
                continue

            return None

    return None


def lookup_teacher_matches(teacher, school=""):
    result = google_post({
        "action": "lookup_teacher_matches",
        "teacher": str(teacher or "").strip(),
        "school": str(school or "").strip()
    }, timeout=7, retries=1)

    if not result or not result.get("success"):
        return []

    matches = []
    for item in result.get("matches", []):
        classes = copy_classes(item.get("classes", []))
        matches.append({
            "school": str(item.get("school", "")).strip(),
            "teacher": str(item.get("teacher", "")).strip(),
            "subjects": unique_list(item.get("subjects", [])),
            "classes": classes
        })

    return matches


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
            subjects = item.get("subjects", [])
            if isinstance(subjects, str):
                subjects = [subjects]

            classes.append({
                "class_name": str(item.get("class_name", "")),
                "students": int(item.get("students", 0) or 0),
                "subjects": unique_list(
                    [str(s or "").strip() for s in subjects if str(s or "").strip()]
                )
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
    }, timeout=7, retries=1)

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
    def do_lookup(school_name):
        return google_post({
            "action": "lookup_versions",
            "school": school_name,
            "grade": grade,
            "subject": subject,
            "academic_period": academic_period
        }, timeout=7, retries=1)

    result = do_lookup(school)

    if not result or not result.get("success"):
        return None

    # 有些工作表存「華興」，老師表存「華興中學」。
    # 第一次如果查不到，直接用去掉校種後的簡稱再查一次。
    if not result.get("versions"):
        short_school = re.sub(
            r"(?:國民中學|國中|高中|國小|中學)$",
            "",
            str(school or "").strip()
        )
        if short_school and short_school != school:
            retry = do_lookup(short_school)
            if retry and retry.get("success") and retry.get("versions"):
                result = retry

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
            subjects = item.get("subjects", [])
            if isinstance(subjects, str):
                subjects = [subjects]

            single_subject = str(item.get("subject", "") or "").strip()
            if single_subject:
                subjects = list(subjects or []) + [single_subject]

            result.append({
                "class_name": str(item.get("class_name", "")),
                "students": int(item.get("students", 0) or 0),
                "subjects": unique_list(
                    [str(s or "").strip() for s in subjects if str(s or "").strip()]
                )
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
# LeBron 固定人設層
# =========================================================
def add_lebron_flavor(message):
    """
    所有系統回覆統一加上 LeBron James 人設。
    只改「說話方式」，不改任何訂單、查詢、資料庫或對話流程邏輯。
    """
    body = str(message or "").strip()
    if not body:
        body = "目前沒有可顯示的內容。"

    # 避免原本已經有 LeBron 開場的訊息重複加兩次。
    if body.startswith("👑 LeBron James"):
        return body

    compact = re.sub(r"\s+", "", body)

    # 查不到／錯誤
    if any(key in compact for key in [
        "查不到", "找不到", "處理失敗", "查詢失敗", "寫入失敗",
        "更新失敗", "沒有讀到", "系統剛剛處理失敗"
    ]):
        intro = "👑 LeBron James 這球沒找到目標"

    # 還缺資料／需要使用者補充
    elif any(key in compact for key in [
        "還差", "請告訴我", "請提供", "請問是哪", "需要哪",
        "尚未提供", "請選擇", "請直接告訴我"
    ]):
        intro = "👑 LeBron James 還差一個助攻"

    # 取消
    elif any(key in compact for key in [
        "確認取消", "已取消", "取消這張", "取消這筆", "取消訂單"
    ]):
        intro = "👑 LeBron James 幫你把這球撤回來了"

    # 修改
    elif any(key in compact for key in [
        "確認修改", "修改確認", "已修改", "調整", "改成", "更新成功"
    ]):
        intro = "👑 LeBron James 幫你把陣容調整好了"

    # 訂購確認
    elif "訂購確認" in compact or "訂書確認" in compact:
        intro = "👑 LeBron James 幫你把這張單整理好了"

    # 教科書版本
    elif any(key in compact for key in ["教科書版本", "版本資料", "版本："]):
        intro = "👑 LeBron James 幫你把版本查好了"

    # 人數／老師班級資料
    elif any(key in compact for key in [
        "班級資料", "學生人數", "總學生人數", "班級總數", "幾個班", "多少人"
    ]):
        intro = "👑 LeBron James 幫你點完名了"

    # 歷史訂單／單日訂單／進度
    elif any(key in compact for key in [
        "歷史訂單", "訂書紀錄", "訂書進度", "單日訂單", "筆訂單", "訂單編號"
    ]):
        intro = "👑 LeBron James 幫你把紀錄翻出來了"

    # 功能說明
    elif any(key in compact for key in [
        "大漢訂書小幫手", "我可以幫你", "功能", "直接用平常講話"
    ]):
        intro = "👑 LeBron James 幫你把戰術板打開了"

    # 成功建立／確認
    elif any(key in compact for key in [
        "訂單已確認", "已建立", "成功", "已寫入", "完成"
    ]):
        intro = "👑 LeBron James 這球漂亮收尾"

    # 其他一般回覆（包含 AI 問答／草擬文字）
    else:
        intro = "👑 LeBron James 幫你處理好了"

    return f"{intro}\n\n{body}"


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
        response = HTTP.post(
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
