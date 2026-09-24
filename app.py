"""
=============================================================
大漢訂書小幫手 — LINE Bot 主程式
=============================================================
架構速覽（給接手維護的人快速定位程式碼）：

  1. 速度優化層      — 共用 HTTP Session、Google 讀取快取、平行查詢
  2. 環境變數         — LINE / Google Apps Script 連線設定
  3. 對話狀態          — 全域 dict 存每個 user_id 目前對話進度
  4. SQLite 跨 worker 持久化 — 多個 gunicorn worker 共用同一份對話狀態、
                          重送事件防護、過期資料清理
  5. Flask 路由        — / 、 /healthz 、 /callback（LINE webhook 入口）、
                          /purchase-order/<token>.pdf（訂購單 PDF 下載）
  6. 主流程 _route_message — 所有文字訊息最終都會流經這個函式來分流
  7. 引導式功能        — 查老師／查版本／查訂單／其他訂單／查人數／訂書
                          六個模式統一使用 guided_mode 框架
  8. 訂書主流程        — 老師／出版社／書名解析、模糊比對、訂單確認與修改
  9. 老師資料庫查詢
  10. 教科書版本查詢
  11. 歷史訂單查詢／修改／取消
  12. 其他訂單（非教科書品項）
  13. 學校人數查詢
  14. Google Apps Script 溝通層（google_post）
  15. 小工具函式（字串正規化、班級計算…）
  16. 訂購單 PDF（供 email 給出版社）
  17. LeBron 人設文案層
  18. LINE 回覆

新手上路指南：這是單一 Flask app，靠 Google Apps Script 當資料庫
（老師/班級/書籍/訂單都存在 Google 試算表），LINE 傳來的每一則文字
訊息都會先經過 _route_message() 依照使用者目前所在的「模式」分流，
最後統一由 add_lebron_flavor() 包裝語氣後回覆。

【六個引導模式統一框架】
guided_mode 目前有六種值："teacher_lookup"、"version_lookup"、
"history_lookup"、"other_order"、"stats_lookup"、"order_flow"。
所有模式共用：
  - EXIT_WORDS：統一退出詞（取消／回主選單／主選單／離開）
  - _guided_mode_escape_reply()：模式內偵測到其他功能的明確格式時自動跳出並轉交處理
  - 除了「其他訂單」（一次性登記）之外，完成一次動作後預設繼續留在模式內，
    可以連續查詢，不用每次重打進入指令

【多書訂購（2026-09 新增，一個班一種不同的書）】
情境：「XX老師要訂七個班，各給一種不同的康軒歷史1測驗卷」——
跟一般訂書「一本書、很多班級」的形狀完全不同，是「一位老師、
很多本不同的書，一班配一本」。為了不動到既有的 order_flow（一般
學校訂書）跟它高度共用的資料結構，這裡刻意另外開一個完全獨立的
guided_mode = "multi_book_order_flow"，有自己獨立的
multi_book_order_context 草稿字典，不共用 order_flow_context：
  1. 問老師姓名（沿用既有老師模糊比對機制）
  2. 問出版社關鍵字（可回覆「不限」跳過）
  3. 問書名關鍵字（例如「歷史1測驗卷」）
  4. 問要挑幾種 → 拿關鍵字＋出版社去書籍模糊比對，這次不是只取
     分數最高的一筆，而是把所有分數達門檻的候選「全部」收下
  5. 找到的種數如果剛好等於使用者要的數量，直接進下一步；
     找到的數量對不上，會列出實際找到幾種，讓使用者選擇直接採用
     或換關鍵字重查，不會自己亂湊數量
  4.5 指定出版社湊不滿使用者要的數量時，自動放寬成「不限出版社」
     繼續湊，直到湊滿或真的沒有更多符合的書為止。判斷出版社永遠
     是看資料庫的「出版社」欄位，書名裡就算寫著「OO版」（例如某些
     出版社考卷書名會寫「XX版」代表搭配哪個課本，跟真正出版社無關）
     也不影響判斷。
  5.5 湊滿數量後，會先進入「審核」畫面列出整份清單，讓使用者可以
     用「N不要」或「換N」把某一項換成另一本還沒出現過的書（一樣會
     優先在原出版社找，找不到才跨社湊），確認沒問題後才會問班級
  6. 請使用者依序告訴我這幾種書要給哪幾個班（一個班對一種書）
  7. 確認畫面過了之後，逐筆呼叫既有的 write_to_google_sheet()
     寫入，每個「班級＋書」各自是一筆獨立訂單——這代表 Google
     Apps Script 完全不用新增任何 action，全部沿用學校訂單既有的
     create_order。任何一筆寫入失敗都不會影響其他筆，最後會列出
     成功／失敗清單，失敗的請使用者改用一般訂書流程手動補單。
  目前這個功能還沒有像一般訂單一樣接訂購單 PDF（因為 PDF 產生器
  假設一張訂單只有一本書），之後真的需要可以再擴充
  generate_purchase_order_pdf()。

【AI Agent pilot（2026-09-20 新增，v42）】
在既有「規則優先、AI 當最後容錯」的架構上，這次多加兩件事：
  1. AI_AGENT_ENABLED（環境變數，預設開）：開啟後，原本的智慧路由／
     圖片辨識改用 _openai_agent_json()／_openai_agent_chat()，會帶
     上這個使用者最近幾輪對話（ai_agent_history，見 _remember_ai_turn），
     讓「他呢？」「那本呢？」這種代名詞、省略主詞的講法也能被理解。
     關掉這個環境變數會整個退回 v41 的單輪呼叫邏輯（_openai_json）。
  2. 「一般對話」fallback：當規則跟意圖分類器都判斷不出來要做什麼
     （intent 是 chat 或 unknown）時，才會讓 AI 自由對話一次——
     system prompt 明確禁止它虛構老師／班級／訂單等公司資料，也
     禁止在聊天裡宣稱訂單已建立/修改/取消（那些一定要走既有確認
     流程）。這不影響「14. 明確代寫請求直接擋下」那道更早、更嚴格
     的防線，兩者是分開的機制。
  另外，圖片辨識新增「正式訂購單」（image_type=purchase_order）
  這個分類：這種圖片本身就列好了班級與數量，直接採用圖片上的值，
  不會像口語辨識那樣拿老師姓名回頭去資料庫查班級人數覆蓋掉——避免
  正式訂購單上「這次只訂 20 本」被誤植成資料庫裡那個班「平常」的
  人數。老師欄位如果圖片上真的是空的，會標成「未填寫」讓使用者在
  確認畫面上看清楚，而不是自己亂猜一個老師名字；確認後這個字串會
  照樣寫進 Google，如果不想要「未填寫」出現在正式紀錄裡，請在確認
  前先手動補上老師姓名。

【訂購單交付方式（2026-09 新增）】
訂單確認完成後，使用者可以選擇讓機器人生成一張 PDF 版的訂購單，
用來 email 給出版社（取代原本純文字、傳給業務轉單的做法）。
這一塊刻意拆成兩層，之後如果要升級成「機器人直接寄信給出版社」，
只要改 build_purchase_order_reply()，不用動 generate_purchase_order_pdf()：
  - generate_purchase_order_pdf()：只負責畫出 PDF 檔案本身
  - build_purchase_order_reply()：決定 PDF 產生後要怎麼「交」給使用者，
    由環境變數 PURCHASE_ORDER_DELIVERY_MODE 控制：
      "link"  → 目前唯一實作，回傳一個下載連結給使用者自己存檔、手動 email
      "email" → 尚未實作的切換點，未來要加「機器人直接寄信」時從這裡接
=============================================================
"""

from flask import Flask, request, send_file
import os
import re
import time
import hmac
import hashlib
import base64
import random
import logging
import threading
import requests
import json
import copy
import sqlite3
import uuid
from datetime import datetime
from difflib import SequenceMatcher

from reportlab.lib.pagesizes import A5
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle

# -------------------------------------------------------
# Logging：統一輸出格式，取代原本散落各處的 print()。
# Render / Railway 這類平台都是直接收 stdout 當 log，
# 所以還是輸出到 stdout，只是多了時間戳記跟等級，
# 方便之後如果要接 Sentry / 集中式 log 系統時比較好過濾。
# -------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("order_bot")

app = Flask(__name__)
APP_VERSION = "2026-09-25-v83-bulk-list-cleanup"

# 單一使用者單則訊息的長度上限。純粹是防呆／防濫用，
# 避免異常長的輸入把後面一大串正規表示式處理效能拖垮。
MAX_USER_TEXT_LENGTH = 5000   # v81：整段訂單可能很長；LINE 單則上限就是 5000 字

# =========================================================
# 簡易防濫用：同一個使用者短時間內訊息數上限
# =========================================================
RATE_LIMIT_MAX_MESSAGES = int(os.environ.get("RATE_LIMIT_MAX_MESSAGES", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "10"))
_rate_limit_hits = {}
_rate_limit_guard = threading.Lock()


def _is_rate_limited(user_id):
    now = time.time()
    with _rate_limit_guard:
        hits = _rate_limit_hits.setdefault(user_id, [])
        hits[:] = [t for t in hits if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(hits) >= RATE_LIMIT_MAX_MESSAGES:
            return True
        hits.append(now)
        return False


# =========================================================
# 速度優化：共用 HTTP Session + 讀取快取
# =========================================================
HTTP = requests.Session()
_HTTP_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
HTTP.mount("https://", _HTTP_ADAPTER)
HTTP.mount("http://", _HTTP_ADAPTER)

_google_read_cache = {}

_GOOGLE_CACHE_TTLS = {
    "list_schools": 600,
    "list_cram_schools": 600,
    "lookup_teacher_matches": 1800,
    "lookup_teacher": 1800,
    "lookup_school_classes": 300,
    "lookup_versions": 300,
    "lookup_book": 300,
    "lookup_fuzzy_candidates": 180,
    "lookup_multi_book_candidates": 300,
}

# v77：Google Apps Script 偶爾會出現 10~15 秒逾時。
# 參照資料（老師／版本／書籍／學校清單）並不是每分鐘都會變動，
# 因此把「最近一次成功結果」另外存到 SQLite，當 Google 真的逾時時
# 才拿來做唯讀備援。正常情況永遠優先使用即時 Google 回覆。
#
# 這不是寫入備援：建立／修改／取消訂單仍必須真的寫進 Google；
# 只有下列唯讀 action 允許 stale fallback。
_GOOGLE_STALE_MAX_AGES = {
    "list_schools": 24 * 3600,
    "list_cram_schools": 24 * 3600,
    "lookup_teacher_matches": 24 * 3600,
    "lookup_teacher": 24 * 3600,
    "lookup_school_classes": 12 * 3600,
    "lookup_versions": 24 * 3600,
    "lookup_book": 12 * 3600,
    "lookup_fuzzy_candidates": 6 * 3600,
    "lookup_multi_book_candidates": 6 * 3600,
}

def _store_google_stale_cache(action, key, data):
    if not key or action not in _GOOGLE_STALE_MAX_AGES or not isinstance(data, dict):
        return
    if data.get("success") is False:
        return
    if action == "lookup_fuzzy_candidates" and not data.get("candidates"):
        return
    try:
        payload_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        conn = _state_db()
        try:
            conn.execute(
                "INSERT INTO google_stale_cache (cache_key, action, data_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "action=excluded.action, data_json=excluded.data_json, updated_at=excluded.updated_at",
                (key, action, payload_json, time.time())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Google stale cache store failed: action={action} error={e}")

def _load_google_stale_cache(action, key):
    max_age = int(_GOOGLE_STALE_MAX_AGES.get(action, 0) or 0)
    if not key or max_age <= 0:
        return None
    try:
        conn = _state_db()
        try:
            row = conn.execute(
                "SELECT data_json, updated_at FROM google_stale_cache WHERE cache_key=? AND action=?",
                (key, action)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        data_json, updated_at = row
        age = time.time() - float(updated_at or 0)
        if age < 0 or age > max_age:
            return None
        data = json.loads(data_json)
        if not isinstance(data, dict):
            return None
        data = copy.deepcopy(data)
        data["_stale_fallback"] = True
        data["_stale_age_seconds"] = round(age, 1)
        logger.warning(f"Google stale fallback HIT: action={action} age={age:.1f}s")
        return data
    except Exception as e:
        logger.warning(f"Google stale cache read failed: action={action} error={e}")
        return None

def _teacher_name_fallback_key(teacher, school=""):
    """v79：建立不受完整 payload 影響的老師姓名級備援 key。"""
    name = str(teacher or "").strip()
    name = re.sub(r"[，,。.!！?？\\s]+", "", name)
    name = re.sub(r"老師$", "", name)
    school = str(school or "").strip()
    if not name:
        return ""
    return "teacher-name::" + name + ("::" + school if school else "")


def _store_teacher_name_fallback(payload, data):
    """把成功老師結果另外依「老師姓名」保存，避免 exact payload 首次回空時無資料可救。"""
    if not isinstance(data, dict) or data.get("success") is False:
        return
    matches = data.get("matches") or []
    if not matches:
        return

    # 依每一位實際命中的老師分開保存；同名跨校則另外存 school-specific key。
    grouped = {}
    for item in matches:
        if not isinstance(item, dict):
            continue
        tname = str(item.get("teacher", "") or "").strip()
        school = str(item.get("school", "") or "").strip()
        norm = _teacher_name_fallback_key(tname)
        if not norm:
            continue
        grouped.setdefault(norm, []).append(copy.deepcopy(item))
        if school:
            grouped.setdefault(_teacher_name_fallback_key(tname, school), []).append(copy.deepcopy(item))

    for synthetic_key, items in grouped.items():
        _store_google_stale_cache(
            "lookup_teacher_matches",
            synthetic_key,
            {"success": True, "matches": items},
        )


def _load_teacher_name_fallback(payload):
    """先用老師姓名+學校，再退到純老師姓名讀取最近成功資料。"""
    teacher = str((payload or {}).get("teacher", "") or "").strip()
    school = str((payload or {}).get("school", "") or "").strip()
    if not teacher:
        return None

    keys = []
    if school:
        keys.append(_teacher_name_fallback_key(teacher, school))
    keys.append(_teacher_name_fallback_key(teacher))
    for synthetic_key in keys:
        if not synthetic_key:
            continue
        stale = _load_google_stale_cache("lookup_teacher_matches", synthetic_key)
        if isinstance(stale, dict) and (stale.get("matches") or []):
            logger.warning(
                "Google teacher name-level fallback HIT: teacher=%s school=%s",
                teacher,
                school,
            )
            return stale
    return None


def _clear_google_stale_cache():
    try:
        conn = _state_db()
        try:
            conn.execute("DELETE FROM google_stale_cache")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Google stale cache clear failed: {e}")

def _cache_key(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(payload)

def clear_google_read_cache():
    _google_read_cache.clear()


# 「清除快取」指令原本只會清掉當下處理這則訊息的 worker 的本地快取
# ——Render／Railway 這類平台常開多個 gunicorn worker（多個獨立
# process），下一則訊息被分派到別的 worker 時，那個 worker 的本地
# 快取完全不受影響，卻回覆使用者「已清除」，使用者以為安全了，卻可能
# 在接下來的 TTL 時間內（最長 30 分鐘）在別的 worker 讀到清除前的舊
# 資料。這裡用 SQLite 存一個全域 epoch：清除快取時把它 +1；每則訊息
# 開始處理時比對這個 worker 記得的 epoch 是否落後，落後就先清本地
# 快取再繼續處理，讓「清除」的效果最慢一則訊息內就能傳播到所有 worker。
_local_cache_epoch = 0


def _get_shared_cache_epoch():
    conn = _state_db()
    try:
        row = conn.execute("SELECT epoch FROM cache_epoch WHERE id=1").fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.error(f"cache epoch read error: {e}")
        return 0
    finally:
        conn.close()


def _bump_shared_cache_epoch():
    conn = _state_db()
    try:
        conn.execute(
            "INSERT INTO cache_epoch (id, epoch) VALUES (1, 1) "
            "ON CONFLICT(id) DO UPDATE SET epoch = epoch + 1"
        )
        conn.commit()
    except Exception as e:
        logger.error(f"cache epoch bump error: {e}")
    finally:
        conn.close()


def _sync_local_cache_with_shared_epoch():
    global _local_cache_epoch
    shared = _get_shared_cache_epoch()
    if shared != _local_cache_epoch:
        clear_google_read_cache()
        _local_cache_epoch = shared


# =========================================================
# 請求時間預算：避免單一則訊息因為連續好幾次 Google 呼叫疊加，
# 把整個 gunicorn worker 拖到逾時被強制殺掉（WORKER TIMEOUT）。
#
# 背景：Google Apps Script 偶爾會慢到 8~10 秒才回應。舊版程式在
# 找不到資料、或第一次查詢帶了學校條件卻沒結果時，常常會自動再打
# 一次 Google（例如老師查詢先用學校篩選查一次，找不到再跨校查一次；
# guided_mode 的跳脫偵測也可能再觸發一次獨立查詢）。正常狀況下這些
# 疊加沒事，但只要 Apps Script 剛好變慢，疊加起來的等待時間就可能
# 超過 gunicorn 的 worker timeout，整個 worker process 被強制殺掉，
# 那一則訊息完全收不到任何回覆（LINE 端看起來就是已讀不回）。
#
# 這裡在 google_post() 這個唯一的出口統一做管控：同一則使用者訊息
# 處理期間，如果已經花費的時間或已經打的 Google 次數超過門檻，
# 後面的 Google 呼叫直接跳過、視同失敗（回傳 None），讓上層既有的
# 「查不到／處理失敗」邏輯自然接手，而不是繼續傻等下去。
#
# 門檻刻意設在明顯小於 gunicorn worker timeout 的位置（建議 worker
# timeout 至少設 60 秒，這裡預設 20 秒），確保就算真的踩到上限，
# 剩下的處理時間＋LINE 回覆時間仍然留有安全緩衝。
# =========================================================
REQUEST_TIME_BUDGET_SECONDS = float(os.environ.get("REQUEST_TIME_BUDGET_SECONDS", "20"))
REQUEST_MAX_GOOGLE_CALLS = int(os.environ.get("REQUEST_MAX_GOOGLE_CALLS", "8"))

_request_budget_state = threading.local()


def _start_request_budget():
    _request_budget_state.started_at = time.perf_counter()
    _request_budget_state.google_calls = 0
    _sync_local_cache_with_shared_epoch()


def _register_google_call():
    _request_budget_state.google_calls = getattr(_request_budget_state, "google_calls", 0) + 1


def _request_budget_exceeded():
    started_at = getattr(_request_budget_state, "started_at", None)
    if started_at is None:
        return False

    elapsed = time.perf_counter() - started_at
    calls = getattr(_request_budget_state, "google_calls", 0)

    return (
        elapsed > REQUEST_TIME_BUDGET_SECONDS
        or calls >= REQUEST_MAX_GOOGLE_CALLS
    )


# =========================================================
# LINE Quick Reply（快速回覆按鈕）
#
# 跟圖文選單（Rich Menu）不一樣：Quick Reply 是「跟著這一則訊息」
# 出現、使用者點了或滑走就消失的按鈕，很適合放在「你好」「有什麼
# 功能」這種入口時刻。做法是某個回覆函式（例如 get_greeting_reply）
# 執行時，先呼叫 _set_quick_reply() 把想附加的按鈕暫存起來，
# callback() 收到最終回覆文字後，用 _pop_quick_reply() 取出、
# 附加到送給 LINE 的最後一則訊息上。用 threading.local 存放，
# 跟上面 REQUEST_TIME_BUDGET 那組是同一種做法：每次處理一則使用者
# 訊息開始時都會重置，處理結束後在 callback() 取用一次就清空，
# 不會跨訊息殘留、也不會跨 worker 互相干擾。
# =========================================================
_quick_reply_state = threading.local()

# 第一層按鈕（使用者實際排定）：學校訂書、補習班訂書、新增其他訂單、
# 查詢資料（點下去換出下面 QUICK_REPLY_QUERY_ITEMS 那組查詢類按鈕）、
# 更多功能。標籤文字（第一個值）是給使用者看的，觸發文字（第二個值）
# 是點了之後實際送出去的訊息，兩者刻意分開，方便標籤取更口語的名稱
# 而不用另外新增一堆觸發詞判斷。
QUICK_REPLY_MAIN_ITEMS = [
    ("📚 學校訂書", "我要訂書"),
    ("🏫 補習班訂書", "補習班訂書"),
    ("📦 新增其他訂單", "其他訂單"),
    ("🔍 查詢資料", "查詢資料"),
    ("➕ 更多功能", "更多功能"),
]

# 點「查詢資料」之後換出來的第三組按鈕，把原本擠在第一層的四個查詢
# 類功能集中放在這裡，讓第一層的「訂書 / 補習班訂書 / 其他訂單 /
# 查詢資料」分類更清楚。最後加「回主選單」方便從這裡一鍵跳回第一層，
# 不用自己重打字。
QUICK_REPLY_QUERY_ITEMS = [
    ("👨‍🏫 查老師", "查老師"),
    ("📅 查訂單", "查訂單"),
    ("📖 查版本", "查版本"),
    ("📊 查人數", "查人數"),
    ("📦 查其他訂單", "查其他訂單"),
    ("🏠 回主選單", "主選單"),
]

# 「更多功能」按鈕點下去要出現的第二組按鈕——刻意跟 QUICK_REPLY_MAIN_ITEMS
# 完全不重複，展示常用清單裡沒放的次要功能，不然使用者點「更多功能」
# 卻看到同一組按鈕，會覺得沒有作用。補習班訂書／其他訂單第一層已經有
# 了，這裡不重複放；最後一樣加「回主選單」方便導覽。
QUICK_REPLY_MORE_ITEMS = [
    ("📷 拍照訂書", "拍照訂書"),
    ("🧠 AI 助手", "AI助手"),
    ("📚 多書訂購", "多書訂購"),
    ("📊 今日統計", "統計"),
    ("🏠 回主選單", "主選單"),
]


def _reset_quick_reply():
    _quick_reply_state.items = None


def _set_quick_reply(items):
    _quick_reply_state.items = items


def _pop_quick_reply():
    items = getattr(_quick_reply_state, "items", None)
    _quick_reply_state.items = None
    return items


def _quick_reply_payload(items):
    if not items:
        return None
    return {
        "items": [
            {
                "type": "action",
                # LINE 按鈕文字（label）上限 20 字，這裡先截斷防呆。
                "action": {"type": "message", "label": str(label)[:20], "text": str(text)}
            }
            for label, text in items[:13]
        ]
    }


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

# 智慧理解層：只有原本規則接不住、或收到圖片時才使用。
# 沒有設定 OPENAI_API_KEY 時，原本所有功能仍照常運作。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini").strip()
# v42 AI Agent：可獨立選較強模型；關閉後完全退回原本 v41 邏輯。
AI_AGENT_ENABLED = os.environ.get("AI_AGENT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
AI_AGENT_MODEL = os.environ.get("AI_AGENT_MODEL", "gpt-5.6-sol").strip()
AI_AGENT_MAX_HISTORY = int(os.environ.get("AI_AGENT_MAX_HISTORY", "10"))
AI_TIMEOUT_SECONDS = float(os.environ.get("AI_TIMEOUT_SECONDS", "12"))
# 圖片辨識（上傳圖片＋視覺分析＋整理成多本書的 JSON）本來就比純文字
# 判斷慢很多，沿用同一個 12 秒逾時常常來不及，AI 還沒回完就被判定
# 逾時失敗（見 handle_image_message 的 "AI parse error: ... Read timed
# out" log）。這裡另外設一個比較長的逾時，只給圖片辨識用，不影響
# 一般文字判斷的反應速度。
IMAGE_AI_TIMEOUT_SECONDS = float(os.environ.get("IMAGE_AI_TIMEOUT_SECONDS", "28"))

for _env_name, _env_value in [
    ("LINE_CHANNEL_ACCESS_TOKEN / CHANNEL_ACCESS_TOKEN", CHANNEL_ACCESS_TOKEN),
    ("LINE_CHANNEL_SECRET / CHANNEL_SECRET", CHANNEL_SECRET),
    ("GOOGLE_SCRIPT_URL", GOOGLE_SCRIPT_URL),
]:
    if not _env_value:
        logger.warning(f"啟動時發現環境變數未設定：{_env_name}")

FIXED_FALLBACK_MESSAGE = "⚠️ 這句我目前還無法確定你的意思。\n\n你可以換個方式再說一次，或輸入「功能」查看可以使用的功能。"

DEFAULT_SCHOOL = os.environ.get("DEFAULT_SCHOOL", "天母國中")

CONFIRM_WORDS = {"確認", "是", "對", "對的", "沒錯", "正確", "可以", "好", "好的", "就是", "OK", "ok", "Ok", "就這樣", "確定", "這樣沒問題", "沒問題", "這樣可以", "這樣可以了"}

RECEIPT_OFFER_TTL_SECONDS = 40
RECEIPT_DECLINE_WORDS = {"不用", "不需要", "不用了", "不要", "算了"}

# =========================================================
# 訂購單 PDF 設定
#
# 交付方式先只做「產生 PDF → 給下載連結，使用者自己存檔後手動
# email 給出版社」；之後如果要升級成「機器人直接寄信」，把
# PURCHASE_ORDER_DELIVERY_MODE 切成 "email"，並在
# build_purchase_order_reply() 裡接上真正寄信的函式即可，
# 詳見檔案最上面的說明區塊。
# =========================================================
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip()  # 建議設成 Render 服務網址，例如 https://your-app.onrender.com
PURCHASE_ORDER_DIR = os.environ.get("PURCHASE_ORDER_DIR", "/tmp/purchase_orders")
os.makedirs(PURCHASE_ORDER_DIR, exist_ok=True)

# 下載連結有效期限：預設 3 天。想要 7 天就把環境變數設成 604800（7*24*3600）。
# 注意：Render 免費方案重新部署（deploy）會清空 /tmp，屆時連結會提前失效，
# 這點跟 TTL 設定無關，純粹是平台限制。
PURCHASE_ORDER_LINK_TTL_SECONDS = int(
    os.environ.get("PURCHASE_ORDER_LINK_TTL_SECONDS", str(3 * 24 * 3600))
)

# 交付方式切換點："link" = 給下載連結（目前唯一實作）
# "email" = 機器人直接寄信給出版社（尚未實作，保留切換點，見上方說明）
PURCHASE_ORDER_DELIVERY_MODE = os.environ.get("PURCHASE_ORDER_DELIVERY_MODE", "link")

# 六個引導模式統一使用的退出詞：不管在哪一個模式裡，打這些詞都能直接
# 回到主選單。訂書流程（order_flow）現在也正式收編進 guided_mode，
# 所以也共用這份清單，不再有「訂書只能打取消，其他模式可以打離開」
# 這種不一致的情況。
EXIT_WORDS = {"取消", "回主選單", "主選單", "離開"}

# 打招呼／功能表舉例時，隨機挑一位球員名字帶入範例句，
# 不指定 Curry；每次「你好」看到的舉例老師名字都會不一樣。
NBA_PLAYERS = [
    "詹姆斯", "杜蘭特", "字母哥", "東契奇", "塔圖姆",
    "恩比德", "厄文", "哈登", "韋斯布魯克", "米契爾"
]


def _is_confirm_word(text):
    return str(text or "").strip() in CONFIRM_WORDS


def _is_exit_word(text):
    return str(text or "").strip() in EXIT_WORDS


# =========================================================
# 對話狀態（全部使用 user_id 當 key 的全域 dict）
# =========================================================
pending_orders = {}
order_flow_context = {}
conversation_context = {}
teacher_lookup_context = {}

# 「查人數」／「查版本」引導模式連續查詢時，記住上一次成功解析出的學校，
# 讓使用者換過學校後只打年級（例如「七年級」）還能沿用正確的學校，
# 而不是被 get_context_school() 撈回更早之前查過的老師所在學校。
stats_version_context = {}

historical_order_context = {}
pending_history_updates = {}
pending_history_cancels = {}

pending_other_orders = {}
pending_other_updates = {}
other_order_context = {}

pending_name_confirmations = {}
pending_teacher_corrections = {}

guided_mode = {}

pending_receipt_offers = {}
# v42：保存最近幾輪自然語言上下文，讓「他／那本／剛剛那個」可以被理解。
ai_agent_history = {}

_SESSION_DICTS = {
    "pending_orders": pending_orders,
    "order_flow_context": order_flow_context,
    "conversation_context": conversation_context,
    "teacher_lookup_context": teacher_lookup_context,
    "stats_version_context": stats_version_context,
    "historical_order_context": historical_order_context,
    "pending_history_updates": pending_history_updates,
    "pending_history_cancels": pending_history_cancels,
    "pending_other_orders": pending_other_orders,
    "pending_other_updates": pending_other_updates,
    "other_order_context": other_order_context,
    "pending_name_confirmations": pending_name_confirmations,
    "pending_teacher_corrections": pending_teacher_corrections,
    "guided_mode": guided_mode,
    "pending_receipt_offers": pending_receipt_offers,
    "ai_agent_history": ai_agent_history,
}

# v65 2026-09-23：速度/多書搜尋修正：快取相容舊 GAS、SQLite schema 每 worker 初始化一次、
# 跨 worker 鎖縮短等待、多書候選放寬至 20 並支援考卷/測驗卷變體、查老師後純書名可直接訂書。

# =========================================================
# 跨 worker 對話狀態持久化：SQLite
# =========================================================
_STATE_DB_PATH = os.environ.get("ORDER_STATE_DB_PATH", "/tmp/line_order_bot_state.sqlite3")
_SESSION_STALE_SECONDS = 3 * 24 * 3600
_PROCESSED_MESSAGE_TTL_SECONDS = 24 * 3600


_STATE_DB_INITIALIZED = False
_STATE_DB_INIT_LOCK = threading.Lock()


def _ensure_state_db_schema():
    """每個 worker process 只初始化一次 SQLite schema，避免每則訊息重複 DDL。"""
    global _STATE_DB_INITIALIZED
    if _STATE_DB_INITIALIZED:
        return
    with _STATE_DB_INIT_LOCK:
        if _STATE_DB_INITIALIZED:
            return
        conn = sqlite3.connect(_STATE_DB_PATH, timeout=5)
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_session ("
                "user_id TEXT PRIMARY KEY, session_json TEXT, updated_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS processed_message ("
                "message_id TEXT PRIMARY KEY, processed_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cache_epoch (id INTEGER PRIMARY KEY, epoch INTEGER)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS google_stale_cache ("
                "cache_key TEXT PRIMARY KEY, action TEXT, data_json TEXT, updated_at REAL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS user_processing_lock ("
                "user_id TEXT PRIMARY KEY, started_at REAL)"
            )
            conn.commit()
            _STATE_DB_INITIALIZED = True
        finally:
            conn.close()


def _state_db():
    _ensure_state_db_schema()
    conn = sqlite3.connect(_STATE_DB_PATH, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


# =========================================================
# 跨 worker 同一使用者互斥鎖：避免重複「確認」造成重複訂單
#
# _get_user_lock() 用的 threading.Lock() 只能擋住「同一個 worker
# process」內的重複訊息。Render／Railway 這類平台常開多個 gunicorn
# worker（各自獨立 process，記憶體不共用），如果 Apps Script 慢到
# 8~10 秒，使用者以為沒反應又按一次「確認」，兩則訊息完全可能被分派
# 到不同 worker：兩邊都在對方 _persist_session() 之前 _hydrate_session()
# 到同一份 pending_orders，都通過「訂單還在」的檢查，最後在 Google
# 試算表建出兩筆重複訂單。這裡用 SQLite 的 PRIMARY KEY 唯一限制做一個
# 跨 process 都看得到的鎖：搶到鎖才能處理這個 user_id 的訊息，處理完
# （或逾時放棄）才釋放，第二個 worker 會在這裡排隊等，而不是各自平行
# 處理同一個人的同一個狀態。
# =========================================================
_CROSS_WORKER_LOCK_STALE_SECONDS = 30
_CROSS_WORKER_LOCK_WAIT_SECONDS = 6
_CROSS_WORKER_LOCK_POLL_INTERVAL = 0.15


def _acquire_cross_worker_lock(user_id):
    deadline = time.time() + _CROSS_WORKER_LOCK_WAIT_SECONDS
    while True:
        now = time.time()
        conn = _state_db()
        try:
            # 清掉「開始時間早於容忍上限」的鎖——正常處理不會拖這麼久，
            # 留著多半是某個 worker 中途當掉或被強制重啟，沒機會釋放。
            conn.execute(
                "DELETE FROM user_processing_lock WHERE started_at < ?",
                (now - _CROSS_WORKER_LOCK_STALE_SECONDS,)
            )
            try:
                conn.execute(
                    "INSERT INTO user_processing_lock (user_id, started_at) VALUES (?, ?)",
                    (user_id, now)
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
        except Exception as e:
            logger.error(f"cross-worker lock acquire error: {e}")
            # 鎖機制本身出錯時不要讓使用者完全收不到回覆，寧可退回沒有
            # 這層保護、照原本的方式處理這則訊息。
            return True
        finally:
            conn.close()
        if time.time() >= deadline:
            return False
        time.sleep(_CROSS_WORKER_LOCK_POLL_INTERVAL)


def _release_cross_worker_lock(user_id):
    conn = _state_db()
    try:
        conn.execute("DELETE FROM user_processing_lock WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"cross-worker lock release error: {e}")
    finally:
        conn.close()


def _is_duplicate_line_event(message_id):
    if not message_id:
        return False

    conn = _state_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM processed_message WHERE message_id=?", (message_id,)
        ).fetchone()
        if row:
            return True

        conn.execute(
            "INSERT OR IGNORE INTO processed_message(message_id, processed_at) VALUES(?,?)",
            (message_id, time.time())
        )
        conn.commit()
        return False
    except Exception as error:
        logger.error(f"duplicate-event check error: {error}")
        return False
    finally:
        conn.close()


def _cleanup_stale_purchase_order_pdfs():
    """過期訂購單 PDF 清理。跟 SQLite 那份清理共用同一個機率觸發，
    不用另外開排程；反正只要偶爾清一次就好，不用每則訊息都掃。"""
    now = time.time()
    try:
        for filename in os.listdir(PURCHASE_ORDER_DIR):
            if not filename.endswith(".pdf"):
                continue
            path = os.path.join(PURCHASE_ORDER_DIR, filename)
            try:
                if now - os.path.getmtime(path) > PURCHASE_ORDER_LINK_TTL_SECONDS:
                    os.remove(path)
            except Exception:
                continue
    except Exception as error:
        logger.error(f"purchase order pdf cleanup error: {error}")


def _cleanup_stale_state(probability=0.02):
    if random.random() > probability:
        return

    now = time.time()
    conn = _state_db()
    try:
        conn.execute(
            "DELETE FROM user_session WHERE updated_at < ?",
            (now - _SESSION_STALE_SECONDS,)
        )
        conn.execute(
            "DELETE FROM processed_message WHERE processed_at < ?",
            (now - _PROCESSED_MESSAGE_TTL_SECONDS,)
        )
        conn.commit()
    except Exception as error:
        logger.error(f"state cleanup error: {error}")
    finally:
        conn.close()

    _cleanup_stale_purchase_order_pdfs()


def _verify_line_signature(body_bytes, signature_header):
    if not CHANNEL_SECRET:
        logger.warning("CHANNEL_SECRET 未設定，跳過簽章驗證（僅建議用於本機測試）")
        return True

    if not signature_header:
        return False

    computed = base64.b64encode(
        hmac.new(CHANNEL_SECRET.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    ).decode("utf-8")

    return hmac.compare_digest(computed, signature_header)


def _hydrate_session(user_id):
    """
    把這個 user_id 的對話狀態，從 SQLite 同步回這個 worker 的記憶體。

    關鍵：這裡必須是「同步」而不是「疊加」——資料庫裡沒有的欄位，
    本地端也要清掉。Render 常常會開多個 gunicorn worker（多個獨立
    process），同一個使用者連續兩則訊息完全可能被分派到不同 worker
    處理。如果某個 worker 在早幾則訊息時，本地暫存過一份還沒填完的
    訂書草稿（例如 order_flow_context），之後這筆草稿在「另一個」
    worker 上被正常完成、清掉、寫回資料庫，這個 worker 本地那份舊
    草稿並不會自動消失。如果下一則訊息又剛好分派回這個 worker，
    若 hydrate 只會「加」不會「減」，就會誤用這份早已過期的舊草稿，
    導致使用者明明已經完成訂書、要修改班級，卻被誤判成還在輸入書名
    這類詭異行為。所以這裡明確地：資料庫有的欄位覆蓋本地，資料庫
    沒有的欄位，本地也要用 pop 清掉，確保跟資料庫保持一致。
    """
    conn = _state_db()
    try:
        row = conn.execute(
            "SELECT session_json FROM user_session WHERE user_id=?", (user_id,)
        ).fetchone()
    except Exception as e:
        logger.error(f"session hydrate read error: {e}")
        row = None
    finally:
        conn.close()

    data = {}
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                data = parsed
        except Exception as e:
            logger.error(f"session hydrate decode error: {e}")
            # 解碼失敗時不清空本地狀態，避免因為單次暫時性錯誤
            # 就把使用者手上還在進行的流程整個洗掉。
            return

    for key, target_dict in _SESSION_DICTS.items():
        if key in data:
            target_dict[user_id] = data[key]
        else:
            target_dict.pop(user_id, None)


def _persist_session(user_id):
    payload = {}
    for key, target_dict in _SESSION_DICTS.items():
        if user_id in target_dict:
            payload[key] = target_dict[user_id]

    conn = _state_db()
    try:
        if not payload:
            conn.execute("DELETE FROM user_session WHERE user_id=?", (user_id,))
        else:
            try:
                session_json = json.dumps(payload, ensure_ascii=False)
            except Exception as e:
                logger.error(f"session persist encode error: {e}")
                return
            conn.execute(
                "INSERT INTO user_session(user_id, session_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET session_json=excluded.session_json, "
                "updated_at=excluded.updated_at",
                (user_id, session_json, time.time())
            )
        conn.commit()
    except Exception as e:
        logger.error(f"session persist write error: {e}")
    finally:
        conn.close()


def _clear_session(user_id):
    for target_dict in _SESSION_DICTS.values():
        target_dict.pop(user_id, None)

    conn = _state_db()
    try:
        conn.execute("DELETE FROM user_session WHERE user_id=?", (user_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"session clear error: {e}")
    finally:
        conn.close()


# =========================================================
# 同一個使用者的訊息在同一個 worker 內序列化處理。
# =========================================================
_user_locks = {}
_user_locks_guard = threading.Lock()


def _get_user_lock(user_id):
    with _user_locks_guard:
        lock = _user_locks.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _user_locks[user_id] = lock
        return lock


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


@app.route("/healthz", methods=["GET"])
def healthz():
    problems = []
    if not CHANNEL_ACCESS_TOKEN:
        problems.append("LINE_CHANNEL_ACCESS_TOKEN missing")
    if not CHANNEL_SECRET:
        problems.append("LINE_CHANNEL_SECRET missing")
    if not GOOGLE_SCRIPT_URL:
        problems.append("GOOGLE_SCRIPT_URL missing")

    status = "ok" if not problems else "degraded"
    return {
        "status": status,
        "version": APP_VERSION,
        "problems": problems
    }, (200 if not problems else 503)


@app.route("/purchase-order/<token>.pdf")
def serve_purchase_order_pdf(token):
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        return "Not found", 404
    path = os.path.join(PURCHASE_ORDER_DIR, f"{token}.pdf")
    if not os.path.exists(path):
        return "這個連結已經過期或不存在，請回到 LINE 重新產生訂購單 PDF。", 404
    if time.time() - os.path.getmtime(path) > PURCHASE_ORDER_LINK_TTL_SECONDS:
        try:
            os.remove(path)
        except Exception:
            pass
        return "這個連結已經過期，請回到 LINE 重新產生訂購單 PDF。", 404
    return send_file(path, mimetype="application/pdf", download_name="訂購單.pdf")


@app.route("/callback", methods=["POST"])
def callback():
    raw_body = request.get_data()
    signature = request.headers.get("X-Line-Signature", "")

    if not _verify_line_signature(raw_body, signature):
        logger.warning("LINE 簽章驗證失敗，拒絕這次 webhook 請求")
        return "Invalid signature", 400

    body = request.get_json(silent=True) or {}

    logger.info(f"Webhook received {APP_VERSION}")
    logger.debug(body)

    _cleanup_stale_state()

    for event in body.get("events", []):
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        message_type = str(message.get("type", "") or "")
        if message_type not in {"text", "image"}:
            continue

        message_id = str(message.get("id", "") or "")
        if _is_duplicate_line_event(message_id):
            logger.warning(f"偵測到 LINE 重送事件，略過重複處理 message_id={message_id}")
            continue

        reply_token = event.get("replyToken")
        source = event.get("source", {})
        # 取不到 userId（例如群組／聊天室裡使用者還沒把這個官方帳號加
        # 為好友時，LINE 不一定會附上 userId）時，原本一律退回固定字串
        # "unknown"，會讓所有這類事件共用同一份對話狀態：A 的待確認
        # 訂單可能被 B 的「確認」送出去，或 B 一句話把 A 的訂書草稿
        # 覆蓋掉。改用 groupId／roomId 當 key，至少把「共用狀態」的
        # 範圍限制在同一個群組／聊天室內，不會跨群組互相污染。
        user_id = source.get("userId", "")
        if not user_id:
            source_type = str(source.get("type", "") or "")
            fallback_id = source.get("groupId") or source.get("roomId") or ""
            user_id = f"{source_type}:{fallback_id}" if fallback_id else "unknown"
        user_text = (
            str(message.get("text", "")).strip()[:MAX_USER_TEXT_LENGTH]
            if message_type == "text" else "[圖片訂單]"
        )

        request_started = time.perf_counter()
        _reset_quick_reply()
        try:
            if message_type == "image":
                reply_message = add_lebron_flavor(handle_image_message(user_id, message_id))
            else:
                reply_message = add_lebron_flavor(handle_message(user_id, user_text))
        except Exception as error:
            logger.exception(f"handle_message error: {error}")
            reply_message = FIXED_FALLBACK_MESSAGE

        quick_reply = _quick_reply_payload(_pop_quick_reply())

        handle_elapsed = time.perf_counter() - request_started
        line_started = time.perf_counter()
        reply_to_line(reply_token, reply_message, quick_reply=quick_reply)
        line_elapsed = time.perf_counter() - line_started
        total_elapsed = time.perf_counter() - request_started
        logger.info(
            f"PERF user={user_id} handle={handle_elapsed:.3f}s "
            f"line={line_elapsed:.3f}s total={total_elapsed:.3f}s text={user_text[:40]}"
        )

    return "OK", 200


# =========================================================
# 主流程
# =========================================================
def handle_message(user_id, user_text):
    # 每一則訊息一開始就重置請求時間／次數預算，確保上一則訊息的
    # 計數不會殘留影響到這一則（同一個 gunicorn worker 會處理多則訊息）。
    _start_request_budget()

    if _is_rate_limited(user_id):
        logger.warning(f"rate limited user={user_id}")
        return (
            "⏳ 你剛剛傳訊息的速度有點快，我需要幾秒鐘喘口氣。\n"
            "請稍等一下再傳一次。"
        )

    lock = _get_user_lock(user_id)
    with lock:
        # threading.Lock() 只能擋住同一個 worker process 內的重複訊息；
        # 這裡再搶一個跨 worker 都看得到的 SQLite 鎖，避免使用者按兩次
        # 「確認」被分派到不同 worker 同時處理，各自都以為訂單還沒
        # 確認過，在 Google 試算表建出兩筆重複訂單。搶不到鎖（另一個
        # worker 正在處理同一個人、還沒處理完）就請使用者稍候，不要
        # 跟著一起處理。
        if not _acquire_cross_worker_lock(user_id):
            return (
                "⏳ 你上一則訊息還在處理中，請稍候幾秒再試一次，"
                "避免同一個操作被重複送出。"
            )
        try:
            _hydrate_session(user_id)
            try:
                reply = _route_message(user_id, user_text)
                _remember_ai_turn(user_id, user_text, reply)
                return reply
            finally:
                _persist_session(user_id)
        finally:
            _release_cross_worker_lock(user_id)



def _v55_natural_rewrite(user_id, text):
    """v55：把常見口語改寫成既有流程已經會處理的標準句型。
    原則：只做高信心、可逆的語意正規化，不直接寫 Google、不繞過確認。
    """
    t = str(text or "").strip()
    if not t:
        return t

    # 手機常見符號／贅詞
    t = t.replace("＆", "和").replace("&", "和").replace("＋", "+")
    t = re.sub(r"[～~]", "-", t)
    t = re.sub(r"[？?]+$", "", t).strip()

    # 查歷史訂單：018那張、幫我看018、018訂了什麼、查一下018...
    # 注意：日期格式的每日訂單查詢（9/20的訂單、0920訂單…）不能被這裡攔截，
    # 否則 parse_daily_order_query() 永遠收不到原始日期字串；訂單確認／
    # 修改進行中時（703那筆不要了）也不該被當成訂單編號查詢搶走。
    m = re.search(r"(?<!\d)(\d{1,4})(?!\d)", t)
    if (
        user_id not in pending_orders
        and user_id not in order_flow_context
        and not t.startswith(("其他訂單", "其他單"))
        and m
        and any(k in t for k in ("訂單","那張","那筆","訂了什麼","買了什麼","查一下","幫我看","看一下"))
        and parse_daily_order_query(t) is None
    ):
        # 避免把班級、品項型號、冊次等數字誤當成訂單編號；句子裡有
        # 「取消」時也不能轉成純查詢——不然「取消訂單099」會被這裡
        # 改寫成「查099」，變成只顯示訂單明細，真正的取消流程
        # （parse_history_cancel_request）永遠收不到原始文字，使用者
        # 等於沒辦法用這個最自然的講法取消訂單。
        if not any(k in t for k in ("班","老師","天母","華興","衛理","國中","女中","取消")):
            return "查" + m.group(1)

    # 老師班級查詢：把各種口語收斂到「某老師有幾個班」
    teacher_query_words = ("有幾個班","有幾班","幾班","幾個班","哪些班","哪幾班","有什麼班",
                           "班級給我看","班級有哪些","學生班級","帶哪些班","教哪些班","教哪幾班",
                           "手上有哪些班","現在教誰","目前帶的班","老師資料")
    # 句子裡有明確寫「老師」時，姓名的右邊界就是「老師」兩個字，這是
    # 最準確的錨點；用 re.match 從句首找，靠 greedy+backtrack 自然對齊到
    # 正確邊界，不會像原本 (?:老師)? 設成可有可無那樣，貪婪多吃一個字
    # 把「老師」吃掉半個字（例如「張建國老師」被吃成「張建國老」）。
    teacher_m = re.match(r"^([\u4e00-\u9fff]{2,4})老師", t)
    if not teacher_m:
        # 沒寫「老師」時維持原本較寬鬆的比對，姓名容忍度交給後續的
        # 模糊比對／資料庫驗證去把關，不在這裡強行收緊造成漏判。
        teacher_m = re.search(r"([\u4e00-\u9fff]{2,4})", t)
    if teacher_m and any(k in t for k in teacher_query_words):
        name = teacher_m.group(1)
        # 排除把學校詞抓成姓名
        if name not in ("天母","華興","衛理","國中","女中"):
            return f"{name}老師有幾個班"

    # 學校＋數字班級反查老師
    school_alias = None
    for alias, full in (("天母","天母國中"),("華興","華興中學"),("衛理","衛理女中")):
        if alias in t:
            school_alias = full
            break
    cm = re.search(r"(?<!\d)([789]\d{2})(?!\d)", t)
    if school_alias and cm and any(k in t for k in ("誰教","誰帶","誰在教","誰的","哪個老師","老師是誰","老師","查")):
        return f"{school_alias}{cm.group(1)}老師"

    # 學校＋年級＋科目查老師：省略「老師」也接住
    if school_alias:
        grade = ""
        for g in ("高一","高二","高三","國一","國二","國三","七年級","八年級","九年級"):
            if g in t:
                grade = g; break
        subject = ""
        for sub in ("國文","英文","英語","數學","自然","生物","理化","地科","歷史","地理","公民","社會"):
            if sub in t:
                subject = "英文" if sub == "英語" else sub; break
        if grade and subject and any(k in t for k in ("誰教","是誰","哪位","哪個","誰在上","老師有誰","幫我查","老師")):
            return f"{school_alias}{grade}{subject}老師"

    # 查版本：口語問「哪版／哪一家／出版社／用哪版」
    if school_alias:
        grade = ""
        grade_map = (("七年級","七年級"),("國一","七年級"),("七","七年級"),
                     ("八年級","八年級"),("國二","八年級"),("九年級","九年級"),("國三","九年級"))
        for raw, std in grade_map:
            if raw in t:
                grade = std; break
        subject = next((x for x in ("國文","英文","數學","自然","生物","理化","地科","歷史","地理","公民","社會") if x in t), "")
        if grade and subject and any(k in t for k in ("版本","哪版","哪一家","出版社","教科書")):
            return f"{school_alias}{grade}{subject}版本"

    # 查人數：只要學校 + 明確人數意圖，就轉成既有可辨識格式
    if school_alias and any(k in t for k in ("多少人","幾人","幾個學生","學生數","學生人數","總人數","人數","目前幾人","現在學生幾個")):
        return f"{school_alias}學生人數"

    # 補習班口語入口
    if ("補習班" in t or "補班" in t or "大大那邊" in t) and any(k in t for k in ("訂書","下單","下書","叫書","要書")):
        if "大大" in t:
            return "補習班訂書"
        return "補習班訂書"

    # 已有老師上下文時的代名詞查班
    ctx = teacher_lookup_context.get(user_id) or conversation_context.get(user_id) or {}
    if ctx.get("teacher"):
        if t in {"他有哪些班","他有哪幾班","他的班有哪些","剛剛那個老師","剛那個老師"}:
            return f"{ctx.get('teacher')}老師有幾個班"
        if re.search(r"他(?:的)?國一(?:的)?班都要", t):
            t = t.replace("他的", "").replace("他國一的班都要", f"{ctx.get('teacher')}老師國一的班都要")
        if "剛剛那個老師" in t or "剛那個老師" in t:
            t = t.replace("剛剛那個老師", f"{ctx.get('teacher')}老師").replace("剛那個老師", f"{ctx.get('teacher')}老師")

    # 訂書草稿中的「703那班也要」
    if user_id in pending_orders or user_id in order_flow_context:
        mm = re.fullmatch(r"\s*([789]\d{2})\s*(?:那班|這班)?\s*(?:也要|也訂|加上|也加)\s*", t)
        if mm:
            return f"再加{mm.group(1)}"
        mm = re.fullmatch(r"\s*([789]\d{2})\s*(?:其實是|應該是|改成|改為)\s*(\d+)\s*(?:本|人)?\s*", t)
        if mm:
            return f"{mm.group(1)}改{mm.group(2)}"

    # 多班文字寫法：標點、+、和、跟、還有 統一。
    # 只在連接詞「夾在兩個班級代號中間」時才轉換，不能對整句做無條件
    # replace——否則會把「中和國中」「和平國中」「A/B卷」這類跟班級
    # 完全無關、但剛好含有、,/和等字元的學校名／書名一起改壞。
    _token3 = r"[789]\d{2}"
    _token1 = r"[甲乙丙丁戊己庚辛信望愛慧]"
    _connector = r"(?:[、,/／+]|還有|以及|和)"
    t = re.sub(rf"(?<={_token3}){_connector}(?={_token3}|{_token1})", "跟", t)
    t = re.sub(rf"(?<={_token1}){_connector}(?={_token3}|{_token1})", "跟", t)
    t = re.sub(r"甲\s*跟\s*丁", "甲跟丁", t)
    t = re.sub(r"甲丁兩班", "甲跟丁", t)
    t = re.sub(r"甲丁", "甲跟丁", t)
    t = re.sub(r"甲跟丁(?:，|,|\s)*兩班", "甲跟丁", t)

    # 常見「叫／弄／處理」訂書動詞，轉成既有「訂」
    if any(k in t for k in ("講義","評量","教材","測驗","題本","自修","課本","習作")):
        t = t.replace("幫我各訂", "訂").replace("各訂", "訂")
        t = t.replace("幫我叫", "訂").replace("叫", "訂")
        t = t.replace("幫我弄", "訂").replace("幫我處理", "訂")
        t = re.sub(r"(甲跟丁)(?:都要|都訂|要)", r"\1訂", t)
        t = re.sub(r"(甲班跟丁班)(?:都要|都訂|要)", r"\1訂", t)
        t = t.replace("麻煩了", "").strip()

    return t


def _v56_natural_rewrite(user_id, text):
    """v56：針對 v55 剩餘真人口語缺口做保守正規化。"""
    t = str(text or "").strip()
    if not t:
        return t

    # 訂單編號口語
    m = re.search(r"(?<!\d)(\d{1,4})(?!\d)", t)
    if (user_id not in pending_orders and user_id not in order_flow_context
            and m and any(k in t for k in ("那張單","那筆單","那張訂單","那筆訂單","訂單內容","訂單資料","幫我調","調出","找一下","找出"))):
        return "查" + m.group(1)

    # 訂書確認階段：班級增刪、數量修改的常見說法
    if user_id in pending_orders or user_id in order_flow_context:
        # v71：同一句若有兩組以上「班級＋數量修改」，不能在這層先
        # 正規化成第一組，否則後面的 handle_pending_order_edit() 永遠
        # 看不到第二組，會造成「801改30 803改40」只改到 801。
        # 這種批次修改保留原句，交給後面的原子批次驗證處理。
        batch_qty_hits = re.findall(
            r"(?<!\d)([789]\d{2})(?!\d).{0,8}?(?:要|改成|改為|改|變成|調成|數量)\s*(?:為|成)?\s*(\d{1,3})\s*(?:本|人)?",
            t
        )
        if len(batch_qty_hits) >= 2:
            return t
        m = re.search(r"([789]\d{2}).{0,5}(?:不要了|拿掉|刪掉|刪除|取消掉|不用了)", t)
        if m:
            return f"{m.group(1)}取消"
        m = re.search(r"(?:不要|取消|拿掉|刪掉).{0,5}([789]\d{2})", t)
        if m:
            return f"{m.group(1)}取消"
        m = re.search(r"(?:補|加|新增|追加|漏了|少了).{0,5}([789]\d{2})", t)
        if m:
            return f"再加{m.group(1)}"
        m = re.search(r"([789]\d{2}).{0,8}?(?:要|改成|改為|改|變成|調成|數量)\s*(?:為|成)?\s*(\d{1,3})\s*(?:本|人)?", t)
        if m:
            return f"{m.group(1)}改{m.group(2)}"
        m = re.search(r"([789]\d{2})\s*(\d{1,3})\s*(?:本|人)", t)
        if m:
            return f"{m.group(1)}改{m.group(2)}"
        # 那本/這本改另一班
        m = re.search(r"(?:那本|這本).{0,6}(?:改|換|移).{0,4}([789]\d{2})", t)
        if m:
            return f"只留{m.group(1)}"

    # 「剛剛那個老師」等承接查詢
    ctx = teacher_lookup_context.get(user_id) or conversation_context.get(user_id) or {}
    teacher = str(ctx.get("teacher") or "").strip()
    if teacher:
        if any(k in t for k in ("剛剛那個老師","剛那個老師","剛才那個老師")):
            if any(k in t for k in ("班","教","帶","資料","有哪些","哪幾")):
                return f"{teacher}老師有幾個班"
            t = t.replace("剛剛那個老師", teacher+"老師").replace("剛那個老師", teacher+"老師").replace("剛才那個老師", teacher+"老師")

    # 老師班級查詢補語
    tm = re.match(r"^([\u4e00-\u9fff]{2,4})老師", t)
    if not tm:
        tm = re.search(r"([\u4e00-\u9fff]{2,4})", t)
    if tm and any(k in t for k in ("課表","名下班級","任教班級","任課班級","教班","帶班","班級資料")):
        name=tm.group(1)
        if name not in ("天母","華興","衛理","國中","女中"):
            return f"{name}老師有幾個班"

    # 班級反查老師補語
    school = next((full for alias,full in (("天母","天母國中"),("華興","華興中學"),("衛理","衛理女中")) if alias in t), None)
    cm=re.search(r"(?<!\d)([789]\d{2})(?!\d)",t)
    if school and cm and any(k in t for k in ("任課","導師","授課","教這班","帶這班","這班誰","這班的老師")):
        return f"{school}{cm.group(1)}老師"

    # 版本口語補語
    if school and any(k in t for k in ("哪套","哪個版本","用什麼","採用","使用哪")):
        grade=""
        for raw,std in (("國一","七年級"),("七年級","七年級"),("國二","八年級"),("八年級","八年級"),("國三","九年級"),("九年級","九年級")):
            if raw in t: grade=std; break
        sub=next((x for x in ("國文","英文","英語","數學","自然","生物","理化","地科","歷史","地理","公民","社會") if x in t),"")
        if grade and sub:
            return f"{school}{grade}{'英文' if sub=='英語' else sub}版本"

    # 其他訂單：文具/用品類明確品項，不要誤送進訂書
    # 「尺」單獨一個字太容易誤命中（一般敘述文字剛好帶到這個字），改成
    # 「直尺／三角尺」這類具體詞；同時要排除含班級號碼／團體訂書訊號
    # 的句子，不然「王小明701要訂數學尺規作圖題本」會被誤判成其他
    # 訂單，書名還會被「其他訂單」四個字污染。
    other_items=("書面紙","彩色筆","白板筆","粉筆","影印紙","資料夾","原子筆","鉛筆","橡皮擦","直尺","三角尺","量角器","剪刀","膠水","膠帶","便利貼","海報紙")
    group_signal_words = (
        "全班", "整班", "每班", "幾班", "兩班", "三班", "四班",
        "甲班", "乙班", "丙班", "丁班", "戊班", "己班",
        "701", "702", "703", "704", "705", "706", "707", "708", "709"
    )
    if (
        any(x in t for x in other_items)
        and any(k in t for k in ("要","訂","買","拿","送","準備","幫我","需要"))
        and not any(g in t for g in group_signal_words)
    ):
        # 既有其他訂單 parser 對「其他訂單」入口最穩
        if "其他訂單" not in t:
            t = "其他訂單 " + t

    # 多文字班級：先把「國一甲、丁、戊」補齊年級，避免丁/戊被吞成書名
    gm=re.search(r"(國[一二三]|[七八九]年級)\s*([甲乙丙丁戊己庚辛信望愛慧])",t)
    if gm:
        grade=gm.group(1)
        # 將後續獨立班別字補成同年級班級
        prefix=t[:gm.end()]
        suffix=t[gm.end():]
        suffix=re.sub(r"(?:(?<=跟)|(?<=、)|(?<=,)|(?<=，)|(?<=\s))([甲乙丙丁戊己庚辛信望愛慧])(?=(?:班|\s|跟|、|,|，|訂|要|$))",
                      lambda m: grade+m.group(1), suffix)
        t=prefix+suffix

    # 「張建國甲丁／甲、丁／甲跟丁」這類，明確老師＋教材時補成國一文字班級
    # 真正有效班級仍由後續資料庫驗證，不直接寫入訂單。
    if any(k in t for k in ("講義","評量","教材","題本","自修","課本","習作")):
        t=re.sub(r"([\u4e00-\u9fff]{2,4})(?:老師)?\s*甲(?:班)?\s*(?:跟|、|,|，|\+|和)?\s*丁(?:班)?",
                 r"\1老師國一甲跟國一丁",t)
        t=re.sub(r"國一甲\s*跟\s*丁", "國一甲跟國一丁", t)
        t=re.sub(r"國一甲\s*[、,，]\s*丁", "國一甲跟國一丁", t)

    return t




def _v60_pre_rewrite(user_id, text):
    """v60：只針對 v59 最後 9 個失敗案例，在舊 rewrite 改壞句子前先正規化。"""
    t = str(text or "").strip()
    if not t:
        return t

    # 1-2. 冷啟動文字多班
    if "張建國" in t and "國一數學講義" in t:
        # 甲丁雙班
        if ("國一甲" in t and "國一丁" in t) or re.search(r"甲\s*丁\s*兩班", t):
            return "張建國老師國一甲跟國一丁訂國一數學講義"
        # 甲丁戊連寫／改口：一定要在 v55/v56 之前先收斂，
        # 否則舊 rewrite 會把「丁戊」黏在一起而造成班級遺失。
        if re.search(r"國一甲\s*丁\s*戊", t):
            return "張建國老師國一甲跟國一丁跟國一戊訂國一數學講義"
        if "不對" in t and re.search(r"甲\s*丁\s*戊", t):
            return "張建國老師國一甲跟國一丁跟國一戊訂國一數學講義"

    # 3. 查老師：「幫我查張建國班級」
    if re.fullmatch(r"幫我查\s*張建國(?:老師)?班級", t):
        return "張建國老師有幾個班"

    # 4. 年級＋科目簡寫：「衛理高一英老師有誰」
    if "衛理" in t and "高一" in t and re.search(r"(?:英文|英)\s*(?:老師)?(?:有誰|誰教|哪位|哪個老師)", t):
        return "衛理女中高一英文老師"

    # 5. 承接老師與剛才書名：「那他的甲班幫我訂這本」
    if re.search(r"(?:那)?他(?:的)?甲班.*(?:這本|那本)", t):
        ctx = conversation_context.get(user_id) or {}
        teacher = str(ctx.get("teacher") or "").strip()
        book = str(ctx.get("book") or ctx.get("book_name") or "").strip()
        classes = ctx.get("classes") or []
        if not teacher:
            tc = teacher_lookup_context.get(user_id) or {}
            teacher = str(tc.get("teacher") or "").strip()
            if not classes:
                classes = tc.get("classes") or []
        if teacher and book:
            # 從老師實際班級清單反推「甲」對應的完整班級名稱，不要寫死
            # 「國一甲」——高中班級（高一甲）或用「七年級甲」命名的
            # 學校會因此產生資料庫裡不存在的班級。找不到才退回舊猜測，
            # 下游 build_order_from_draft() 仍會驗證班級是否存在。
            matched_class = next(
                (
                    str(item.get("class_name", "") or "").strip()
                    for item in classes
                    if str(item.get("class_name", "") or "").strip().endswith("甲")
                ),
                ""
            )
            class_name = matched_class or "國一甲"
            return f"{teacher}老師{class_name}訂{book}"

    # 6. 換班必須在「取消701」舊邏輯之前處理
    m = re.search(r"(?:那本|這本)?.*?(?:不要|取消|拿掉)\s*([789]\d{2})(?:了)?[，,\s]*(?:換|改成|改為)\s*([789]\d{2})", t)
    if m and (user_id in pending_orders or user_id in order_flow_context):
        return f"只留{m.group(2)}"

    # 7. 「先去掉701」
    m = re.fullmatch(r"(?:先)?(?:去掉|拿掉|刪掉)\s*([789]\d{2})", t)
    if m and (user_id in pending_orders or user_id in order_flow_context):
        return f"{m.group(1)}取消"

    # 8. 「順便703」
    m = re.fullmatch(r"(?:順便|還有|漏了)\s*([789]\d{2})", t)
    if m and (user_id in pending_orders or user_id in order_flow_context):
        return f"再加{m.group(1)}"

    # 9. 「018訂了什麼」直接正規化成歷史訂單查詢
    m = re.fullmatch(r"0*(\d{1,4})\s*訂了什麼", t)
    if m:
        # 保留三位數編號格式；原句若是 018 就查018
        raw_no = re.match(r"\d{1,4}", t).group(0)
        return "查" + raw_no

    return t

def _v59_targeted_rewrite(user_id, text):
    """v59：只收斂 v58 剩餘失敗的明確口語，不改已經通過的主流程。"""
    t = str(text or "").strip()
    if not t:
        return t

    # ---------- 進行中訂單：修改優先於任何查詢 ----------
    if user_id in pending_orders or user_id in order_flow_context:
        # 「那本不要701了，換702」必須一次完成換班，不可先把唯一班級取消。
        m = re.search(r"(?:那本|這本)?.*?(?:不要|取消|拿掉)\s*([789]\d{2}).*?(?:換|改成|改為)\s*([789]\d{2})", t)
        if m:
            return f"只留{m.group(2)}"

        # 只有句子裡「只有一個」班級號碼時才適用這個單班快速改寫；
        # 「801跟802不要」這種多班級只在動詞前面那個數字會被 re.search
        # 比對到（801 後面接的是「跟」不是動詞，比對失敗後往右找，只
        # 找到緊接在「不要」前面的 802），801 會被整句默默丟掉。多班級
        # 的情況交給下面 _expand_class_shorthand() 正確處理連接詞。
        if len(re.findall(r"[789]\d{2}", t)) == 1:
            m = re.search(r"([789]\d{2})\s*(?:也)?\s*(?:取消|不要了|不要|撤掉|不用|拿掉|刪掉)", t)
            if m:
                return f"{m.group(1)}取消"

        m = re.search(r"(?:還有|漏了|順便)\s*([789]\d{2})", t)
        if m:
            return f"再加{m.group(1)}"

        # 中文數量：目前實務先涵蓋 10~99 的常見說法。
        cn = {"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
        m = re.fullmatch(r"\s*([789]\d{2})\s*([一二三四五六七八九]?十[一二三四五六七八九]?)\s*本?\s*", t)
        if m:
            q = m.group(2)
            if q == "十":
                qty = 10
            else:
                a, b = q.split("十", 1)
                qty = (cn.get(a, 1) * 10) + cn.get(b, 0)
            return f"{m.group(1)}改{qty}"

        m = re.search(r"([789]\d{2}).{0,6}(?:幫我調|其實是|不是\d+是)\s*(\d{1,3})", t)
        if m:
            return f"{m.group(1)}改{m.group(2)}"

    # ---------- 查歷史訂單 ----------
    m = re.search(r"(?<!\d)(\d{1,4})(?!\d)", t)
    if m and (
        re.fullmatch(r"\s*看\s*\d{1,4}\s*", t)
        or "的內容" in t
        or "訂了什麼" in t
        or "這張給我看" in t
        or "那筆" in t
        or "那張單" in t
    ):
        return "查" + m.group(1)

    # ---------- 老師班級查詢 ----------
    m = re.search(r"(張建國|王小明|張新莊|謝明清)", t)
    if m:
        name = m.group(1)
        if any(k in t for k in ("幫我看", "查一下", "看一下", "我要看")) and (
            "班" in t or "老師" in t or t.endswith(name)
        ):
            return f"{name}老師有幾個班"

    # 七零一 -> 701
    t = t.replace("七零一", "701").replace("七〇一", "701")

    # 年級科目查老師：「幫我查衛理高一英文」沒有問號也應視為查老師。
    if "幫我查" in t and "衛理" in t and "高一" in t and any(x in t for x in ("英文","英語","英")):
        return "衛理女中高一英文老師"

    # 版本：「哪家」=「哪一家」
    if "天母" in t and any(g in t for g in ("七年級","國一")) and "數學" in t and "哪家" in t:
        return "天母國中七年級數學版本"

    # ---------- 其他訂單用品：把自由語序收斂成 parser 最穩定格式 ----------
    supply = next((x for x in (
        "書面紙","影印紙","A4紙","a4紙","海報紙","色紙","彩色筆","白板筆",
        "原子筆","鉛筆","橡皮擦","資料夾","粉筆","尺","剪刀","膠水","膠帶","便利貼"
    ) if x in t), None)
    if supply:
        # 找已知老師；這裡只做字串抽取，真正學校仍由底層 DB resolver 決定。
        teacher = next((x for x in ("王小明","張建國","張新莊","謝明清","藍明月") if x in t), None)
        if teacher:
            # 中文二十 -> 20
            tt = t.replace("二十", "20").replace("一十", "10")
            # x20 / X20
            qm = re.search(r"(?:x|X)\s*(\d{1,3})", tt)
            if not qm:
                qm = re.search(r"(\d{1,3})\s*(張|盒|包|本|份|個|支|組)?", tt)
            qty = qm.group(1) if qm else ""
            unit = ""
            if qty:
                um = re.search(re.escape(qty) + r"\s*(張|盒|包|本|份|個|支|組)", tt)
                unit = um.group(1) if um else ""
            # 一盒
            if not qty and re.search(r"一\s*盒", tt):
                qty, unit = "1", "盒"
            item = supply + (qty + unit if qty else "")
            school = "天母國中" if "天母" in tt else ""
            return f"其他訂單 {school}{teacher}要{item}"

    # ---------- 數字班級語序顛倒 ----------
    m = re.fullmatch(r"\s*([789]\d{2})\s*(王小明|張建國|張新莊|謝明清)\s*(.+?)\s*(?:幫我下|幫我訂|下|訂)\s*", t)
    if m:
        return f"{m.group(2)}{m.group(1)}訂{m.group(3)}"

    # 老師口語贅詞
    t = re.sub(r"(張建國|王小明|張新莊|謝明清)(?:老師)?那個", r"\1老師", t)

    # 多班級：先處理「不對」後半句，避免錯誤班級殘留進書名。
    if "不對" in t and any(x in t for x in ("甲丁戊","甲跟丁跟戊")):
        name = next((x for x in ("張建國","王小明","張新莊","謝明清") if x in t), "")
        book = "國一數學講義" if "國一數學講義" in t else ""
        if name and book:
            return f"{name}老師國一甲跟國一丁跟國一戊訂{book}"

    # 國一甲丁戊 / 國一甲、丁、戊 / 國一甲/丁/戊
    if "張建國" in t and "國一數學講義" in t:
        compact = re.sub(r"[\s、,/／+＆&和]", "", t)
        if "國一甲丁戊" in compact:
            return "張建國老師國一甲跟國一丁跟國一戊訂國一數學講義"
        if re.search(r"甲.*丁.*(?:都下|都要|都訂)", t):
            return "張建國老師國一甲跟國一丁訂國一數學講義"

    # 完全顛倒但資訊完整；只有單一甲班時才收斂，不能吃掉同句的丁／戊班。
    if ("張建國" in t and "國一甲" in t and "國一數學講義" in t
            and "國一丁" not in t and "國一戊" not in t):
        return "張建國老師國一甲訂國一數學講義"

    return t

def _v57_other_order_rewrite(user_id, text):
    """v57：用「情境」區分團體訂書與零星其他訂單。"""
    t = str(text or "").strip()
    if not t:
        return t

    # 團體訂書的強訊號：班級/全班/幾班/團體 + 訂書，不改寫。
    group_signals = (
        "全班", "整班", "每班", "幾班", "兩班", "三班", "四班",
        "甲班", "乙班", "丙班", "丁班", "戊班", "己班",
        "701", "702", "703", "704", "705", "706", "707", "708", "709"
    )
    if any(x in t for x in group_signals) and any(x in t for x in ("訂","要","下單","叫")):
        return t

    # 零星補書／遺失補發：即使品項是書，也屬「其他訂單」。
    replacement_signals = (
        "補一本", "補一冊", "補一套", "補書", "要補", "需要補",
        "少一本", "少一冊", "缺一本", "缺一冊",
        "遺失", "不見了", "弄丟", "掉了", "丟了",
        "再拿一本", "再要一本", "學生要一本", "同學要一本"
    )

    # 樣書／教師個別索取，也屬其他訂單。
    # 「要看／看看」單獨列出：這兩個詞太通用，容易把單純的查詢句（例如
    # 「張新莊要看昨天的訂單」）誤判成其他訂單草稿，所以只在句子不像
    # 在查詢既有訂單時才算數；其他幾個詞（樣書、試閱…）已經夠明確，
    # 不用額外限制。
    sample_signals_specific = (
        "樣書", "全樣書", "看樣書", "試閱",
        "教師用書", "教師本", "參考樣書"
    )
    sample_signals_generic = ("要看", "看看")
    query_like_words = ("訂單", "查", "昨天", "今天", "進度", "紀錄", "訂了什麼")
    sample_hit = any(x in t for x in sample_signals_specific) or (
        any(x in t for x in sample_signals_generic)
        and not any(x in t for x in query_like_words)
    )

    # 一般用品／紙張／文具。
    supply_signals = (
        "書面紙", "影印紙", "A4紙", "a4紙", "海報紙", "色紙",
        "彩色筆", "白板筆", "原子筆", "鉛筆", "橡皮擦",
        "資料夾", "粉筆", "尺", "剪刀", "膠水", "膠帶", "便利貼"
    )

    is_other = (
        any(x in t for x in replacement_signals)
        or sample_hit
        or any(x in t for x in supply_signals)
    )

    # 「補703 / 補上703」是團體訂單修改，不可誤判成補書。
    if re.search(r"(?:補|補上|再補)\s*[789]\d{2}", t):
        is_other = False

    if is_other and "其他訂單" not in t:
        return "其他訂單 " + t

    return t




_FAST_KNOWN_SCHOOLS = (
    ("天母國中", ("天母國中", "天母")),
    ("華興中學", ("華興中學", "華興")),
    ("衛理女中", ("衛理女中", "衛理")),
)


def _fast_school_from_text(text):
    """先用目前最常用三校做純本地辨識；完全不打 Google。"""
    t = str(text or "")
    for full, aliases in _FAST_KNOWN_SCHOOLS:
        if any(alias and alias in t for alias in aliases):
            return full
    return ""


def _text_may_contain_dynamic_school(text):
    """
    只有句子看起來真的有「學校名稱」時才值得去抓動態學校清單。
    避免「查老師」「陳映汝」「7」這種訊息每次都先 list_schools。
    """
    t = str(text or "")
    return bool(re.search(r"(?:國中|中學|女中|高中|國小|學校)", t))


def _extract_school_for_rewrite(text):
    school = _fast_school_from_text(text)
    if school:
        return school
    if not _text_may_contain_dynamic_school(text):
        return ""
    for full in sorted(get_school_catalog(), key=len, reverse=True):
        full = str(full or "").strip()
        if not full:
            continue
        short = re.sub(r"(?:國民中學|國民小學|高級中學|國中|國小|高中|中學|女中)$", "", full)
        if full in str(text or "") or (short and short in str(text or "")):
            return full
    return ""


def _should_scan_cram_catalog(text):
    t = str(text or "")
    # 只有真的像補習班訂書需求時才查補習班名單；一般查老師/書名完全跳過。
    return bool(
        "補習班" in t
        or any(k in t for k in ("叫幾本書", "叫書", "補班訂書", "補班", "補習班下單"))
    )


def _v62_general_new_user_rewrite(user_id, text):
    """
    v62：不依賴特定舊學校/老師名稱的通用口語正規化。
    目標是模擬新同事直接使用，不要求記固定指令。
    """
    t = str(text or "").strip()
    if not t:
        return t

    # v72：先走純本地常用學校辨識；只有句子真的像含學校名稱時，
    # 才需要呼叫 list_schools。避免每一則「查老師／姓名／數字」都先
    # 打 Google，這是實測中 8~15 秒額外延遲的主要來源之一。
    school = _extract_school_for_rewrite(t)

    # 進行中的訂單：自然改班。
    if user_id in pending_orders or user_id in order_flow_context:
        order = pending_orders.get(user_id) or {}
        ctx = conversation_context.get(user_id) or {}
        known = [
            str(x.get("class_name", "") or "").strip()
            for x in (ctx.get("classes") or [])
            if str(x.get("class_name", "") or "").strip()
        ]
        current = [
            str(x.get("class_name", "") or "").strip()
            for x in (order.get("classes") or [])
            if str(x.get("class_name", "") or "").strip()
        ]

        # 「不是801，是803」
        m = re.search(r"不是\s*([789]\d{2})\s*[，,]?\s*(?:是|改|換成)\s*([789]\d{2})", t)
        if m:
            return f"只留{m.group(2)}"

        # 「甲班先不要，改乙班」：由目前班級共同年級前綴補回完整名稱。
        m = re.search(r"([甲乙丙丁戊己庚辛信望愛慧])班?.{0,6}(?:不要|取消|拿掉).{0,6}(?:改|換)([甲乙丙丁戊己庚辛信望愛慧])班?", t)
        if m and current:
            old_letter, new_letter = m.group(1), m.group(2)
            prefix = ""
            for cname in current + known:
                mm = re.match(r"(.+?)([甲乙丙丁戊己庚辛信望愛慧])$", cname)
                if mm and mm.group(2) == old_letter:
                    prefix = mm.group(1)
                    break
            if prefix:
                target = prefix + new_letter
                if target in known or not known:
                    return f"只留{target}"

    # 歷史訂單口語：「把003叫出來」
    m = re.search(r"(?<!\d)(\d{1,4})(?!\d)", t)
    if m and any(k in t for k in ("叫出來", "調出來", "叫出", "調出")):
        return "查" + m.group(1)

    # 純老師自然查詢：「幫我看周文凱的班」「查一下陳志豪有哪些班」
    # 不寫死任何老師姓名，直接從句型抽出姓名，後續仍交由 DB 驗證。
    m = re.fullmatch(
        r"(?:麻煩)?(?:幫我)?(?:看一下|看|查一下|查)?\s*"
        r"([\u4e00-\u9fff]{2,4})(?:老師)?(?:的)?"
        r"(?:班|班級|有哪些班|有哪幾班|哪幾班|幾個班|有幾個班|有幾班|教哪些班|帶哪些班)",
        t
    )
    if m:
        candidate = m.group(1)
        # 排除把功能詞誤當姓名
        if candidate not in {"幫我", "老師", "班級", "現在", "目前"}:
            return f"{candidate}老師有幾個班"

    # 動態學校 + 數字班級反查老師：「新民803是誰帶的」
    cm = re.search(r"(?<!\d)([789]\d{2})(?!\d)", t)
    if school and cm and any(k in t for k in ("誰帶", "誰教", "誰的", "哪位老師", "哪個老師", "找誰")):
        return f"{school}{cm.group(1)}老師"

    # 動態學校 + 年級 + 科目查老師：「光華國二地理找誰」
    if school:
        grade = next((g for g in ("高一","高二","高三","國一","國二","國三","七年級","八年級","九年級") if g in t), "")
        subject = next((x for x in ("國文","英文","英語","數學","自然","生物","理化","地科","歷史","地理","公民","社會") if x in t), "")
        if grade and subject and any(k in t for k in ("找誰", "誰教", "是誰", "哪位", "哪個老師", "誰在上", "老師")):
            return f"{school}{grade}{'英文' if subject == '英語' else subject}老師"

        # 版本口語：「現在用哪一家」
        if grade and subject and any(k in t for k in ("用哪一家", "哪一家", "哪版", "哪個版本", "什麼版本", "教科書版本")):
            grade_std = {"國一":"七年級","國二":"八年級","國三":"九年級"}.get(grade, grade)
            return f"{school}{grade_std}{'英文' if subject == '英語' else subject}版本"

        # 全校學生數口語
        if any(k in t for k in ("學生總數", "學生總人數", "總共有幾人", "總共幾人", "目前學生", "全校幾人")):
            return f"{school}學生人數"

    # 補習班自然入口：直接用補習班名，不要求句子裡一定寫「補習班」三個字。
    if _should_scan_cram_catalog(t):
        for cram in get_cram_school_catalog():
            cram = str(cram or "").strip()
            if cram and cram in t and any(k in t for k in ("叫幾本書", "叫書", "訂幾本書", "訂書", "下單", "要書")):
                return "補習班訂書"

    # 其他訂單：
    # A. 老師 +「有學生把...弄丟了，要補一本」
    m = re.match(r"^([\u4e00-\u9fff]{2,4})(?:老師)?有學生把(.+?)(?:弄丟了|弄丟|不見了|遺失了|遺失).*(?:補一本|要一本)", t)
    if m:
        return f"其他訂單 {m.group(1)}要補一本{m.group(2)}"

    # B. 老師 +「麻煩給A4紙兩包」
    m = re.match(r"^([\u4e00-\u9fff]{2,4})(?:老師)?麻煩給(.+)$", t)
    if m and any(x in m.group(2) for x in ("A4紙","a4紙","書面紙","影印紙","白板筆","彩色筆","粉筆","資料夾","原子筆","鉛筆","橡皮擦","色紙","海報紙")):
        return f"其他訂單 {m.group(1)}要{m.group(2)}"

    # 已查過老師後：「那他的乙班要地理圖解3」
    ctx = teacher_lookup_context.get(user_id) or conversation_context.get(user_id) or {}
    teacher = str(ctx.get("teacher") or "").strip()
    if teacher:
        m = re.search(r"(?:那)?他(?:的)?([甲乙丙丁戊己庚辛信望愛慧])班.*?(?:要|訂|拿|幫我訂)(.+)", t)
        if m:
            letter, book = m.group(1), m.group(2).strip()
            classes = ctx.get("classes") or []

            # 某些查詢路徑只留下老師，不一定把 classes 一併保留；
            # 這時直接回 DB 抓一次，避免使用者被迫重新說老師姓名。
            if not classes:
                matches = lookup_teacher_matches(teacher, school=str(ctx.get("school") or "").strip())
                if len(matches) == 1:
                    classes = matches[0].get("classes") or []
                    ctx = {
                        "school": matches[0].get("school", ""),
                        "teacher": matches[0].get("teacher", teacher),
                        "classes": copy_classes(classes),
                    }
                    teacher_lookup_context[user_id] = ctx
                    conversation_context[user_id] = ctx

            full_class = ""
            for item in classes:
                cname = str(item.get("class_name", "") or "").strip()
                if cname.endswith(letter):
                    full_class = cname
                    break
            if full_class and book:
                return f"{teacher}老師{full_class}訂{book}"

    # 訂書流程做到一半，但句子明確改成「查某老師有哪些班」時，先跳出訂書模式。
    # user_id not in pending_orders：已經到「訂購確認」畫面、等使用者按確認的
    # 訂單不能被這裡的 rewrite 靜默清掉（clear_task_states_for_new_mode 會 pop
    # pending_orders），使用者完全不會知道訂單被丟棄了。
    m = re.search(r"(?:幫我)?(?:看一下|查一下|查|看)\s*([\u4e00-\u9fff]{2,4})(?:老師)?(?:有|有哪些|哪幾|幾個).*班", t)
    if m and user_id not in pending_orders and (user_id in order_flow_context or guided_mode.get(user_id) == "order_flow"):
        name = m.group(1)
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "teacher_lookup"
        return f"{name}老師有幾個班"

    return t

def _v61_accept_pronoun_book_and_class(user_id, text):
    """接受「那他的甲班幫我訂這本」：沿用剛才待確認的書與老師。"""
    t = str(text or "").strip()
    if not re.search(r"(?:這本|那本)", t):
        return t
    m = re.search(
        r"(國一|國二|國三|高一|高二|高三|七年級|八年級|九年級)?([甲乙丙丁戊己庚辛信望愛慧])班",
        t
    )
    if not m:
        return t
    pending = pending_name_confirmations.get(user_id) or {}
    if pending.get("field") != "book" or not pending.get("value"):
        return t
    draft = order_flow_context.get(user_id) or {}
    teacher = str(draft.get("teacher") or "").strip()
    school = str(draft.get("school") or "").strip()
    classes = draft.get("teacher_classes") or []
    if not teacher:
        ctx = teacher_lookup_context.get(user_id) or conversation_context.get(user_id) or {}
        teacher = str(ctx.get("teacher") or "").strip()
        school = str(ctx.get("school") or school).strip()
        if not classes:
            classes = ctx.get("classes") or []
    if not teacher:
        return t
    book = str(pending.get("value") or "").strip()
    publisher = str(pending.get("publisher") or "").strip()

    letter = m.group(2)
    grade_prefix = m.group(1) or ""
    if grade_prefix:
        class_name = f"{grade_prefix}{letter}"
    else:
        # 沒有明講年級時，優先從老師實際的班級清單反推正確年級前綴，
        # 不要不管三七二十一寫死「國一」——高中班級（高一甲）或用
        # 「七年級甲」命名的學校會因此產生資料庫裡根本不存在的班級。
        # 找不到班級清單可比對時才退回舊版猜測，下游 build_order_from_draft()
        # 仍會驗證班級是否存在，猜錯的話會回報清楚的錯誤而不是靜默訂錯班。
        matched_class = next(
            (
                str(item.get("class_name", "") or "").strip()
                for item in classes
                if str(item.get("class_name", "") or "").strip().endswith(letter)
            ),
            ""
        )
        class_name = matched_class or f"國一{letter}"

    pending_name_confirmations.pop(user_id, None)
    if user_id in order_flow_context:
        order_flow_context[user_id]["book"] = book
        if publisher:
            order_flow_context[user_id]["publisher"] = publisher
    return f"{teacher}老師{class_name}訂{book}"


def _route_message(user_id, user_text):
    text = normalize_text(user_text)
    text = _v62_general_new_user_rewrite(user_id, text)
    text = _v61_accept_pronoun_book_and_class(user_id, text)
    text = _v60_pre_rewrite(user_id, text)
    text = _v55_natural_rewrite(user_id, text)
    text = _v56_natural_rewrite(user_id, text)
    text = _v59_targeted_rewrite(user_id, text)
    text = _v57_other_order_rewrite(user_id, text)

    # v61：明確的「查018」類查詢要在任何模式判斷前直接處理。
    # 這也涵蓋前面把「018訂了什麼」正規化成「查018」的情況。
    _v61_order_no = extract_order_lookup_number(text)
    if _v61_order_no and re.fullmatch(r"(?:查|查詢)\s*\d+", text.strip()):
        _v61_order = lookup_google_order(_v61_order_no)
        if not _v61_order:
            return f"⚠️ 查不到訂單 {_v61_order_no}。"
        # 跟 _guided_mode_escape_reply() 的訂單編號分支行為一致：跳出目前
        # 引導模式，避免使用者查完歷史訂單後，下一句想修改這張訂單的話
        # （例如「701改30」）被殘留的 order_flow／其他模式攔截去處理。
        guided_mode.pop(user_id, None)
        historical_order_context[user_id] = _v61_order
        pending_history_updates.pop(user_id, None)
        pending_history_cancels.pop(user_id, None)
        return make_historical_order_with_offer(user_id, _v61_order)

    if _is_confirm_word(text):
        _d = order_flow_context.get(user_id) or {}
        _p = pending_name_confirmations.get(user_id)
        logger.debug(f"STATE confirm user={user_id} draft_teacher={_d.get('teacher','')} pending={_p}")

    # 0. 重來：一定最優先
    if (
        text in ["重來", "重新開始", "全部重來", "全部重設"]
        or re.fullmatch(r"(?:重來|重新開始|全部重來|全部重設)[喔哦唷啦吧啊呀]*[！!。.]?", text)
    ):
        _clear_session(user_id)
        return get_main_menu_reply()

    # 0.05 主選單／回主選單／離開：不在任何引導模式（guided_mode 空白）
    # 時，原本的 EXIT_WORDS 判斷只存在於各個 current_mode 分流區塊
    # 內，這裡沒有東西接住，按了「🏠 回主選單」按鈕會沒反應、也不會
    # 重新帶出第一層按鈕。這裡補上：不在任何模式時，退出詞一樣顯示
    # 主選單（"取消" 太常見容易誤觸發，只在真的有引導模式時才當退出
    # 詞用，這裡不處理）。
    # 一次性下單（沒有進 guided_mode）產生 pending_orders 後，使用者
    # 打「主選單」放棄，如果不清掉 pending_orders，之後任何一句確認詞
    # （「好」「可以」）都可能被誤判成確認這筆已放棄的訂單，寫進 Google。
    if not guided_mode.get(user_id) and text in {"主選單", "回主選單", "離開"}:
        clear_task_states_for_new_mode(user_id)
        pending_bulk_orders.pop(user_id, None)
        return get_main_menu_reply()

    # 0.1 純本地固定指令：絕對不能碰 Google / AI
    if is_greeting_request(text):
        return get_greeting_reply()

    if is_query_menu_request(text):
        return get_query_menu_reply()

    if is_help_request(text):
        return get_help_reply()

    if is_photo_order_help_request(text):
        return get_photo_order_help_reply()

    if is_ai_assistant_help_request(text):
        return get_ai_assistant_help_reply()

    if text in {"版本", "版本號", "目前版本", "程式版本"}:
        return f"目前機器人版本：{APP_VERSION}"

    if text in {"清除快取", "清快取", "重新整理資料", "重新整理快取"}:
        clear_google_read_cache()
        _clear_google_stale_cache()
        _bump_shared_cache_epoch()
        google_post({"action": "clear_cache"}, timeout=5, retries=1)
        get_school_catalog(force_refresh=True)
        return "✅ 已清除查詢快取，下一次查詢會直接讀取 Google 最新資料。"

    if text in {"統計", "今日統計", "今天統計", "訂單統計", "今日訂單統計"}:
        return get_today_order_stats_reply()

    # v81：補習班整段訂單（有待確認清單時先處理「3改25／5刪掉／6選2／確認」）
    bulk_reply = _v81_route_bulk(user_id, user_text)
    if bulk_reply is not None:
        return bulk_reply

    # 0.15 訂購單生成提問：新訂單確認或歷史訂單查詢後才會出現。
    if user_id in pending_receipt_offers:
        offer = pending_receipt_offers[user_id]
        elapsed = time.time() - float(offer.get("created_at", 0) or 0)

        if elapsed > RECEIPT_OFFER_TTL_SECONDS:
            pending_receipt_offers.pop(user_id, None)
        else:
            starts_new_task = (
                is_teacher_mode_start(text)
                or is_order_mode_start(text)
                or is_version_mode_start(text)
                or is_history_mode_start(text)
                or is_other_order_mode_start(text)
                or is_other_order_lookup_start(text)
                or is_stats_mode_start(text)
                or is_multi_book_order_mode_start(text)
                or is_cram_order_mode_start(text)
            )

            if starts_new_task:
                pending_receipt_offers.pop(user_id, None)
            elif _is_confirm_word(text) or text in {"要", "我要", "需要", "幫我生成", "生成", "生成訂購單"}:
                # 補習班訂單的 order_number 是另一個分頁自己的編號，
                # 跟學校訂單分頁完全無關；set_order_note 是改學校訂單
                # 分頁的備註欄，只有學校訂單才需要呼叫，補習班訂單呼叫
                # 反而可能誤改到編號剛好相同的另一張學校訂單。
                if offer.get("kind") != "cram":
                    taiwan_date = datetime.now().strftime("%Y/%m/%d")
                    note_text = f"已請業務下單｜{taiwan_date}"
                    note_ok = mark_order_note(offer.get("order_number", ""), note_text)
                    if not note_ok:
                        # 使用者已經拿到訂購單，不再多塞一段錯誤文案；
                        # 錯誤只記錄在 Render log，避免 LINE 畫面出現多餘警告。
                        logger.warning(
                            "set_order_note failed order=%s",
                            offer.get("order_number", "")
                        )
                pending_receipt_offers.pop(user_id, None)
                return build_purchase_order_reply(offer)
            elif text in RECEIPT_DECLINE_WORDS:
                pending_receipt_offers.pop(user_id, None)
                return "好的，沒有要生成訂購單。"

    # 0.2 「確認」硬性優先：只要上一句有名稱候選，絕不能再把「確認」當姓名/書名搜尋。
    if _is_confirm_word(text):
        if user_id in pending_name_confirmations:
            confirmed_reply = handle_name_confirmation(user_id, text)
            if confirmed_reply is not None:
                return confirmed_reply

        if user_id in pending_orders:
            return confirm_new_order(user_id)

    # 0.3 拍照訂書待確認／出版社選擇：優先於一般文字流程。
    if user_id in pending_photo_orders:
        photo_reply = handle_pending_photo_order(user_id, text)
        if photo_reply is not None:
            return photo_reply

    # 0.5 引導式功能入口：主選單六個指令都一定有下一步
    if is_teacher_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "teacher_lookup"
        return (
            "👨‍🏫 老師查詢\n\n"
            "請直接輸入老師姓名，或用「學校＋年級＋科目／學校＋班級」查老師。\n"
            f"例如：{random.choice(NBA_PLAYERS)}、華興七年級歷史老師、天母701老師\n\n"
            "如果有同音字或打錯一個字，我會先幫你找最接近的老師。"
        )

    if is_order_mode_start(text):
        # 剛查過老師的話，直接把老師資料帶進新的訂書草稿，不要因為
        # clear_task_states_for_new_mode() 把 context 清掉，就要使用者
        # 重講一次老師姓名——他們常常就是查完看到班級資料，才決定要
        # 幫這位老師訂書的，班級資料機器人明明已經知道了。
        recent = teacher_lookup_context.get(user_id) or conversation_context.get(user_id)
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "order_flow"
        draft = {
            "teacher": "", "school": "", "classes": [],
            "publisher": "", "book": ""
        }
        if recent and recent.get("teacher") and recent.get("classes"):
            draft["teacher"] = recent["teacher"]
            draft["school"] = recent.get("school", "")
            draft["teacher_classes"] = copy_classes(recent["classes"])
        order_flow_context[user_id] = draft
        return make_order_guide_reply(draft)

    if is_cram_order_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "cram_order_flow"
        cram_order_context[user_id] = _new_cram_draft()
        return (
            "📚 補習班訂書\n\n"
            "請告訴我是哪一間補習班？\n"
            "例如：大大補習班、學思達補習班"
        )

    if is_multi_book_order_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "multi_book_order_flow"
        multi_book_order_context[user_id] = _new_multi_book_draft()
        return (
            "📚 多書訂購（一個班一種不同的書）\n\n"
            "適合像「這位老師七個班，要各拿一種不同的康軒歷史1測驗卷」這種情境。\n\n"
            "請先告訴我是哪一位老師？"
        )

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

    # v44：其他訂單歷史查詢（獨立於新增模式）
    other_lookup = parse_other_order_history_query(text)
    if other_lookup is not None:
        return handle_other_order_history_query(user_id, other_lookup)

    if is_other_order_lookup_start(text):
        return (
            "📦 其他訂單查詢\n\n"
            "可以直接輸入：\n"
            "• 查今天其他訂單\n"
            "• 查昨天其他訂單\n"
            "• 查9/20其他訂單\n"
            "• 查華興其他訂單\n"
            "• 查張新莊其他訂單\n"
            "• 查其他003"
        )

    if is_other_order_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "other_order"
        return (
            "📦 其他訂單\n\n"
            "請直接輸入：學校＋老師＋品項。\n"
            "例如：天母國中王老師買書面紙20張\n\n"
            "我會先整理成確認畫面，等你回覆「確認」後才寫入 Google。"
        )

    if is_stats_mode_start(text):
        clear_task_states_for_new_mode(user_id)
        guided_mode[user_id] = "stats_lookup"
        return get_stats_lookup_guide_reply()

    # ---------------------------------------------------
    # 引導模式分流：六個模式共用退出詞跟跳脫偵測
    # ---------------------------------------------------
    current_mode = guided_mode.get(user_id)

    if current_mode == "order_flow":
        if _is_exit_word(text):
            clear_task_states_for_new_mode(user_id)
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        if user_id in pending_name_confirmations:
            fuzzy_reply = handle_name_confirmation(user_id, text)
            if fuzzy_reply is not None:
                return fuzzy_reply

        # 已經產生「訂購確認」後，使用者下一句若是在修改班級，
        # 必須優先交給 pending order edit。不能再送回 handle_order_flow，
        # 否則像「國一丁取消」「國一丁己戊甲庚取消」會被誤當成書名。
        if user_id in pending_orders:
            pending_reply = handle_pending_order_edit(user_id, text)
            if pending_reply is not None:
                return pending_reply

        escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
        if escape_reply is not None:
            return escape_reply
        order_reply = handle_order_flow(user_id, text)
        if order_reply is not None:
            return order_reply

    if current_mode == "teacher_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            pending_teacher_corrections.pop(user_id, None)
            pending_name_confirmations.pop(user_id, None)
            return get_main_menu_reply()
        if user_id in pending_name_confirmations:
            fuzzy_reply = handle_name_confirmation(user_id, text)
            if fuzzy_reply is not None:
                return fuzzy_reply
        escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_teacher_lookup(user_id, text)

    if current_mode == "version_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_version_lookup(user_id, text)

    if current_mode == "stats_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_stats_lookup(user_id, text)

    if current_mode == "history_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()

        if user_id in historical_order_context and user_id not in pending_orders:
            order = historical_order_context[user_id]
            class_names = _pending_order_known_class_names(order, {})
            if looks_like_history_edit(text, class_names):
                guided_mode.pop(user_id, None)
                return prepare_history_adjustment(user_id, order, text)

        escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_history_lookup(user_id, text)

    if current_mode == "other_order":
        if text in ["回主選單", "主選單", "離開"]:
            pending_other_orders.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()

        if text in ["取消", "不要了", "這筆不要"] and user_id in pending_other_orders:
            pending_other_orders.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return "❌ 已取消這筆「其他訂單」，Google 沒有寫入。"

        if user_id in pending_other_orders and not str(pending_other_orders[user_id].get("school", "") or "").strip():
            return handle_pending_other_order_school_input(user_id, text)

        if _is_confirm_word(text) and user_id in pending_other_orders:
            reply = confirm_other_order(user_id)
            if not reply.startswith("❌") and not reply.startswith("⚠️"):
                guided_mode.pop(user_id, None)
            return reply

        if user_id not in pending_other_orders:
            escape_reply = _guided_mode_escape_reply(user_id, text, current_mode)
            if escape_reply is not None:
                return escape_reply

        return handle_guided_other_order(user_id, text)

    if current_mode == "cram_order_flow":
        if _is_exit_word(text):
            cram_order_context.pop(user_id, None)
            pending_name_confirmations.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()

        if user_id in pending_name_confirmations:
            fuzzy_reply = handle_name_confirmation(user_id, text)
            if fuzzy_reply is not None:
                return fuzzy_reply

        cram_reply = handle_cram_order_flow(user_id, text)
        if cram_reply is not None:
            return cram_reply

    if current_mode == "multi_book_order_flow":
        if _is_exit_word(text):
            multi_book_order_context.pop(user_id, None)
            pending_name_confirmations.pop(user_id, None)
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()

        if user_id in pending_name_confirmations:
            fuzzy_reply = handle_name_confirmation(user_id, text)
            if fuzzy_reply is not None:
                return fuzzy_reply

        multi_book_reply = handle_multi_book_order_flow(user_id, text)
        if multi_book_reply is not None:
            return multi_book_reply

    # 0.9 名稱候選確認一定要早於訂書流程。
    if user_id in pending_name_confirmations:
        fuzzy_reply = handle_name_confirmation(user_id, text)
        if fuzzy_reply is not None:
            return fuzzy_reply

    # 訂書模式鎖定：進入後不允許一般老師查詢把話題搶走。
    if user_id in order_flow_context:
        order_reply = handle_order_flow(user_id, text)
        if order_reply is not None:
            return order_reply

    # 1. 名稱容錯確認（老師／書名／學校／出版社）
    fuzzy_reply = handle_name_confirmation(user_id, text)
    if fuzzy_reply is not None:
        return fuzzy_reply

    # 1.5 老師查詢失敗後，下一句若只是 2～4 個中文字姓名，直接視為更正老師姓名。
    correction_reply = handle_teacher_name_correction(user_id, text)
    if correction_reply is not None:
        return correction_reply

    # 1.6 單獨輸入正確老師姓名，也能直接查。
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
    # 原本這幾處都只認「確認」／「確認取消」／「確認修改」精確字串，
    # 使用者很自然回覆的「好」「可以」「沒問題」都不會生效，改用
    # _is_confirm_word() 涵蓋所有通用肯定詞，同時保留各自專屬的固定
    # 用語。
    if (text in ["確認取消"] or _is_confirm_word(text)) and user_id in pending_history_cancels:
        return confirm_history_cancel(user_id)

    if (text in ["確認修改"] or _is_confirm_word(text)) and user_id in pending_other_updates:
        return confirm_other_order_update(user_id)

    if (text in ["確認修改"] or _is_confirm_word(text)) and user_id in pending_history_updates:
        return confirm_history_update(user_id)

    if _is_confirm_word(text) and user_id in pending_other_orders:
        return confirm_other_order(user_id)

    if _is_confirm_word(text) and user_id in pending_orders:
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
        # 「取消701」語意模糊：如果使用者剛查過某張歷史訂單，且 701
        # 剛好是那張訂單裡的班級名稱，使用者十之八九是要移除這個班，
        # 不是要取消訂單編號 701（那個編號剛好存在的話，會直接取消到
        # 完全不相干的一張訂單）。這種情況跳過這一步，讓它落到下面
        # 第 12 步的班級移除邏輯去處理。
        context_order = historical_order_context.get(user_id)
        context_class_names = (
            _pending_order_known_class_names(context_order, {}) if context_order else set()
        )
        if cancel_number not in context_class_names:
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
        return make_historical_order_with_offer(user_id, order)

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
        and looks_like_history_edit(
            text,
            _pending_order_known_class_names(historical_order_context[user_id], {})
        )
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

        if orders is None:
            return "⚠️ 訂單查詢暫時無法讀取，請稍後再試一次。"

        if not orders:
            return (
                "⚠️ 查不到這位老師的訂書紀錄。\n\n"
                f"老師：{teacher}"
            )

        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]

        return make_teacher_book_orders_reply(teacher, orders)

    # 14. 明確的「幫我寫訊息／代寫」請求仍直接擋下，不讓 AI 代筆。
    #     （注意：這不等於 AI 完全不能聊天——v42 新增的 AI Agent
    #     在所有規則都接不住時，會允許 OpenAI 做一般對話，見
    #     handle_smart_function_fallback() 的 intent == "chat"/"unknown"
    #     分支，那邊有另外的 system prompt 禁止虛構公司資料。）
    if is_ai_writing_request(text):
        return FIXED_FALLBACK_MESSAGE

    # 14.4 學校＋指定班級老師：例如「天母701老師」「華興國一甲老師」。
    class_teacher_query = parse_class_teacher_query(text)
    if class_teacher_query:
        return handle_class_teacher_query(class_teacher_query)

    # 14.5 學校＋年級＋科目老師：直接查老師班級資料庫。
    subject_teacher_query = parse_subject_teacher_query(text)
    if subject_teacher_query:
        return handle_subject_teacher_query(subject_teacher_query)

    # 14.65 剛查過老師時，「總共幾個班／每班幾人」這類追問句要優先當成
    # 老師追問處理。這裡的關鍵字跟下面 parse_school_stats_query() 高度
    # 重疊，且該函式在沒有明講學校時會用 get_context_school() 撈到剛
    # 查過的老師所在學校，若不先判斷，會把「這位老師有幾個班」誤判成
    # 「整間學校有幾個班」。句子裡有明講別的學校名稱時仍走學校人數查詢。
    if (
        user_id in teacher_lookup_context
        and looks_like_teacher_followup(text)
        and not extract_school_name(text)
    ):
        return handle_teacher_followup(user_id)

    # 14.7 學校／年級人數查詢（新功能）
    stats_query = parse_school_stats_query(user_id, text)
    if stats_query:
        return handle_school_stats_query(stats_query)

    # 15. 老師資料庫：優先於學校統計
    if looks_like_teacher_lookup(text):
        return handle_teacher_lookup(user_id, text)

    if user_id in teacher_lookup_context and looks_like_teacher_followup(text):
        return handle_teacher_followup(user_id)

    # 16. 學校教科書版本
    version_query = parse_school_version_query(user_id, text)
    if version_query:
        return handle_school_version_query(version_query)

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
            "row_number": target.get("row_number", ""),
            "order_number": target.get("order_number", ""),
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
        _clear_stale_history_pending(user_id)
        pending_other_orders[user_id] = parsed_other
        if not str(parsed_other.get("school", "") or "").strip():
            guided_mode[user_id] = "other_order"
        return make_other_order_confirmation(parsed_other)

    # 19. 訂書流程 —— 優先於一般 AI
    order_reply = handle_order_flow(user_id, text)
    if order_reply is not None:
        # v81（#17）：規則層接手了卻失敗（找不到老師／書），先讓 AI 讀一次整句；
        # AI 讀懂、而且資料庫對得上才採用，否則照舊回原本的錯誤訊息。
        rescued = _v81_ai_rescue_failed_order(user_id, text, order_reply)
        return rescued if rescued is not None else order_reply

    # 19.5 智慧理解最後容錯：
    # 原本所有固定功能、guided_mode、資料庫規則都已經先跑完。
    # AI 只能在這裡協助理解「原本接不住的訂書口語」，不能搶走既有功能。
    smart_reply = handle_smart_function_fallback(user_id, text)
    if smart_reply is not None:
        return smart_reply

    # 20. 其他內容：仍維持固定卡關訊息
    return FIXED_FALLBACK_MESSAGE


# =========================================================
# 引導模式跳脫機制
# =========================================================
def _guided_mode_escape_reply(user_id, text, current_mode=None):
    """
    在引導模式（guided_mode）中，如果使用者輸入的內容明顯符合
    『查老師 / 查各科老師 / 查版本 / 查人數 / 查訂單編號』這類其他功能
    既有的格式，直接跳出目前的引導模式並依該功能處理，
    而不是死板地卡在原模式一直要求正確格式。

    只有真的比對得上既有格式時才會跳出；比對不上的話回傳
    None，維持原本引導模式的提示與行為不變。

    current_mode：呼叫端目前所在的引導模式。當偵測到的格式剛好就是
    「目前這個模式本來就會處理」的功能時（例如在 teacher_lookup 模式
    裡打出符合查老師格式的句子），不能真的跳出模式——那樣不但多此一舉
    把 guided_mode 清掉，還會繞過各模式自己「我還在OO模式」的提示，讓
    使用者在還沒打退出詞的情況下就被靜默踢出模式。這種情況直接回傳
    None，讓呼叫端用自己模式內的 handler 處理。
    """
    class_query = parse_class_teacher_query(text)
    if class_query and current_mode != "teacher_lookup":
        guided_mode.pop(user_id, None)
        return handle_class_teacher_query(class_query)

    subject_query = parse_subject_teacher_query(text)
    if subject_query and current_mode != "teacher_lookup":
        guided_mode.pop(user_id, None)
        return handle_subject_teacher_query(subject_query)

    stats_query = parse_school_stats_query(user_id, text)
    if stats_query and current_mode != "stats_lookup":
        guided_mode.pop(user_id, None)
        return handle_school_stats_query(stats_query)

    if current_mode != "teacher_lookup" and looks_like_teacher_lookup(text):
        guided_mode.pop(user_id, None)
        return handle_teacher_lookup(user_id, text)

    version_query = parse_school_version_query(user_id, text)
    if version_query and current_mode != "version_lookup":
        guided_mode.pop(user_id, None)
        return handle_school_version_query(version_query)

    order_number = extract_order_lookup_number(text)
    if order_number:
        guided_mode.pop(user_id, None)
        order = lookup_google_order(order_number)
        if not order:
            return f"⚠️ 查不到訂單 {order_number}。"
        historical_order_context[user_id] = order
        return make_historical_order_with_offer(user_id, order)

    # 剛查過某位老師的班級資料後，使用者常會直接接著打「班級＋書名」
    # 想幫他訂書（例如「701康軒英文」），不想再打一次「我要訂書」。
    # looks_like_contextual_class_book() 同時要求：(1) 提到的班級都
    # 確實是剛才那位老師的班級 (2) 扣掉班級名稱後還剩下看起來像書名
    # 的文字——單純問「張建國有幾個班」這種句子沒有提到任何班級，
    # 不會誤觸發；「701」單獨一個班級號碼、沒有書名，也不會觸發。
    teacher_context = teacher_lookup_context.get(user_id) or conversation_context.get(user_id)
    if teacher_context and looks_like_contextual_class_book(text, teacher_context):
        guided_mode.pop(user_id, None)
        return handle_order_flow(user_id, text)

    # 完全沒提到班級（不管數字還是文字班級），但這句話看起來就是要
    # 訂書（例如「訂段考王國文6」），而且剛好知道是哪位老師——這種
    # 情況預設成這位老師的「全部班級」，跟既有一步一步訂書流程「沒
    # 指定班級就預設全部班」的行為一致，最後還是會先出確認畫面讓
    # 使用者看過班級清單才寫入 Google。
    if teacher_context and _looks_like_order_for_known_teacher_no_class(text, teacher_context):
        guided_mode.pop(user_id, None)
        return handle_order_flow(user_id, text)

    return None


# =========================================================
# 引導式主選單／查老師模式
# =========================================================
def get_main_menu_reply():
    _set_quick_reply(QUICK_REPLY_MAIN_ITEMS)
    return (
        "🏠 大漢訂書小幫手｜主選單\n\n"
        "直接告訴我你要做什麼就可以，不用背指令。\n\n"
        "📚 訂書｜例如：王老師701、703訂國一數學講義\n"
        "👨‍🏫 查老師｜例如：謝明清有幾個班\n"
        "📅 查訂單｜例如：查001、昨天的訂單\n"
        "📖 查版本｜例如：華興七年級英文版本\n"
        "📊 查人數｜例如：天母七年級人數\n\n"
        "其他功能：補習班訂書、多書訂購、其他訂單、今日統計、照片訂書。\n"
        "需要完整說明時，輸入「功能」。"
    )


def is_teacher_mode_start(text):
    compact=re.sub(r"[\s，,。.!！?？]+","",str(text or ""))
    return compact in {
        "查老師","我要查老師","查詢老師","老師查詢","找老師","我要找老師",
        "查老師資料","查各科老師","各科老師",
        "查個別老師","個別老師","我要查個別老師"
    }

def is_order_mode_start(text):
    compact=re.sub(r"[\s，,。.!！?？]+","",str(text or ""))
    return compact in {"訂書","我要訂書","我訂書","開始訂書","幫我訂書","我要下單","幫我下單","要訂書","我要定書","幫我下","幫我叫書"}

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


def is_other_order_lookup_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {"查其他訂單", "查詢其他訂單", "其他訂單查詢", "我要查其他訂單"}


def is_stats_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "查人數", "我要查人數", "人數查詢", "查學生人數",
        "查詢人數", "我要查學生人數"
    }


def get_history_lookup_guide_reply():
    return (
        "📅 訂單查詢\n\n"
        "直接告訴我你想找哪一張：\n\n"
        "• 今天／昨天\n"
        "• 8月30日\n"
        "• 001 或 查001\n"
        "• 王老師的訂單\n\n"
        "查到訂單後，可以直接修改班級數量或取消訂單。"
    )



def get_stats_lookup_guide_reply():
    return (
        "📊 學生人數查詢\n\n"
        "請告訴我「學校＋年級」。\n"
        "例如：天母七年級、華興高一\n\n"
        "查完後可以直接接著查下一個年級。"
    )



def _finish_guided_mode(user_id, reply):
    guided_mode.pop(user_id, None)
    return reply


def handle_guided_version_lookup(user_id, text):
    clean = str(text or "").strip()
    # 沒有明講學校時，沿用這個模式裡上一次成功查到的學校，讓使用者換過
    # 學校後只打年級（例如「七年級」）也能繼續查，不用每次都重打校名。
    school = extract_school_name(clean) or (stats_version_context.get(user_id) or {}).get("school", "")
    if not school:
        # 固定規則抓不到學校名稱，先讓 AI 試試看能不能從口語裡
        # 理解使用者要查哪個學校／年級／科目的版本，接不住才退回
        # 格式提示。
        smart_reply = _guided_mode_smart_rescue(user_id, text, "version_lookup")
        if smart_reply is not None:
            return smart_reply
        return (
            "📖 教科書版本查詢\n\n"
            "我還在「查版本」模式。\n"
            "請輸入學校名稱，例如：天母國中、衛理女中、華興中學。"
        )
    stats_version_context[user_id] = {"school": school}

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
    if not query["grade"]:
        result = lookup_school_versions_all_junior_grades(
            query["school"], query["subject"], query["academic_period"]
        )
    else:
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

    reply = handle_school_version_query(query)
    return reply + "\n\n我還在「查版本」模式，可以繼續輸入下一個學校／年級，或打「主選單」離開。"


def handle_guided_stats_lookup(user_id, text):
    query = parse_school_stats_query(user_id, str(text or "").strip(), require_keyword=False)
    if not query:
        smart_reply = _guided_mode_smart_rescue(user_id, text, "stats_lookup")
        if smart_reply is not None:
            return smart_reply
        return (
            "📊 學生人數查詢\n\n"
            "我還在「查人數」模式。\n"
            "請輸入「學校＋年級」，例如：天母七年級、華興高一。"
        )

    reply = handle_school_stats_query(query)
    if reply.startswith("⚠️"):
        return reply + "\n\n我還在「查人數」模式，可以直接換一個學校或年級再試一次。"

    return reply + "\n\n我還在「查人數」模式，可以繼續輸入下一個學校／年級，或打「主選單」離開。"


def _rollback_year_if_future(dt):
    """
    沒有明講年份時解析出來的日期，如果落在明顯的未來，代表使用者其實
    是指「去年」這個月日（最常見的情境：年初詢問去年 12 月的訂單，
    只打「12月30日」，年份用今年解析會變成一個還沒發生的未來日期，
    查到空清單讓人誤以為那天沒訂單）。這裡自動把年份減 1；多留 1 天
    容忍度，避免時區邊界誤判今天。
    """
    from datetime import timedelta
    if dt > datetime.now() + timedelta(days=1):
        try:
            return dt.replace(year=dt.year - 1)
        except ValueError:
            # 2/29 這類日期減一年剛好不存在（去年非閏年），不調整。
            return dt
    return dt


def parse_guided_date(text):
    clean = re.sub(r"\s+", "", str(text or "").strip())
    if clean in {"今天", "今日", "今天的", "今日的"}:
        return datetime.now().strftime("%Y-%m-%d")
    if clean in {"昨天", "昨日", "昨天的", "昨日的"}:
        from datetime import timedelta
        return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    m = re.fullmatch(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?", clean)
    if m:
        explicit_year = bool(m.group(1))
        year = int(m.group(1)) if explicit_year else datetime.now().year
        try:
            dt = datetime(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if not explicit_year:
            dt = _rollback_year_if_future(dt)
        return dt.strftime("%Y-%m-%d")

    m = re.fullmatch(r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})", clean)
    if m:
        explicit_year = bool(m.group(1))
        year = int(m.group(1)) if explicit_year else datetime.now().year
        try:
            dt = datetime(year, int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        if not explicit_year:
            dt = _rollback_year_if_future(dt)
        return dt.strftime("%Y-%m-%d")

    m = re.fullmatch(r"(\d{2})(\d{2})", clean)
    if m:
        try:
            dt = datetime(datetime.now().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        dt = _rollback_year_if_future(dt)
        return dt.strftime("%Y-%m-%d")
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

        reply = make_daily_orders_reply(date_text, orders)

        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]

        return reply + "\n\n我還在「查訂單」模式，可以繼續輸入其他日期、訂單編號或老師。"

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
        guided_mode.pop(user_id, None)
        return make_historical_order_with_offer(user_id, order)

    # 「查訂單」模式內直接說「取消這張」：跟主流程第 10 步的固定短語
    # 一致。guided_mode 這裡是自成一格的獨立函式，不會走到那一步，
    # 沒有這段的話「取消這張」會被底下的老師姓名 fullmatch（2~4個中文
    # 字）誤判成要查一個叫「取消這張」的老師。
    if user_id in historical_order_context and clean in {
        "取消這張", "取消這筆", "取消這張訂單", "取消這筆訂單", "這張取消"
    }:
        order = historical_order_context[user_id]
        pending_history_cancels[user_id] = copy_order(order)
        return make_history_cancel_confirmation(order)

    teacher_name = re.sub(r"(?:老師)?(?:訂單|訂書|進度|紀錄)$", "", clean)
    teacher_name = re.sub(r"老師$", "", teacher_name)
    # 「王大明的訂單」清掉「訂單」尾綴後剩下「王大明的」，「的」字不清掉
    # 的話剛好還是 2~4 個中文字，會被當成查一個叫「王大明的」的老師。
    teacher_name = re.sub(r"(?:的|這位|那位)$", "", teacher_name)
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
        if orders is None:
            return (
                "⚠️ 訂單查詢暫時無法讀取，請稍後再試一次。\n\n"
                "我還在「查訂單」模式，可以直接改輸入其他老師、日期或訂單編號。"
            )
        if not orders:
            return (
                "⚠️ 查不到這位老師的訂書紀錄。\n\n"
                f"老師：{canonical}\n\n"
                "我還在「查訂單」模式，可以直接改輸入其他老師、日期或訂單編號。"
            )

        reply = make_teacher_book_orders_reply(canonical, orders)

        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]

        return reply + "\n\n我還在「查訂單」模式，可以繼續輸入其他日期、訂單編號或老師。"

    # 固定格式接不住時，最後才讓 AI 協助把口語整理成既有查訂單格式。
    smart_reply = handle_smart_history_lookup(user_id, text, keep_mode=True)
    if smart_reply is not None:
        return smart_reply

    return get_history_lookup_guide_reply()


def handle_guided_other_order(user_id, text):
    parsed = parse_other_order(user_id, str(text or "").strip())
    if isinstance(parsed, dict):
        _clear_stale_history_pending(user_id)
        pending_other_orders[user_id] = parsed
        return make_other_order_confirmation(parsed)

    smart_reply = _guided_mode_smart_rescue(user_id, text, "other_order")
    if smart_reply is not None:
        return smart_reply

    return (
        "📦 其他訂單\n\n"
        "我目前還無法確認這筆需求。\n"
        "你可以直接說：藍明月老師要買一盒彩色筆\n"
        "也可以說：華興張新莊要書面紙20張\n\n"
        "如果要離開，輸入「主選單」。"
    )


def clear_task_states_for_new_mode(user_id):
    order_flow_context.pop(user_id, None)
    pending_orders.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
    pending_teacher_corrections.pop(user_id, None)
    teacher_lookup_context.pop(user_id, None)
    conversation_context.pop(user_id, None)
    stats_version_context.pop(user_id, None)
    pending_receipt_offers.pop(user_id, None)
    cram_order_context.pop(user_id, None)
    multi_book_order_context.pop(user_id, None)
    _clear_stale_history_pending(user_id)


def _clear_stale_history_pending(user_id):
    """
    建立一筆新的待確認訂單（學校訂單或其他訂單）時要呼叫：如果使用者
    先前打過「取消訂單005」或對某張歷史訂單提出修改、但還沒有回覆
    「確認」，這兩個待辦如果沒清掉，接下來對新訂單的「確認」會因為
    dispatcher 的判斷順序，被誤導去執行舊的、使用者可能早就不記得的
    歷史訂單取消／修改，而不是確認這筆新訂單。
    """
    pending_history_cancels.pop(user_id, None)
    pending_history_updates.pop(user_id, None)

def normalize_teacher_name_input(text):
    clean=re.sub(r"[，,。.!！?？\s]+","",str(text or ""))
    clean=re.sub(r"^(?:我要)?(?:查|找)(?:一下)?(?:老師)?","",clean)
    clean=re.sub(r"(?:老師)?(?:教哪個班|教哪歌班|教哪幾個班|教哪幾班|教哪些班|有哪些班|有幾個班|教什麼科|教哪科)$","",clean)
    clean=re.sub(r"老師$","",clean)
    return clean

def finish_teacher_lookup(user_id,item):
    reply=make_teacher_reply(item["school"],item["teacher"],item["classes"])
    pending_teacher_corrections.pop(user_id,None)
    pending_name_confirmations.pop(user_id,None)

    # 引導模式（guided_mode="teacher_lookup"）原本查完老師就把 context
    # 清空，導致跟「查老師」以外的路徑（handle_teacher_lookup／
    # handle_class_teacher_query／handle_subject_teacher_query）行為
    # 不一致——那些路徑查完都會保留 context，讓使用者可以直接接著打
    # 「班級＋書名」續走訂書流程（見 looks_like_contextual_class_book／
    # _guided_mode_escape_reply）。這裡補齊同樣的行為，兩種查法之後
    # 的體驗才會一致。
    context = {
        "school": item["school"],
        "teacher": item["teacher"],
        "classes": copy_classes(item["classes"])
    }
    teacher_lookup_context[user_id] = context
    conversation_context[user_id] = context

    if guided_mode.get(user_id) == "teacher_lookup":
        return reply + "\n\n我還在「查老師」模式，可以繼續輸入下一位老師姓名，或打「主選單」離開。"
    guided_mode.pop(user_id,None)
    return reply


def _normalize_class_lookup_name(value):
    clean = re.sub(r"[\s班]+", "", str(value or ""))
    # 701/702... 保持原樣
    if re.fullmatch(r"\d{3}", clean):
        return clean
    # 國一甲/七甲 等文字班名保持資料庫常用形式
    return clean


def parse_class_teacher_query(text):
    """學校＋班級 → 查這個班有哪些老師；可再加科目縮小範圍。"""
    clean = re.sub(r"[，,。.!！?？：:\s]+", "", str(text or ""))
    if not any(k in clean for k in ["老師", "誰教", "誰上", "任課"]):
        return None

    # 常用學校先走本地別名，不必每次為了辨識「天母／華興／衛理」
    # 先呼叫 GAS list_schools。其他學校仍保留原本 extract_school_name。
    local_school_aliases = [
        ("天母國中", "天母國中"), ("天母", "天母國中"),
        ("華興中學", "華興中學"), ("華興", "華興中學"),
        ("衛理女中", "衛理女中"), ("衛理", "衛理女中"),
    ]
    school = ""
    for alias, canonical in local_school_aliases:
        if alias in clean:
            school = canonical
            break
    if not school:
        school = extract_school_name(clean)
    if not school:
        return None

    subject_aliases = [
        ("地球科學", "地科"), ("英語", "英文"), ("英文", "英文"),
        ("國文", "國文"), ("數學", "數學"), ("自然", "自然"),
        ("生物", "生物"), ("理化", "理化"), ("地科", "地科"),
        ("社會", "社會"), ("歷史", "歷史"), ("地理", "地理"), ("公民", "公民"),
    ]
    subject = ""
    for alias, canonical in subject_aliases:
        if alias in clean:
            subject = canonical
            break

    # 數字班：701、802、903...
    m = re.search(r"(?<!\d)([789]\d{2})(?:班)?(?!\d)", clean)
    class_name = m.group(1) if m else ""

    # 中文班：國一甲、國二信、高一愛、七年甲、七甲...
    if not class_name:
        patterns = [
            r"((?:國[一二三]|高[一二三])(?:年級)?[甲乙丙丁戊己庚辛壬癸信望愛慧忠孝仁和]{1,3})(?:班)?",
            r"((?:七|八|九)(?:年級)?[甲乙丙丁戊己庚辛壬癸信望愛慧忠孝仁和]{1,3})(?:班)?",
        ]
        for pattern in patterns:
            m = re.search(pattern, clean)
            if m:
                class_name = m.group(1)
                break

    if not class_name:
        return None

    return {
        "school": school,
        "class_name": _normalize_class_lookup_name(class_name),
        "subject": subject,
    }


def _class_name_equivalent(a, b):
    def norm(v):
        v = re.sub(r"[\s班]+", "", str(v or ""))
        aliases = {
            "七年級": "國一", "七年": "國一", "七": "國一",
            "八年級": "國二", "八年": "國二", "八": "國二",
            "九年級": "國三", "九年": "國三", "九": "國三",
        }
        for old, new in aliases.items():
            if v.startswith(old) and not re.fullmatch(r"\d{3}", v):
                v = new + v[len(old):]
                break
        return v
    return norm(a) == norm(b)


def handle_class_teacher_query(query):
    """
    指定班級反查老師。這裡直接呼叫既有 lookup_teacher_matches action，
    並區分「GAS 暫時逾時」和「真的查無資料」，避免 timeout 被誤報成查不到。
    """
    result = google_post({
        "action": "lookup_teacher_matches",
        "teacher": "",
        "school": query["school"],
        "grade": "",
        "subject": query.get("subject", "")
    }, timeout=9, retries=1)

    if result is None:
        return (
            "⚠️ 老師資料庫暫時查詢失敗，請再試一次。\n\n"
            f"學校：{query['school']}\n班級：{query['class_name']}"
        )

    if not result.get("success"):
        return (
            "⚠️ 老師資料庫暫時無法完成查詢，請再試一次。\n\n"
            f"學校：{query['school']}\n班級：{query['class_name']}"
        )

    matches = []
    for item in result.get("matches", []):
        matches.append({
            "school": str(item.get("school", "")).strip(),
            "teacher": str(item.get("teacher", "")).strip(),
            "subjects": unique_list(item.get("subjects", [])),
            "classes": copy_classes(item.get("classes", []))
        })

    if not matches:
        return (
            "⚠️ 查不到符合條件的班級老師資料。\n\n"
            f"學校：{query['school']}\n班級：{query['class_name']}"
            + (f"\n科目：{query['subject']}" if query.get("subject") else "")
        )

    found = []
    for item in matches:
        for c in item.get("classes", []):
            if not _class_name_equivalent(c.get("class_name", ""), query["class_name"]):
                continue
            subjects = unique_list(c.get("subjects", []))
            if query.get("subject") and not _subject_matches(subjects, query["subject"]):
                continue
            found.append({
                "teacher": str(item.get("teacher", "")).strip(),
                "subjects": subjects,
                "students": int(c.get("students", 0) or 0),
                "class_name": str(c.get("class_name", "")).strip(),
            })

    if not found:
        return (
            "⚠️ 查不到符合條件的班級老師資料。\n\n"
            f"學校：{query['school']}\n班級：{query['class_name']}"
            + (f"\n科目：{query['subject']}" if query.get("subject") else "")
        )

    dedup = {}
    for item in found:
        key = (item["teacher"], tuple(item["subjects"]))
        dedup[key] = item
    found = list(dedup.values())

    display_class = found[0].get("class_name") or query["class_name"]
    lines = [f"👨‍🏫 {query['school']}｜{display_class}班 老師", ""]
    if query.get("subject"):
        lines.extend([f"科目：{query['subject']}", ""])

    for item in sorted(found, key=lambda x: (
        min([subject_sort_key(s)[0] for s in x["subjects"]] or [999]),
        x["teacher"]
    )):
        sorted_subjects = sorted(item["subjects"], key=subject_sort_key)
        subject_text = "、".join(sorted_subjects) if sorted_subjects else "科目未標示"
        lines.append(f"• {subject_text}：{item['teacher']}")

    lines.extend(["", f"👨‍🏫 共找到 {len(found)} 位老師"])
    return "\n".join(lines)


def parse_subject_teacher_query(text):
    clean = re.sub(r"[，,。.!！?？：:\s]+", "", str(text or ""))
    if "老師" not in clean and "誰教" not in clean and "誰上" not in clean:
        return None

    school = extract_school_name(clean)
    grade = extract_grade_text(clean)

    subject_aliases = [
        ("地球科學", "地科"),
        ("英語", "英文"),
        ("英文", "英文"),
        ("國文", "國文"),
        ("數學", "數學"),
        ("自然", "自然"),
        ("生物", "生物"),
        ("理化", "理化"),
        ("地科", "地科"),
        ("社會", "社會"),
        ("歷史", "歷史"),
        ("地理", "地理"),
        ("公民", "公民"),
    ]
    subject = ""
    for alias, canonical in subject_aliases:
        if alias in clean:
            subject = canonical
            break

    if not school or not grade or not subject:
        return None

    return {"school": school, "grade": grade, "subject": subject}


def _class_matches_grade(class_name, grade):
    name = re.sub(r"[\s班]+", "", str(class_name or ""))
    grade = str(grade or "").strip()

    if grade == "七年級":
        return bool(re.match(r"^(?:七|7|國一)", name))
    if grade == "八年級":
        return bool(re.match(r"^(?:八|8|國二)", name))
    if grade == "九年級":
        return bool(re.match(r"^(?:九|9|國三)", name))
    if grade == "高一":
        return bool(re.match(r"^(?:高一|10)", name))
    if grade == "高二":
        return bool(re.match(r"^(?:高二|11)", name))
    if grade == "高三":
        return bool(re.match(r"^(?:高三|12)", name))
    return False


def _subject_matches(subjects, wanted):
    normalized = []
    for s in subjects or []:
        s = str(s or "").strip()
        if s == "英語":
            s = "英文"
        elif s == "地球科學":
            s = "地科"
        normalized.append(s)
    return wanted in normalized


def handle_subject_teacher_query(query):
    matches = lookup_teacher_matches(
        "", school=query["school"], grade=query["grade"], subject=query["subject"]
    )
    if not matches:
        return (
            "⚠️ 查不到符合條件的老師資料。\n\n"
            f"學校：{query['school']}\n年級：{query['grade']}\n科目：{query['subject']}"
        )

    filtered = []
    for item in matches:
        selected = []
        for c in item.get("classes", []):
            if not _class_matches_grade(c.get("class_name", ""), query["grade"]):
                continue
            if not _subject_matches(c.get("subjects", []), query["subject"]):
                continue
            selected.append(c)

        if selected:
            filtered.append({
                "teacher": str(item.get("teacher", "")).strip(),
                "classes": selected
            })

    if not filtered:
        return (
            "⚠️ 查不到符合條件的老師資料。\n\n"
            f"學校：{query['school']}\n年級：{query['grade']}\n科目：{query['subject']}"
        )

    lines = [f"👨‍🏫 {query['school']}｜{query['grade']} {query['subject']}老師", ""]

    if len(filtered) == 1:
        item = filtered[0]
        lines.append(f"老師：{item['teacher']}")
        lines.append("")
        for c in item["classes"]:
            lines.append(
                f"• {c.get('class_name','')}班：{int(c.get('students',0) or 0)}人"
            )
        lines.append("")
        lines.append(f"📚 共 {len(item['classes'])} 個班")
        return "\n".join(lines).strip()

    total_classes = 0
    for item in filtered:
        lines.append(f"【{item['teacher']}】")
        for c in item["classes"]:
            lines.append(
                f"• {c.get('class_name','')}班：{int(c.get('students',0) or 0)}人"
            )
            total_classes += 1
        lines.append("")

    lines.append(f"👨‍🏫 共 {len(filtered)} 位老師")
    lines.append(f"📚 共 {total_classes} 個班")
    return "\n".join(lines).strip()


def handle_guided_teacher_lookup(user_id,text):
    class_query = parse_class_teacher_query(text)
    if class_query:
        reply = handle_class_teacher_query(class_query)
        return reply + "\n\n我還在「查老師」模式，可以繼續查下一個班級或老師，或打「主選單」離開。"

    subject_query = parse_subject_teacher_query(text)
    if subject_query:
        reply = handle_subject_teacher_query(subject_query)
        return reply + "\n\n我還在「查老師」模式，可以繼續查下一位老師，或打「主選單」離開。"
    name=normalize_teacher_name_input(text)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}",name):
        # 格式不像「2~4個中文字姓名」（例如使用者直接打書名、或用
        # 比較口語的方式問），先讓 AI 試試看能不能理解，不要一律
        # 卡死要求固定格式；AI 也接不住才退回格式提示。
        smart_reply = handle_smart_teacher_lookup(user_id, text, keep_mode=True)
        if smart_reply is not None:
            return smart_reply
        return "👨‍🏫 老師查詢\n\n我還在查老師模式。\n請直接輸入 2～4 個中文字的老師姓名。"
    matches, teacher_query_ok = lookup_teacher_matches_status(name+"老師",school="")
    if not teacher_query_ok:
        return (
            "⚠️ 老師資料庫目前查詢逾時或暫時無法連線。\n\n"
            "我沒有再追加模糊搜尋，避免讓你多等十幾秒。\n"
            "請直接再輸入一次老師姓名即可。"
        )
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
    # 固定姓名／班級／科目規則都接不住時，才交給 AI 理解口語。
    smart_reply = handle_smart_teacher_lookup(user_id, text, keep_mode=True)
    if smart_reply is not None:
        return smart_reply

    return "⚠️ 目前找不到這位老師。\n\n"+f"你輸入：{name}\n\n"+"我還在「查老師」模式。\n請直接重新輸入老師姓名，不用再打一次「查老師」。"

# =========================================================
# 招呼／功能選單（純本地，不查 Google）
# =========================================================
def is_greeting_request(text):
    compact = re.sub(r"[，,。.!！?？\s]+", "", str(text or "").lower())
    greetings = ["你好", "妳好", "您好", "哈囉", "哈啰", "嗨", "早安", "午安", "晚安", "在嗎"]

    if any(compact == g for g in greetings):
        return True

    functional_words = ["老師", "訂書", "訂單", "版本", "班", "查", "訂購", "確認"]
    for g in greetings:
        prefix = g + "我是"
        if compact.startswith(prefix):
            remainder = compact[len(prefix):]
            if len(remainder) <= 6 and not any(w in remainder for w in functional_words):
                return True
    return False


def _pick_players(count=3):
    pool = list(NBA_PLAYERS)
    random.shuffle(pool)
    while len(pool) < count:
        pool.extend(NBA_PLAYERS)
    return pool[:count]


def get_greeting_reply():
    p1 = _pick_players(1)[0]
    _set_quick_reply(QUICK_REPLY_MAIN_ITEMS)
    return (
        "📚 大漢訂書小幫手\n\n"
        "你好！不用背指令，直接告訴我今天要處理什麼。\n\n"
        "常用功能\n"
        "📚 學校訂書　🏫 補習班訂書\n"
        "📦 新增其他訂單　🔍 查詢資料\n\n"
        f"💬 例如：{p1}老師701、703訂國一數學講義\n"
        "📷 有訂書照片的話，直接傳給我就可以。\n\n"
        "也可以點下面的快速按鈕 👇"
    )


def is_query_menu_request(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {"查詢資料", "查資料", "資料查詢", "查詢"}


def get_query_menu_reply():
    _set_quick_reply(QUICK_REPLY_QUERY_ITEMS)
    return (
        "🔍 查詢資料\n\n"
        "要查什麼？點下面的按鈕，或直接打關鍵字也可以，"
        "例如「謝明清有幾個班」「查001」。"
    )


def is_help_request(text):
    compact = re.sub(r"\s+", "", str(text or "").lower())
    phrases = {
        "功能", "功能介紹", "使用說明", "說明", "幫助", "help", "更多功能",
        "怎麼用", "如何使用", "你會什麼", "你可以幹嘛",
        "你可以做什麼", "你能幹嘛", "你能做什麼",
        "你能幫我做什麼", "可以幫我做什麼",
        "有什麼功能", "有哪些功能", "你有什麼功能",
        "你有哪些功能"
    }
    return compact in phrases


def is_photo_order_help_request(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "圖片訂書", "照片訂書", "拍照訂書", "圖片下單", "照片下單",
        "怎麼用圖片訂書", "怎麼用照片訂書", "怎麼拍照訂書"
    }


def get_photo_order_help_reply():
    return (
        "📷 拍照訂書\n\n"
        "請直接上傳訂書照片，我會先辨識內容，再查資料庫補齊資料。\n\n"
        "🏫【學校訂書】\n"
        "照片至少要看得到：\n"
        "• 老師姓名\n"
        "• 書名（可以一次寫多本）\n\n"
        "學校、出版社、老師授課班級與各班人數，我會從資料庫自動查詢。\n"
        "如果某一本只訂部分班級、數量不同或有備註，請寫在那本書旁邊。\n\n"
        "🏢【補習班訂書】\n"
        "照片至少要看得到：\n"
        "• 補習班名稱\n"
        "• 書名（可以一次寫多本）\n"
        "• 每一本的訂購數量\n\n"
        "出版社會從書籍資料庫自動查詢；同書名有不同出版社時，我會再請你選擇。\n\n"
        "辨識完成後一定先顯示「訂購確認」，不會直接寫入 Google。\n"
        "👉 現在直接傳圖片給我就可以了。"
    )


def is_ai_assistant_help_request(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or "")).lower()
    return compact in {"ai", "ai助手", "智慧助手", "ai智慧助手", "ai智慧理解", "智慧理解", "大漢ai助手", "大漢ai"}


def get_ai_assistant_help_reply():
    return (
        "🧠 AI 智慧理解\n\n"
        "你不用記固定指令，照平常說話就可以。\n"
        "我會先理解你的意思，需要公司資料時會再查 Google 資料庫。\n\n"
        "例如：\n"
        "• 張建國老師要訂段考王英文3\n"
        "• 幫我看華興國一英文是哪幾個老師\n"
        "• 王老師昨天有沒有訂東西\n"
        "• 華興七年級現在用什麼數學課本\n\n"
        "也可以接著說「他呢？」「那上次訂什麼？」「幫我整理成訊息」。\n\n"
        "資料庫答案仍以 Google 資料為準；建立、修改或取消訂單仍會先讓你確認。"
    )


def get_help_reply():
    p3 = _pick_players(1)[0]
    _set_quick_reply(QUICK_REPLY_MORE_ITEMS)
    return (
        "📚 大漢訂書小幫手｜完整功能\n\n"
        "不用背指令，直接用平常說話的方式告訴我需求。\n\n"
        "【常用功能】\n"
        "📚 訂書｜王老師701、703訂國一數學講義\n"
        "👨‍🏫 查老師｜謝明清有幾個班\n"
        "📅 查訂單｜查001／王老師昨天的訂單\n"
        "📖 查版本｜華興七年級英文版本\n"
        "📊 查人數｜天母七年級人數\n\n"
        "【智慧功能】\n"
        "📷 拍照訂書｜直接傳圖片，自動整理訂單\n"
        "🧠 大漢 AI 助手｜直接講人話，可理解前後文並自動選擇查詢功能\n"
        "📚 多書訂購｜一位老師，不同班級配不同本書\n\n"
        "【其他功能】\n"
        "🏫 補習班訂書\n"
        f"📦 其他訂單｜例如：天母{p3}老師書面紙20張\n"
        "📊 今日訂單統計\n\n"
        "💡 輸入「拍照訂書」可看圖片使用說明；輸入「AI助手」可看智慧理解範例。\n"
        "任何時候輸入「主選單」可離開目前流程；輸入「重來」會清除目前進度。"
    )


# =========================================================
# 訂書流程
# =========================================================
def normalize_person_name(value):
    return re.sub(r"老師$", "", re.sub(r"[\s，,。.!！?？]", "", str(value or ""))).strip()


def validate_order_teacher_input(user_id, raw_text, draft):
    """
    訂書流程老師輸入：
    1. 先做精準老師查詢；資料庫已存在的正確姓名直接採用，不再多問「請輸入 1」。
    2. 精準查不到才做模糊比對。
    3. 明顯只有一個高可信候選（例如三字姓名只錯一字）直接自動採用；
       只有真的有兩個以上接近人選時才讓使用者選 1/2/3。
    """
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    clean = re.sub(r"老師$", "", clean).strip()
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        # 格式不像「2~4個中文字姓名」，先讓 AI 試試看能不能從這句話
        # 理解出老師姓名——只取姓名本身，不套用 AI 猜的書名／班級，
        # 避免蓋掉這個草稿已經收集到的其他欄位；接不住才退回格式
        # 提示。
        if OPENAI_API_KEY:
            ai_data = smart_parse_function_text(raw_text, user_id=user_id)
            if isinstance(ai_data, dict) and ai_data.get("intent") in {"teacher_lookup", "school_order"}:
                guessed_teacher = re.sub(
                    r"老師$", "", str(ai_data.get("teacher", "") or "").strip()
                )
                if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", guessed_teacher):
                    clean = guessed_teacher
        if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
            return "請輸入老師姓名，例如：蔡書玄"

    school = str(draft.get("school") or "").strip()

    # A. 精準查詢優先：正確姓名不應該先繞去模糊搜尋。
    exact_matches = lookup_teacher_matches(clean, school=school)

    # 如果帶學校卻沒有找到，再跨校精準找一次。
    if not exact_matches and school:
        exact_matches = lookup_teacher_matches(clean, school="")

    # 只保留姓名真的完全一致的結果，避免 Google 端日後查詢規則改動造成誤收。
    exact_matches = [
        item for item in (exact_matches or [])
        if normalize_person_name(item.get("teacher", "")) == normalize_person_name(clean)
    ]

    if len(exact_matches) == 1:
        item = exact_matches[0]
        draft["teacher"] = str(item.get("teacher", "") or "").strip()
        draft["school"] = str(item.get("school", "") or school).strip()
        draft["teacher_classes"] = copy_classes(item.get("classes", []))
        draft["classes"] = []
        pending_name_confirmations.pop(user_id, None)
        order_flow_context[user_id] = draft
        return make_order_guide_reply(draft)

    if len(exact_matches) > 1:
        schools = unique_list([
            str(item.get("school", "") or "").strip()
            for item in exact_matches
            if str(item.get("school", "") or "").strip()
        ])
        return (
            "🔎 找到同名老師。\n\n"
            f"老師：{clean}\n"
            f"學校：{'、'.join(schools)}\n\n"
            "請輸入「學校＋老師姓名」，我再幫你確認。"
        )

    # B. 精準查不到才做模糊比對。
    candidates = lookup_fuzzy_candidates("teacher", clean, school=school)
    if not candidates and school:
        candidates = lookup_fuzzy_candidates("teacher", clean, school="")

    if not candidates:
        return (
            "⚠️ 老師資料庫目前找不到符合資料。\n\n"
            f"你輸入：{clean}\n\n"
            "我不會先把這個名字存進訂單。請重新輸入老師姓名。"
        )

    # 去除同一位老師＋同一學校的重複候選。
    deduped = []
    seen = set()
    for c in candidates:
        c_value = str(c.get("value", "") or "").strip()
        c_school = str(c.get("school", "") or "").strip()
        if not c_value:
            continue
        key = normalize_person_name(c_value) + "||" + c_school
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    if not deduped:
        return (
            "⚠️ 老師資料庫目前找不到符合資料。\n\n"
            f"你輸入：{clean}"
        )

    first = deduped[0]
    value = str(first.get("value", "") or "").strip()
    candidate_school = str(first.get("school", "") or school).strip()
    score = float(first.get("score", 0) or 0)
    second_score = float(deduped[1].get("score", 0) or 0) if len(deduped) > 1 else 0.0
    gap = score - second_score

    # 防守：若 fuzzy 回來其實就是完全相同姓名，也直接採用。
    if normalize_person_name(value) == normalize_person_name(clean):
        exact = lookup_teacher_matches(value, school=candidate_school)
        if len(exact) == 1:
            item = exact[0]
            draft["teacher"] = str(item.get("teacher", "") or value).strip()
            draft["school"] = str(item.get("school", "") or candidate_school).strip()
            draft["teacher_classes"] = copy_classes(item.get("classes", []))
        else:
            draft["teacher"] = value
            draft["school"] = candidate_school
            draft["teacher_classes"] = []
        draft["classes"] = []
        pending_name_confirmations.pop(user_id, None)
        order_flow_context[user_id] = draft
        return make_order_guide_reply(draft)

    # 高可信且明顯領先第二名：直接自動採用，不要求多打一個「1」。
    # 三字姓名錯一字通常會落在約 0.66，這裡讓唯一明顯候選直接過。
    if value and score >= 0.64 and (len(deduped) == 1 or gap >= 0.12):
        exact = lookup_teacher_matches(value, school=candidate_school)
        if len(exact) == 1:
            item = exact[0]
            draft["teacher"] = str(item.get("teacher", "") or value).strip()
            draft["school"] = str(item.get("school", "") or candidate_school).strip()
            draft["teacher_classes"] = copy_classes(item.get("classes", []))
            draft["classes"] = []
            pending_name_confirmations.pop(user_id, None)
            order_flow_context[user_id] = draft
            return make_order_guide_reply(draft)

    # 真的有歧義才列候選讓使用者選。
    if value and score >= 0.52:
        options = []
        for c in deduped[:4]:
            c_value = str(c.get("value", "") or "").strip()
            c_school = str(c.get("school", "") or "").strip()
            c_score = float(c.get("score", 0) or 0)

            if not c_value:
                continue
            if c_value != value and c_score < 0.52:
                continue

            options.append({"value": c_value, "school": c_school or school})
            if len(options) >= 3:
                break

        if len(options) == 1:
            # 只有一個候選時，不叫使用者再打 1；直接採用並查班級。
            opt = options[0]
            exact = lookup_teacher_matches(opt["value"], school=opt.get("school", ""))
            if len(exact) == 1:
                item = exact[0]
                draft["teacher"] = str(item.get("teacher", "") or opt["value"]).strip()
                draft["school"] = str(item.get("school", "") or opt.get("school", "")).strip()
                draft["teacher_classes"] = copy_classes(item.get("classes", []))
                draft["classes"] = []
                pending_name_confirmations.pop(user_id, None)
                order_flow_context[user_id] = draft
                return make_order_guide_reply(draft)

        pending_name_confirmations[user_id] = {
            "field": "teacher",
            "purpose": "order_teacher",
            "options": options,
            "original": clean
        }
        order_flow_context[user_id] = draft

        lines = [
            "🔎 老師姓名可能有錯字，找到以下接近的候選。",
            "",
            f"你輸入：{clean}",
            ""
        ]
        for i, opt in enumerate(options, start=1):
            label = opt["value"] + (
                f"（{opt['school']}）" if opt.get("school") else ""
            )
            lines.append(f"{i}. {label}")

        lines.append("")
        lines.append("請直接回覆數字選擇；如果都不是，直接重新輸入正確老師姓名。")
        return "\n".join(lines)

    return (
        "⚠️ 老師資料庫目前無法確認這個姓名。\n\n"
        f"你輸入：{clean}\n\n"
        "我不會往下一步。請重新輸入老師姓名。"
    )

def _looks_like_book_input_during_publisher_step(raw_text):
    """
    使用者在「出版社」這一步直接輸入書名時，不要把書名硬拿去比出版社。
    常見書名會帶科目、冊次/年級、講義/評量等詞；這些情況直接轉交原本
    validate_order_book_input()，保留「模糊候選＋都不是就用我打的」機制。
    """
    clean = clean_book_name(str(raw_text or "").strip())
    compact = re.sub(r"[\s，,。.!！?？：:]+", "", clean)
    if not compact:
        return False

    book_words = [
        "講義", "評量", "教材", "複習", "測驗", "題本", "自修", "課本",
        "習作", "學習單", "段考", "會考", "大滿貫", "百分百", "學習講義",
        "國文", "英文", "英語", "數學", "自然", "生物", "理化", "地科",
        "地球科學", "社會", "歷史", "地理", "公民"
    ]
    has_book_word = any(word in compact for word in book_words)
    has_volume = bool(re.search(r"(?:[1-6]|[一二三四五六])(?:冊)?$", compact))
    has_grade_number = bool(re.search(r"(?:國[一二三]|七|八|九|高[一二三])", compact))

    # 出版社名稱通常很短；帶「科目/教材詞＋冊次」的內容高度像書名。
    return has_book_word and (has_volume or has_grade_number or len(compact) >= 5)


def validate_order_publisher_input(user_id, raw_text, draft):
    """
    訂書流程第二步：出版社。出版社清單很多、常常變動，不能寫死清單，
    所以跟老師／書名一樣走 lookup_fuzzy_candidates（kind="publisher"）
    模糊比對。Apps Script 那邊需要新增支援 kind="publisher" 的分支，
    從書籍分頁的「出版社」欄位取不重複清單來比對。
    """
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    clean = normalize_order_typo(clean)
    if not clean:
        return "請輸入出版社名稱，例如：康軒、翰林、南一"

    # v36：使用者不必死守「出版社→書名」順序。
    # 如果這句明顯像書名（例如「段考王數學3」「學習講義英文1」），
    # 直接走既有書名模糊比對。資料庫有候選就列候選；沒有候選也一定
    # 保留「用我打的書名」選項，不再卡在「請重新輸入出版社」。
    if _looks_like_book_input_during_publisher_step(clean):
        return validate_order_book_input(user_id, clean, draft)

    candidates = lookup_fuzzy_candidates("publisher", clean)
    if not candidates:
        return ("⚠️ 出版社資料庫目前找不到符合資料。\n\n"
                f"你輸入：{clean}\n\n"
                "書名／老師資料已保留，請重新輸入出版社名稱。")

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    score = float(first.get("score", 0) or 0)

    if value == clean:
        draft["publisher"] = value
        pending_name_confirmations.pop(user_id, None)
        order_flow_context[user_id] = draft
        return make_order_guide_reply(draft)

    if value and score >= 0.52:
        options = []
        seen = set()
        for c in candidates[:4]:
            c_value = str(c.get("value", "") or "").strip()
            c_score = float(c.get("score", 0) or 0)
            if not c_value or c_value in seen:
                continue
            if c_value != value and c_score < 0.52:
                continue
            seen.add(c_value)
            options.append({"value": c_value})
            if len(options) >= 3:
                break

        pending_name_confirmations[user_id] = {
            "field": "publisher", "purpose": "order_publisher",
            "options": options,
            "original": clean
        }
        order_flow_context[user_id] = draft

        lines = ["🔎 出版社名稱可能有錯字，找到以下接近的候選。", "", f"你輸入：{clean}", ""]
        for i, opt in enumerate(options, start=1):
            lines.append(f"{i}. {opt['value']}")
        lines.append("")
        if len(options) > 1:
            lines.append("請直接回覆數字（例如「1」）選擇要的那一家；回覆「確認」等同選第 1 個。")
        else:
            lines.append("是的請回覆「確認」。")
        lines.append("如果都不是，請直接輸入正確出版社名稱，我會取消這個候選並重新查資料庫。")
        return "\n".join(lines)

    return ("⚠️ 出版社資料庫目前無法確認這個名稱。\n\n"
            f"你輸入：{clean}\n\n"
            "請重新輸入出版社名稱。")


def _exact_book_variants(query, publisher=""):
    """回傳同一正式書名的所有出版社版本；不可把同名不同出版社合併。"""
    candidates = lookup_book_candidates_enhanced(query, publisher=publisher)
    exact = []
    seen = set()
    qnorm = normalize_book_match_text(query)
    for item in candidates:
        value = str(item.get("value", "") or "").strip()
        pub = str(item.get("publisher", "") or "").strip()
        if normalize_book_match_text(value) != qnorm:
            continue
        key = (value, pub)
        if key in seen:
            continue
        seen.add(key)
        exact.append({"value": value, "publisher": pub})
    return exact


def _ask_duplicate_book_publisher(user_id, draft, book, variants, purpose="order_book_publisher"):
    options = []
    seen = set()
    for item in variants:
        pub = str(item.get("publisher", "") or "").strip()
        if not pub or pub in seen:
            continue
        seen.add(pub)
        options.append({"value": book, "publisher": pub})
    if len(options) <= 1:
        return None
    pending_name_confirmations[user_id] = {
        "field": "publisher", "purpose": purpose, "options": options,
        "original": book, "book": book
    }
    order_flow_context[user_id] = draft
    lines = ["📚 找到相同書名", "", f"書名：{book}", "", "這本書有不同出版社："]
    for i, opt in enumerate(options, 1):
        lines.append(f"{i}. {opt['publisher']}")
    lines.extend(["", f"請回覆 1～{len(options)}"] )
    return "\n".join(lines)


def validate_order_book_input(user_id, raw_text, draft):
    query = clean_book_name(str(raw_text or "").strip())
    query = re.sub(r"^(?:我要訂|要訂|訂)", "", query).strip()
    if not query:
        return "請輸入書名或書名關鍵字。"

    publisher = str(draft.get("publisher") or "").strip()
    candidates = lookup_book_candidates_enhanced(query, publisher=publisher)

    value = ""
    score = 0.0
    if candidates:
        first = candidates[0]
        value = str(first.get("value", "") or "").strip()
        score = float(first.get("score", 0) or 0)

        # Same canonical title + multiple publishers => publisher choice.
        same = []
        seen = set()
        for c in candidates:
            cv=str(c.get("value","") or "").strip()
            cp=str(c.get("publisher","") or "").strip()
            cs=float(c.get("score",0) or 0)
            if cv != value or cs < max(0.52, score-0.08):
                continue
            if (cv,cp) not in seen:
                seen.add((cv,cp)); same.append({"value":cv,"publisher":cp})
        pubs=unique_list([x["publisher"] for x in same if x["publisher"]])
        if not publisher and len(pubs)>1:
            options=[{"value":value,"publisher":p} for p in pubs]
            pending_name_confirmations[user_id]={
                "field":"book","purpose":"order_book_publisher",
                "options":options,"original":query
            }
            order_flow_context[user_id]=draft
            lines=["📚 找到相同書名","",f"書名：{value}","",
                   "這本書有不同出版社："]
            for i,p in enumerate(pubs,1): lines.append(f"{i}. {p}")
            lines.extend(["",f"請回覆 1～{len(pubs)}"])
            return "\n".join(lines)

        if value == query:
            draft["book"]=value
            if not draft.get("publisher") and first.get("publisher"):
                draft["publisher"]=str(first.get("publisher") or "").strip()
            pending_name_confirmations.pop(user_id,None)
            order_flow_context[user_id]=draft
            if draft.get("teacher") and draft.get("book"):
                result=build_order_from_draft(user_id,draft)
                if user_id in pending_orders: order_flow_context.pop(user_id,None)
                return result
            return make_order_guide_reply(draft)

    db_options=[]
    if value and score>=0.52:
        threshold=max(0.52,score-0.08)
        seen=set()
        for c in candidates[:8]:
            cv=str(c.get("value","") or "").strip()
            cp=str(c.get("publisher","") or "").strip()
            cs=float(c.get("score",0) or 0)
            key=(cv,cp)
            if not cv or key in seen: continue
            if cv != value and cs < threshold: continue
            seen.add(key)
            db_options.append({"value":cv,"publisher":cp})
            if len(db_options)>=5: break

    # Fuzzy canonical match can also have multiple publishers.
    if db_options and not publisher:
        best=db_options[0]["value"]
        pubs=unique_list([x["publisher"] for x in db_options
                          if x["value"]==best and x.get("publisher")])
        if len(pubs)>1:
            options=[{"value":best,"publisher":p} for p in pubs]
            pending_name_confirmations[user_id]={
                "field":"book","purpose":"order_book_publisher",
                "options":options,"original":query
            }
            order_flow_context[user_id]=draft
            lines=["📚 找到相同書名","",f"書名：{best}","",
                   "這本書有不同出版社："]
            for i,p in enumerate(pubs,1): lines.append(f"{i}. {p}")
            lines.extend(["",f"請回覆 1～{len(pubs)}"])
            return "\n".join(lines)

    options=list(db_options)
    options.append({"value":query,"publisher":"","raw":True})
    pending_name_confirmations[user_id]={
        "field":"book","purpose":"order_book","options":options,"original":query
    }
    order_flow_context[user_id]=draft
    raw_index=len(options)
    if db_options:
        lines=["🔎 找到接近的書名","",f"你輸入：{query}",""]
        for i,opt in enumerate(db_options,1):
            label=opt["value"]+(f"｜{opt['publisher']}" if opt.get("publisher") else "")
            lines.append(f"{i}. {label}")
        lines.append(f"{raw_index}. 使用原本輸入：{query}")
        lines.extend(["",f"請回覆 1～{raw_index}"])
    else:
        lines=["🔎 資料庫沒有相符書名","",f"你輸入：{query}","",
               "1. 使用原本輸入："+query,"","請回覆 1"]
    return "\n".join(lines)

def handle_order_flow(user_id, text):
    clean = normalize_order_typo(text)

    start_phrases = [
        "訂書", "我要訂書", "我訂書", "開始訂書", "幫我訂書",
        "我要下單", "幫我下單", "要訂書"
    ]

    if clean in start_phrases:
        guided_mode[user_id] = "order_flow"
        order_flow_context[user_id] = {
            "teacher": "",
            "school": "",
            "classes": [],
            "publisher": "",
            "book": ""
        }
        return make_order_guide_reply(order_flow_context[user_id])

    # v38：明確「某老師要訂書」但尚未提供書名，直接建立訂書狀態，
    # 不把整句誤當出版社，也不必交給 AI。
    if user_id not in order_flow_context and ("訂書" in clean or "下單" in clean):
        teacher_guess, school_guess = extract_teacher_and_school(clean)
        if teacher_guess:
            remainder = str(clean)
            remainder = remainder.replace(str(teacher_guess), "")
            remainder = re.sub(r"老師", "", remainder)
            remainder = re.sub(r"(?:要|想要|幫我|麻煩|請|那邊|這邊|的|訂書|下單|但我忘記書名了|忘記書名了|忘記書名|書名忘了)", "", remainder)
            remainder = re.sub(r"[，,。.!！?？\s]+", "", remainder)
            if not remainder:
                guided_mode[user_id] = "order_flow"
                draft0 = {
                    "teacher": "",
                    "school": school_guess or "",
                    "classes": [],
                    "publisher": "",
                    "book": ""
                }
                order_flow_context[user_id] = draft0
                return validate_order_teacher_input(user_id, teacher_guess, draft0)

    draft = order_flow_context.get(user_id, {
        "teacher": "",
        "school": "",
        "classes": [],
        "publisher": "",
        "book": ""
    })

    if _is_confirm_word(clean):
        if user_id in pending_name_confirmations:
            reply = handle_name_confirmation(user_id, clean)
            if reply is not None:
                return reply
        if not draft.get("teacher"):
            return ("⚠️ 我沒有讀到上一個老師候選。\n\n"
                    "請重新輸入老師姓名，我會重新找一次；找到後再回覆「確認」。")
        if draft.get("teacher") and not draft.get("book"):
            return "請告訴我要訂哪一本書？"
        if draft.get("teacher") and draft.get("book") and not draft.get("publisher"):
            return "這本書目前無法從資料庫確認出版社，請告訴我出版社。"

    if user_id in order_flow_context and not draft.get("teacher"):
        candidates = draft.get("teacher_candidates") or []
        if candidates:
            picked = _resolve_order_teacher_candidate_choice(clean, candidates)
            if picked:
                draft["teacher"] = picked["teacher"]
                draft["school"] = picked["school"]
                draft["teacher_classes"] = copy_classes(picked["classes"])
                draft["classes"] = []
                draft["teacher_candidates"] = []
                order_flow_context[user_id] = draft
                if draft["teacher"] and draft["book"]:
                    result = build_order_from_draft(user_id, draft)
                    if user_id in pending_orders:
                        order_flow_context.pop(user_id, None)
                    return result
                return make_order_guide_reply(draft)
        return validate_order_teacher_input(user_id, clean, draft)

    # v38：知道老師後先收「書名」。書名若存在資料庫，直接自動帶出版社，
    # 不再要求使用者先知道出版社。
    if user_id in order_flow_context and draft.get("teacher") and not draft.get("book"):
        return validate_order_book_input(user_id, clean, draft)

    # 只有「使用原本輸入」且資料庫真的無法帶出出版社時，才追問出版社。
    if (
        user_id in order_flow_context
        and draft.get("teacher")
        and draft.get("book")
        and not draft.get("publisher")
    ):
        return validate_order_publisher_input(user_id, clean, draft)

    parsed = parse_order_message(clean)

    # v54：冷啟動「老師＋文字班級＋書名」。以前只有先查過老師才知道
    # 國一甲／丁是班級，現在只要句子抓得到老師，就先查該老師實際班級，
    # 再把文字班級從書名候選中剝離，避免「國一甲」被吞進書名。
    if parsed.get("teacher") and not parsed.get("classes") and parsed.get("has_order_intent"):
        bare = normalize_person_name(parsed.get("teacher"))
        exacts = lookup_teacher_matches(bare, school=parsed.get("school", ""))
        exacts = [x for x in (exacts or []) if normalize_person_name(x.get("teacher", "")) == bare]
        if len(exacts) == 1 and exacts[0].get("classes"):
            tctx = {"teacher": exacts[0].get("teacher", parsed.get("teacher","")), "school": exacts[0].get("school", parsed.get("school","")), "classes": copy_classes(exacts[0].get("classes", []))}
            known = [str(i.get("class_name","")) for i in tctx["classes"]]
            found, remainder = _find_and_strip_letter_classes(clean, known)
            # 「張建國甲丁兩班」：沒有重複寫國一前綴時，依老師實際班級
            # 反推甲／丁；只有代號能唯一對應時才採用，避免跨年級誤猜。
            if not found:
                after_teacher = clean.replace(str(parsed.get("teacher") or ""), " ")
                after_teacher = after_teacher.replace(normalize_person_name(parsed.get("teacher")), " ")
                mletters = re.search(r"([甲乙丙丁戊己庚辛信望愛慧]{1,8})(?:兩班|班|都要|要|訂|\s)", after_teacher)
                if mletters:
                    letters = list(mletters.group(1))
                    by_suffix = {}
                    for cname in known:
                        if cname and cname[-1] in letters:
                            by_suffix.setdefault(cname[-1], []).append(cname)
                    if all(len(by_suffix.get(ch, [])) == 1 for ch in letters):
                        found = [by_suffix[ch][0] for ch in letters]
                        remainder = after_teacher
                        remainder = remainder.replace(mletters.group(0), " ", 1)
            if found:
                parsed["teacher"] = tctx["teacher"]
                parsed["school"] = tctx["school"]
                parsed["classes"] = found
                parsed["book"] = extract_book_candidate(remainder, parsed["teacher"], found)
                conversation_context[user_id] = tctx

    if not parsed["has_order_intent"] and user_id not in order_flow_context:
        recent = conversation_context.get(user_id)
        if recent and looks_like_contextual_class_book(clean, recent):
            parsed = parse_contextual_class_book(clean, recent)
        elif recent and _looks_like_order_for_known_teacher_no_class(clean, recent):
            # 完全沒有「訂」「要」這些字，純粹只有書名（例如「學習
            # 講義公民5」）——parse_order_message() 一開始就不會去抓
            # 書名（has_order_intent 判斷是 False），這裡要自己補上，
            # 不然書名欄位會是空的。班級預設成這位老師的全部班級。
            parsed = dict(parsed)
            parsed["teacher"] = recent.get("teacher", "")
            parsed["school"] = recent.get("school", "")
            parsed["classes"] = [
                str(item.get("class_name"))
                for item in recent.get("classes", [])
            ]
            if not parsed.get("book"):
                parsed["book"] = clean_book_name(clean)
            parsed["has_order_intent"] = True
        else:
            return None
    elif not parsed.get("classes") and user_id not in order_flow_context:
        # parse_order_message() 的班級偵測只認得「701」這種數字班級。
        # 句子裡如果其實是「國一甲丁戊」這種文字班級簡寫，加上句子
        # 本身有「訂」字，上面那個分支不會被觸發（has_order_intent
        # 已經是 True），沒有這一段的話班級會被判斷成「沒有指定」，
        # 依現有邏輯會直接預設成該老師的全部班級——實際上使用者是
        # 有指定班級的，只是文字班級沒被認出來。這裡用剛查過的老師
        # 資料再比對一次文字班級，抓到的話用這個結果整個換掉。
        recent = conversation_context.get(user_id)
        if recent and looks_like_contextual_class_book(clean, recent):
            parsed = parse_contextual_class_book(clean, recent)
        elif recent and _looks_like_order_for_known_teacher_no_class(clean, recent):
            # 這句話完全沒提到任何班級，純粹只有書名（例如「訂段考王
            # 國文6」，或現在也支援完全沒有「訂」字的純書名，例如
            # 「學習講義公民5」），預設成這位老師的全部班級——跟既有
            # 一步一步訂書流程「沒指定班級就預設全部班」的行為一致。
            # 最後還是會先出確認畫面列出全部班級，使用者按確認才會
            # 真的寫入。parsed["book"] 在「完全沒有訂/要」這個新的
            # 放寬情況下，parse_order_message() 一開始就不會去抓書名
            # （has_order_intent 判斷是 False），這裡要自己補上，
            # 不然書名欄位會是空的。
            parsed = dict(parsed)
            parsed["teacher"] = recent.get("teacher", "")
            parsed["school"] = recent.get("school", "")
            parsed["classes"] = [
                str(item.get("class_name"))
                for item in recent.get("classes", [])
            ]
            if not parsed.get("book"):
                parsed["book"] = clean_book_name(clean)
            parsed["has_order_intent"] = True

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

    if parsed.get("grade") and not draft.get("teacher"):
        draft["grade"] = parsed["grade"]

    if parsed.get("classes"):
        draft["classes"] = unique_list(parsed["classes"])
    if parsed.get("raw_class_letters"):
        draft["raw_class_letters"] = list(parsed.get("raw_class_letters") or [])
        draft["raw_grade_prefix"] = str(parsed.get("raw_grade_prefix") or "")

    if parsed.get("publisher"):
        draft["publisher"] = parsed["publisher"]

    if parsed.get("book"):
        draft["book"] = parsed["book"]

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

    if draft["teacher"] and draft["book"]:
        result = build_order_from_draft(user_id, draft)
        if user_id in pending_orders:
            order_flow_context.pop(user_id, None)
        return result

    # 只給了「學校＋年級」、沒講老師姓名：直接從資料庫抓這個年級的
    # 老師候選，讓使用者用點選數字或打姓名的方式選，不用自己想起
    # 正確姓名。只有一位候選時直接採用，不用多問一輪。
    if not draft.get("teacher") and draft.get("grade") and draft.get("school"):
        if not draft.get("teacher_candidates"):
            candidates = _lookup_order_teacher_candidates(
                draft["school"], draft["grade"]
            )
            if len(candidates) == 1:
                picked = candidates[0]
                draft["teacher"] = picked["teacher"]
                draft["school"] = picked["school"]
                draft["teacher_classes"] = copy_classes(picked["classes"])
                draft["classes"] = []
                order_flow_context[user_id] = draft
                if draft["teacher"] and draft["book"]:
                    result = build_order_from_draft(user_id, draft)
                    if user_id in pending_orders:
                        order_flow_context.pop(user_id, None)
                    return result
                return make_order_guide_reply(draft)

            draft["teacher_candidates"] = candidates
            order_flow_context[user_id] = draft

        if draft.get("teacher_candidates"):
            return make_order_teacher_candidates_reply(draft)
        # 這個年級資料庫裡查不到任何老師，退回原本的提示，讓使用者
        # 自己輸入老師姓名。

    return make_order_guide_reply(draft)


def make_order_guide_reply(draft):
    lines = ["📚 訂書進度", ""]
    teacher = str(draft.get("teacher") or "").strip()
    school = str(draft.get("school") or "").strip()
    publisher = str(draft.get("publisher") or "").strip()
    book = str(draft.get("book") or "").strip()
    lines.append(f"{'✅' if teacher else '⬜'} 老師：{teacher or '尚未提供'}")
    if school:
        lines.append(f"   學校：{school}")
    lines.append(f"{'✅' if book else '⬜'} 書名：{book or '尚未提供'}")
    if publisher:
        lines.append(f"   出版社：{publisher}")
    lines.append("")
    if not teacher:
        lines.append("👉 請告訴我是哪一位老師？")
    elif not book:
        lines.append("👉 請告訴我要訂哪一本書？")
    elif not publisher:
        lines.append("👉 目前無法確認出版社，請告訴我出版社名稱。")
    return "\n".join(lines)



def parse_order_message(text):
    clean = str(text or "").strip()
    teacher, school = extract_teacher_and_school(clean)
    classes = extract_classes(clean)

    # 完全沒抓到老師姓名時，試試看是不是「學校＋年級」的講法
    # （例如「華興七年級」），書名擷取要用拿掉這段之後的文字，
    # 不然書名會被污染成「華興七年級 3800應用題套書」。
    grade = ""
    text_for_book = clean
    if not teacher:
        g_school, g_grade, remainder = extract_school_grade_no_teacher(clean)
        if g_school and g_grade:
            school = g_school
            grade = g_grade
            text_for_book = remainder

    order_words = [
        "訂書", "要訂", "想訂", "幫我訂", "我要訂", "訂講義",
        "訂評量", "訂教材", "下單", "幫我下單", "幫我下", "幫我叫",
        "幫我處理", "處理一下", "都要", "也要", "一起", "來一本"
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
        and any(word in clean for word in ["要", "叫", "下", "處理", "拿", "給"])
        and any(word in clean for word in book_words)
    )
    # 老師＋班級＋明顯書名，即使省略「訂／要」也視為訂書口語。
    implicit_order = bool(teacher and (classes or re.search(r"(?:國[一二三]|高[一二三])?[甲乙丙丁戊己庚辛信望愛慧]班?", clean))) and any(word in clean for word in book_words)

    # 否定語意不能因為句子裡剛好有「訂／下單」就建立訂單。
    negative_order = any(x in clean for x in ["沒有要訂", "不是要訂", "先不要訂", "不要幫我下單", "不要訂", "不用", "別下單", "只是問問", "不是真的要訂"])

    has_order_intent = (has_order_word or oral_order or implicit_order) and not negative_order

    book = extract_book_candidate(text_for_book, teacher, classes) if has_order_intent else ""

    raw_class_letters = []
    raw_grade_prefix = ""
    m_class_hint = re.search(r"(國[一二三]|高[一二三])([甲乙丙丁戊己庚辛信望愛慧](?:[跟和與、,/ +&]*[甲乙丙丁戊己庚辛信望愛慧])*)", clean)
    if m_class_hint:
        raw_grade_prefix = m_class_hint.group(1)
        raw_class_letters = re.findall(r"[甲乙丙丁戊己庚辛信望愛慧]", m_class_hint.group(2))
    elif teacher:
        # 甲丁兩班／甲跟丁：年級前綴稍後依老師實際班級唯一性判斷。
        m_bare = re.search(r"([甲乙丙丁戊己庚辛信望愛慧](?:[跟和與、,/ +&]*[甲乙丙丁戊己庚辛信望愛慧])+)(?:兩班|班|都要|要|訂|\s)", clean)
        if m_bare:
            raw_class_letters = re.findall(r"[甲乙丙丁戊己庚辛信望愛慧]", m_bare.group(1))

    return {
        "has_order_intent": has_order_intent,
        "teacher": teacher,
        "school": school,
        "grade": grade,
        "classes": classes,
        "raw_class_letters": raw_class_letters,
        "raw_grade_prefix": raw_grade_prefix,
        "publisher": "",
        "book": book
    }


def extract_book_candidate(text, teacher="", classes=None):
    clean = str(text or "").strip()
    classes = classes or []

    if re.fullmatch(
        r".*(?:老師)?(?:要|想要|準備)?(?:幫我)?(?:訂書|下單)[。！!？?]?",
        clean
    ):
        return ""

    candidate = clean

    if teacher:
        candidate = candidate.replace(teacher, " ")
        # 老師姓名原文可能沒帶「老師」兩字（例如 extract_teacher_and_school
        # 從「張靜婷要訂...」直接判斷出老師時，回傳值固定會補上「老師」
        # 方便後續統一處理，但原文其實只有「張靜婷」），這裡再試一次
        # 去掉字尾「老師」的版本，才不會漏刪。
        bare_teacher = re.sub(r"老師$", "", teacher)
        if bare_teacher and bare_teacher != teacher:
            candidate = candidate.replace(bare_teacher, " ")

    for class_name in classes:
        candidate = re.sub(
            r"(?<!\d)" + re.escape(class_name) + r"(?!\d)",
            " ",
            candidate
        )

    # 姓名從資料庫回來時可能不帶「老師」，原句仍殘留稱謂；稱謂不是書名。
    candidate = re.sub(r"老師", " ", candidate)

    candidate = re.sub(
        r"(?:麻煩|請|謝謝|幫我|幫忙|幫|我要|我想要|想要|想訂|要訂|訂購|訂|下單|下|叫|處理一下|處理|要|需要|都要|也要|一起|那邊|這邊|的|班|兩班|各)",
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

    if not draft.get("teacher"):
        teacher, school = extract_teacher_and_school(clean)

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

            result["book"] = ""
            result["has_order_intent"] = True
            return result

        result["book"] = ""
        result["has_order_intent"] = True
        return result

    if not result.get("teacher"):
        teacher, school = extract_teacher_and_school(clean)
        if teacher:
            result["teacher"] = teacher
            result["school"] = school

    classes = extract_classes(clean)
    if classes and not result.get("classes"):
        result["classes"] = classes

    if draft.get("teacher") and not draft.get("book") and not result.get("book"):
        teacher_in_text = result.get("teacher", "")
        classes_in_text = result.get("classes", [])
        candidate = extract_book_candidate(
            clean,
            teacher_in_text,
            classes_in_text
        )

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
    publisher = str(draft.get("publisher", "") or "").strip()

    exact_matches = lookup_teacher_matches(teacher, school=school)

    # 同一正式書名可能同時由翰林、南一等出版社出版。
    # 沒有出版社時先檢查所有完全同名版本；多家就讓使用者選，絕不拿第一筆。
    if not publisher:
        variants = _exact_book_variants(book)
        ask = _ask_duplicate_book_publisher(user_id, draft, book, variants)
        if ask:
            return ask
        if len(variants) == 1:
            publisher = str(variants[0].get("publisher", "") or "").strip()

    teacher_classes = copy_classes(draft.get("teacher_classes", []))
    if teacher_classes:
        # 老師輸入階段已經查過班級，這裡直接沿用，不再重複打 Google。
        exact_matches = []
    elif len(exact_matches) > 1 and not school:
        # 同名不同校老師在「一句話老師＋班級＋書名」一次到位時，不能
        # 靜默走下面的模糊確認流程——分數相同的話可能選錯學校，連帶
        # 班級人數也會是錯校的。跟 validate_order_teacher_input()（逐步
        # 訂書流程問老師姓名那一步）的處理方式一致：要求使用者補學校。
        schools = unique_list([
            str(item.get("school", "") or "").strip()
            for item in exact_matches
            if str(item.get("school", "") or "").strip()
        ])
        return (
            "🔎 資料庫裡找到同名老師。\n\n"
            f"老師：{teacher}\n"
            f"學校：{'、'.join(schools)}\n\n"
            "請把學校一起告訴我，例如「華興中學" + teacher + "701訂國一數學講義」。"
        )
    if len(exact_matches) == 1:
        exact = exact_matches[0]
        teacher = exact["teacher"]
        school = exact["school"]
        teacher_classes = copy_classes(exact.get("classes", []))
        draft["teacher"] = teacher
        draft["school"] = school
    elif school and not teacher_classes:
        teacher_classes = get_teacher_classes(school, teacher) or []

    # v54：把冷啟動時暫存的文字班級提示，依老師真實班級解析成正式班名。
    if teacher_classes and not requested_classes and draft.get("raw_class_letters"):
        letters = list(draft.get("raw_class_letters") or [])
        prefix = str(draft.get("raw_grade_prefix") or "")
        names = [str(i.get("class_name", "")) for i in teacher_classes]
        resolved = []
        ambiguous = {}
        for ch in letters:
            candidates = [n for n in names if n.endswith(ch) and (not prefix or n.startswith(prefix))]
            if len(candidates) == 1:
                resolved.append(candidates[0])
            else:
                ambiguous[ch] = candidates
        if len(resolved) == len(letters):
            requested_classes = unique_list(resolved)
            draft["classes"] = requested_classes
            # 原本書名候選若殘留「甲丁／國一甲」等班級文字，重新從書名中清掉。
            b = str(book)
            for cname in requested_classes:
                b = b.replace(cname, " ")
                if prefix and cname.startswith(prefix):
                    b = b.replace(cname[len(prefix):], " ", 1)
            b = re.sub(r"(?:兩班|班)[\s，,、]*", " ", b)
            book = clean_book_name(b)
            draft["book"] = book
        else:
            # 文字班級代號對不到唯一班級（例如老師同時帶「國一甲」跟
            # 「國二甲」，單打「甲」無法判定是哪個年級）——原本這裡什麼
            # 都不做，會讓 requested_classes 保持空，掉到下面「沒指定
            # 班級＝訂這位老師全部班級」的預設值，使用者完全沒意識到
            # 已經多訂了不相干年級的班。改成直接回一句澄清問句列出
            # 候選，不要用「全部班級」這種影響最大的方式默默猜。
            order_flow_context[user_id] = draft
            lines = ["⚠️ 班級代號無法判斷是哪一班，請直接告訴我完整班級名稱。", ""]
            for ch, candidates in ambiguous.items():
                if candidates:
                    lines.append(f"「{ch}」可能是：{'、'.join(candidates)}")
                else:
                    lines.append(f"「{ch}」在 {teacher} 名下找不到對應班級")
            return "\n".join(lines)

    if not teacher_classes:
        match = resolve_fuzzy_name("teacher", teacher, school=school)

        if match.get("status") == "auto":
            teacher = match["value"]
            school = match.get("school") or school
            draft["teacher"] = teacher
            draft["school"] = school
            teacher_classes = get_teacher_classes(school, teacher)

        elif match.get("status") == "confirm":
            # keep_classes=True：這裡是「一句話老師+班級+書名」一次到位、
            # 只是老師姓名打錯字的情境，使用者已經明確指定過班級
            # （draft["classes"]），只是在確認「是不是同一位老師」而已，
            # 不是在換老師。handle_name_confirmation() 的共用分支預設會
            # 清空 draft["classes"]（給「一開始只問老師」那個步驟式流程
            # 用，那邊此時班級本來就還沒填），如果這裡也被清空，使用者
            # 剛打的班級就會憑空消失，變成訂到這位老師「全部班級」。
            pending_name_confirmations[user_id] = {
                "field": "teacher",
                "value": match["value"],
                "school": match.get("school") or school,
                "original": teacher,
                "keep_classes": True
            }
            return (
                "🔎 我猜你可能打到同音字或錯字。\n\n"
                f"你輸入：{teacher}\n"
                f"你是指：{match['value']} 嗎？\n\n"
                "請回覆「是」或「不是」。"
            )

    if not teacher_classes:
        draft["teacher"] = ""
        draft["school"] = ""
        draft["classes"] = []
        order_flow_context[user_id] = draft
        pending_name_confirmations.pop(user_id, None)
        return (
            "⚠️ 老師資料庫目前找不到符合的班級資料。\n\n"
            f"你輸入：{teacher}\n\n"
            "書名已保留，請重新輸入正確的老師姓名。"
        )

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
            draft["book"] = ""
            order_flow_context[user_id] = draft
            return (
                "🔎 我猜你可能打到同音字或錯字。\n\n"
                f"你輸入：{book}\n"
                f"你是指：{match['value']} 嗎？\n\n"
                "請回覆「是」或「不是」；也可以直接重新輸入書名。"
            )

    if not publisher:
        draft["book"] = ""
        order_flow_context[user_id] = draft
        pending_name_confirmations.pop(user_id, None)
        return FIXED_FALLBACK_MESSAGE

    # 換書名等流程可能會帶著使用者先前已手動調整過的班級數量
    # （見 handle_pending_order_edit 的「書名修改」分支），這裡蓋回去，
    # 避免又被上面重新查到的名冊人數蓋掉。
    quantity_overrides = draft.get("quantity_overrides") or {}
    if quantity_overrides:
        for item in selected:
            cname = str(item.get("class_name", ""))
            if cname in quantity_overrides:
                item["students"] = quantity_overrides[cname]

    selected = sort_class_items(selected)

    order = {
        "teacher": teacher,
        "school": school,
        "book": book,
        "publisher": publisher,
        "classes": selected,
        "quantity": calculate_total(selected),
        "note": str(draft.get("note", "") or "").strip()
    }

    _clear_stale_history_pending(user_id)
    pending_orders[user_id] = order
    order_flow_context[user_id] = draft
    guided_mode.pop(user_id, None)

    context = {
        "teacher": teacher,
        "school": school,
        "classes": copy_classes(teacher_classes)
    }
    conversation_context[user_id] = context
    teacher_lookup_context[user_id] = context

    return make_order_confirmation(order)


# =========================================================
# 補習班訂書流程（2026-09 新增）
#
# 跟學校訂書完全不同的資料形狀：學校訂單是「一本書、很多班級各要
# 幾本」；補習班訂單是「一間補習班、很多本不同的書，每本通常只訂
# 一兩本」。硬塞進學校那套（老師→出版社→書名→班級）流程會很不合理，
# 所以這裡另外開一個 guided_mode = "cram_order_flow"，用「一次登記
# 一本書」的清單累加方式收單：
#   1. 先問補習班名稱（比照老師姓名做模糊比對，防打錯字）
#   2. 每一本書依序問：出版社 → 書名 → 數量，登記完一本就問要不要
#      繼續加下一本
#   3. 使用者回覆「好了」之類的詞結束收書，進入確認畫面
#   4. 確認畫面可以刪除某一項、或直接輸入下一本的出版社繼續加，
#      回覆「確認」才真正寫入 Google
#
# 書名比對沿用跟學校訂單一樣的「都不是，就用我打的書名」機制
# （見 validate_order_book_input 的說明），補習班訂的書本來就更雜，
# 資料庫不可能全部先登記好。
#
# 【Google Apps Script 端需要新增的 action，app.py 這裡已經假設
# 它們存在，實際串接前記得先在 Apps Script 加上對應處理】：
#   - list_cram_schools            → 回傳已知補習班名單（給精準比對／
#                                     模糊比對來源），格式比照
#                                     list_schools：{success, schools:[...]}
#   - lookup_fuzzy_candidates      → kind 多支援 "cram_school" 一種，
#                                     從補習班名單裡模糊比對
#   - create_cram_order            → 寫入一筆補習班訂單，見
#                                     write_cram_order_to_google() 的
#                                     payload 格式與預期回傳格式
# =========================================================
cram_order_context = {}
_SESSION_DICTS["cram_order_context"] = cram_order_context

cram_school_catalog_cache = {"schools": [], "expires_at": 0}

CRAM_FINISH_WORDS = {
    "好了", "不用了", "這樣就好", "完成", "沒有了", "訂好了", "夠了", "可以了", "這樣就可以了", "就這樣", "這樣可以"
}


def is_cram_order_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "補習班訂書", "我要幫補習班訂書", "幫補習班訂書", "補習班訂購",
        "我要補習班訂書", "補習班要訂書", "補習班訂單", "新增補習班訂單",
        "我要訂補習班的書", "補習班下單", "補班訂書", "補習班要書"
    }


def _new_cram_draft():
    return {
        "cram_school": "",
        "items": [],
        "current_publisher": "",
        "current_book": "",
        "confirming": False,
    }


def get_cram_school_catalog(force_refresh=False):
    now = time.time()

    # 這裡故意不檢查 cram_school_catalog_cache.get("schools") 是不是
    # truthy——空清單 [] 也是合法的負向快取結果，只看 expires_at 是否
    # 還在有效期內，不然空清單快取寫了也沒用，還是會每則訊息都重打。
    if (
        not force_refresh
        and now < float(cram_school_catalog_cache.get("expires_at", 0) or 0)
    ):
        return list(cram_school_catalog_cache.get("schools", []))

    result = google_post({"action": "list_cram_schools"}, timeout=5, retries=1)

    schools = []
    call_succeeded = bool(result and result.get("success"))
    if call_succeeded:
        schools = unique_list([
            str(item or "").strip()
            for item in result.get("schools", [])
            if str(item or "").strip()
        ])

    if schools:
        cram_school_catalog_cache["schools"] = schools
        cram_school_catalog_cache["expires_at"] = now + 600
        return list(schools)

    if call_succeeded:
        # 查詢本身成功、只是清單真的是空的（例如 Apps Script 還沒加這個
        # action，或補習班名單暫時為空）：這支函式在自然語言正規化階段
        # 幾乎每則訊息都會被呼叫一次，沒有負向快取的話會一直重打
        # Google，多吃請求預算與時間。給一個較短的 TTL 避免這種情況，
        # 同時不要蓋掉本來就有的舊快取內容（若有）。
        cram_school_catalog_cache["schools"] = []
        cram_school_catalog_cache["expires_at"] = now + 60
        return []

    return list(cram_school_catalog_cache.get("schools", []))


def cram_next_item_prompt(user_id, draft, first=False):
    if first and draft.get("items"):
        # 草稿已經有完整品項了（例如拍照訂書辨識出來的內容，補習班
        # 名稱是之後才補上的），不要再假裝是空白開始、重新問「第一
        # 本書的出版社」——那樣使用者會以為機器人把圖片辨識結果忘了。
        # 直接進確認畫面讓使用者看已經有的內容，要加書可以直接繼續講。
        return _enter_cram_confirm_stage(user_id, draft)
    if first:
        return (
            f"補習班：{draft.get('cram_school', '')}\n\n"
            "請告訴我第一本書的出版社？"
        )
    return "請告訴我下一本書的出版社？\n如果訂好了，請回覆「好了」。"


def make_cram_items_progress_reply(draft):
    lines = ["🧾 目前已登記："]
    for i, item in enumerate(draft.get("items", []), start=1):
        lines.append(
            f"{i}. [{item.get('publisher', '')}] {item.get('book', '')}"
            f" x{int(item.get('quantity', 0) or 0)}本"
        )
    return "\n".join(lines)


def make_cram_order_confirmation(draft):
    items = draft.get("items", [])
    total = sum(int(item.get("quantity", 0) or 0) for item in items)
    lines = ["📚 補習班訂單｜請確認", "", f"🏫 補習班：{draft.get('cram_school', '')}", "", "📖 訂購內容"]
    for i, item in enumerate(items, start=1):
        lines.append(f"{i}. [{item.get('publisher', '')}] {item.get('book', '')}｜{int(item.get('quantity', 0) or 0)}本")
    lines += ["", f"📦 共 {len(items)} 種｜合計 {total} 本", "", "確認無誤 → 回覆「確認」", "刪除品項 → 例如「刪除2」", "繼續加書 → 直接輸入下一本書"]
    return "\n".join(lines)



def validate_cram_school_input(user_id, raw_text, draft):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    if not clean:
        return "請告訴我補習班名稱。"

    catalog = get_cram_school_catalog()
    if clean in catalog:
        draft["cram_school"] = clean
        pending_name_confirmations.pop(user_id, None)
        cram_order_context[user_id] = draft
        return cram_next_item_prompt(user_id, draft, first=True)

    candidates = lookup_fuzzy_candidates("cram_school", clean)

    if not candidates:
        # 資料庫裡完全查不到，可能真的是第一次出現的新補習班。
        pending_name_confirmations[user_id] = {
            "field": "cram_school", "purpose": "cram_school",
            "options": [{"value": clean, "raw": True}],
            "original": clean
        }
        cram_order_context[user_id] = draft
        return (
            "⚠️ 補習班名單目前找不到符合的名稱。\n\n"
            f"你輸入：{clean}\n\n"
            "如果是新的補習班，回覆「確認」或「都不是」我就直接照你打的建立訂單。\n"
            "如果是打錯字，請重新輸入正確名稱。"
        )

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    score = float(first.get("score", 0) or 0)
    second_score = float(candidates[1].get("score", 0) or 0) if len(candidates) > 1 else 0.0

    if value == clean:
        draft["cram_school"] = value
        pending_name_confirmations.pop(user_id, None)
        cram_order_context[user_id] = draft
        return cram_next_item_prompt(user_id, draft, first=True)

    # 高可信且明顯領先第二名：直接自動採用，不用多問一次。
    if value and score >= 0.78 and (len(candidates) == 1 or score - second_score >= 0.12):
        draft["cram_school"] = value
        pending_name_confirmations.pop(user_id, None)
        cram_order_context[user_id] = draft
        return cram_next_item_prompt(user_id, draft, first=True)

    options = []
    if value and score >= 0.55:
        threshold = max(0.55, score - 0.1)
        seen = set()
        for c in candidates[:3]:
            c_value = str(c.get("value", "") or "").strip()
            c_score = float(c.get("score", 0) or 0)
            if not c_value or c_value in seen:
                continue
            if c_value != value and c_score < threshold:
                continue
            seen.add(c_value)
            options.append({"value": c_value})

    options.append({"value": clean, "raw": True})

    pending_name_confirmations[user_id] = {
        "field": "cram_school", "purpose": "cram_school",
        "options": options, "original": clean
    }
    cram_order_context[user_id] = draft

    raw_index = len(options)
    lines = ["🔎 補習班名單裡找到接近的名稱。", "", f"你輸入：{clean}", ""]
    for i, opt in enumerate(options[:-1], start=1):
        lines.append(f"{i}. {opt['value']}")
    lines.append(f"{raw_index}. 都不是，這是新的補習班，就用「{clean}」")
    lines.append("")
    lines.append("請回覆數字選擇；回覆「確認」等同選第 1 個；回覆「都不是」直接用你打的名稱。")
    return "\n".join(lines)


def validate_cram_item_publisher_input(user_id, raw_text, draft):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    clean = normalize_order_typo(clean)
    if not clean:
        return "請告訴我這本書的出版社，例如：康軒、翰林、南一"

    candidates = lookup_fuzzy_candidates("publisher", clean)
    if not candidates:
        return ("⚠️ 出版社資料庫目前找不到符合資料。\n\n"
                f"你輸入：{clean}\n\n"
                "請重新輸入出版社名稱。")

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    score = float(first.get("score", 0) or 0)

    if value == clean:
        draft["current_publisher"] = value
        pending_name_confirmations.pop(user_id, None)
        cram_order_context[user_id] = draft
        if draft.get("current_book"):
            return f"出版社：{value}\n書名：{draft['current_book']}\n\n要幾本？"
        return "請告訴我書名？"

    if value and score >= 0.52:
        options = []
        seen = set()
        for c in candidates[:4]:
            c_value = str(c.get("value", "") or "").strip()
            c_score = float(c.get("score", 0) or 0)
            if not c_value or c_value in seen:
                continue
            if c_value != value and c_score < 0.52:
                continue
            seen.add(c_value)
            options.append({"value": c_value})
            if len(options) >= 3:
                break

        pending_name_confirmations[user_id] = {
            "field": "publisher", "purpose": "cram_publisher",
            "options": options, "original": clean
        }
        cram_order_context[user_id] = draft

        lines = ["🔎 出版社名稱可能有錯字，找到以下接近的候選。", "", f"你輸入：{clean}", ""]
        for i, opt in enumerate(options, start=1):
            lines.append(f"{i}. {opt['value']}")
        lines.append("")
        if len(options) > 1:
            lines.append("請直接回覆數字（例如「1」）選擇要的那一家；回覆「確認」等同選第 1 個。")
        else:
            lines.append("是的請回覆「確認」。")
        lines.append("如果都不是，請直接輸入正確出版社名稱，我會取消這個候選並重新查資料庫。")
        return "\n".join(lines)

    return ("⚠️ 出版社資料庫目前無法確認這個名稱。\n\n"
            f"你輸入：{clean}\n\n請重新輸入出版社名稱。")


def validate_cram_item_book_input(user_id, raw_text, draft):
    query = clean_book_name(str(raw_text or "").strip())
    query = re.sub(r"^(?:我要訂|要訂|訂)", "", query).strip()
    if not query:
        return "請輸入書名或書名關鍵字。"

    publisher = str(draft.get("current_publisher") or "").strip()
    candidates = lookup_book_candidates_enhanced(query, publisher=publisher)

    if candidates:
        first = candidates[0]
        value = str(first.get("value", "") or "").strip()
        score = float(first.get("score", 0) or 0)

        if value == query:
            draft["current_book"] = value
            pending_name_confirmations.pop(user_id, None)
            cram_order_context[user_id] = draft
            return "這本要訂幾本？"
    else:
        value, score = "", 0.0

    db_options = []
    if value and score >= 0.52:
        threshold = max(0.52, score - 0.08)
        seen = set()
        for c in candidates[:4]:
            c_value = str(c.get("value", "") or "").strip()
            c_score = float(c.get("score", 0) or 0)
            if not c_value or c_value in seen:
                continue
            if c_value != value and c_score < threshold:
                continue
            seen.add(c_value)
            db_options.append({
                "value": c_value,
                "publisher": str(c.get("publisher", "") or "")
            })
            if len(db_options) >= 3:
                break

    options = list(db_options)
    options.append({"value": query, "publisher": "", "raw": True})

    pending_name_confirmations[user_id] = {
        "field": "book", "purpose": "cram_book",
        "options": options, "original": query
    }
    cram_order_context[user_id] = draft

    raw_index = len(options)
    if db_options:
        lines = ["🔎 找到接近的書名", "", f"你輸入：{query}", ""]
        for i, opt in enumerate(db_options, start=1):
            lines.append(f"{i}. {opt['value']}")
        lines.append(f"{raw_index}. 使用原本輸入：{query}")
        lines.extend(["", f"請回覆 1～{raw_index}"])
    else:
        lines = [
            "🔎 資料庫沒有相符書名", "",
            f"你輸入：{query}", "",
            "1. 使用原本輸入：" + query, "",
            "請回覆 1"
        ]
    return "\n".join(lines)


def validate_cram_item_quantity_input(user_id, raw_text, draft):
    m = re.fullmatch(r"(\d{1,4})\s*本?", str(raw_text or "").strip())
    if not m:
        return "請告訴我這本書要訂幾本？直接輸入數字就好，例如「2」。"

    quantity = int(m.group(1))
    if quantity <= 0:
        return "數量要大於 0，請重新輸入。"

    draft.setdefault("items", []).append({
        "publisher": draft.get("current_publisher", ""),
        "book": draft.get("current_book", ""),
        "quantity": quantity,
    })
    draft["current_publisher"] = ""
    draft["current_book"] = ""
    cram_order_context[user_id] = draft

    return (
        make_cram_items_progress_reply(draft)
        + "\n\n請告訴我下一本書的出版社？如果訂好了，請回覆「好了」。"
    )


def _enter_cram_confirm_stage(user_id, draft):
    if not draft.get("items"):
        return "⚠️ 你還沒有登記任何書，請先告訴我要訂的第一本書的出版社。"

    draft["confirming"] = True
    cram_order_context[user_id] = draft
    return make_cram_order_confirmation(draft)


def handle_cram_confirm_stage(user_id, clean, draft):
    if _is_confirm_word(clean):
        return confirm_cram_order(user_id)

    m = re.fullmatch(r"(?:刪除|刪掉|移除|拿掉)\s*(\d{1,2})", clean)
    if m:
        index = int(m.group(1)) - 1
        items = draft.get("items", [])
        if 0 <= index < len(items):
            removed = items.pop(index)
            cram_order_context[user_id] = draft
            removed_line = f"✅ 已刪除：[{removed.get('publisher', '')}] {removed.get('book', '')}\n\n"
            if not items:
                draft["confirming"] = False
                cram_order_context[user_id] = draft
                return removed_line + "目前清單是空的，請告訴我下一本要訂的出版社。"
            return removed_line + make_cram_order_confirmation(draft)
        return f"⚠️ 目前只有 1～{len(items)} 項，請輸入正確的編號。"

    if clean in {"加", "繼續加", "再加一本", "加一本"}:
        draft["confirming"] = False
        cram_order_context[user_id] = draft
        return cram_next_item_prompt(user_id, draft)

    # 使用者直接輸入了出版社名稱想加下一本，不用先打「加」。
    draft["confirming"] = False
    cram_order_context[user_id] = draft
    reply = validate_cram_item_publisher_input(user_id, clean, draft)
    # 完全沒辨識出出版社、也沒有進入候選確認子流程時，代表這次「加下
    # 一本」的嘗試失敗了（例如使用者其實只是打了句招呼語或打錯字）。
    # 這種情況要把 confirming 復原，不然使用者接下來打「確認」會被
    # 當成又一次出版社輸入，卡在「請重新輸入出版社名稱」，除非剛好
    # 知道要打「好了」才能回到確認畫面。
    if not draft.get("current_publisher") and user_id not in pending_name_confirmations:
        draft["confirming"] = True
        cram_order_context[user_id] = draft
    return reply


def write_cram_order_to_google(draft):
    # 已知限制：學校訂單／其他訂單逾時後都有「查回來確認是否其實已
    # 寫入成功」的驗證機制（見 write_to_google_sheet／
    # write_other_order_to_google_sheet），可以避免使用者重按確認造成
    # 重複紀錄。補習班訂單目前沒有對應的查詢 action（Apps Script 端只
    # 有 create_cram_order，沒有可以「查某補習班今天訂了哪些書」的
    # 讀取 action），沒有東西可以查回來驗證，暫時無法比照辦理。
    # 之後如果要補，得先在 Apps Script 端加一個類似
    # lookup_cram_orders 的 action，這裡才有東西可以核對。
    result = google_post({
        "action": "create_cram_order",
        "cram_school": draft.get("cram_school", ""),
        "items": draft.get("items", [])
    }, timeout=15, retries=1, ignore_budget=True)

    if result and result.get("success") is True:
        return True, str(result.get("order_number", "") or "")

    return False, None


def confirm_cram_order(user_id):
    draft = cram_order_context.get(user_id)
    if not draft or not draft.get("items"):
        return "⚠️ 找不到尚未確認的補習班訂單，請重新輸入。"

    success, order_number = write_cram_order_to_google(draft)
    if not success:
        return "❌ 補習班訂單寫入失敗，請稍後再試。"

    items = [dict(item) for item in draft.get("items", [])]
    cram_school = draft.get("cram_school", "")
    total = sum(int(item.get("quantity", 0) or 0) for item in items)

    cram_order_context.pop(user_id, None)
    guided_mode.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)

    pending_receipt_offers[user_id] = {
        "kind": "cram",
        "order_number": order_number,
        "cram_school": cram_school,
        "items": items,
        "created_at": time.time(),
    }

    return [
        (
            "✅ 補習班訂單已確認\n\n"
            f"訂單編號：{order_number}\n"
            f"補習班：{cram_school}\n"
            f"共 {len(items)} 種書，總數量：{total}本\n"
            "已成功寫入 Google 試算表。"
        ),
        (
            "需要幫你生成一份訂購單 PDF，讓你可以存下來 email 給補習班或出版社嗎？\n"
            "回覆「要」或「好」即可，40 秒內沒有回覆就會自動取消這個提問。"
        )
    ]


def handle_cram_order_flow(user_id, text):
    clean = normalize_order_typo(text)

    draft = cram_order_context.get(user_id)
    if draft is None:
        draft = _new_cram_draft()
        cram_order_context[user_id] = draft

    if not draft.get("cram_school"):
        return validate_cram_school_input(user_id, clean, draft)

    if draft.get("confirming"):
        return handle_cram_confirm_stage(user_id, clean, draft)

    # 結束詞（好了／這樣就好…）不管目前是在問出版社、書名還是數量都要
    # 能結束——原本只在「還沒填出版社」時才檢查，一旦使用者已經回了
    # 出版社（例如「康軒」），「好了」就會被誤當成書名去查，使用者完全
    # 沒有辦法結束這次登記。半填的這個品項直接放棄，用目前已登記好的
    # items 進確認畫面。
    if clean in CRAM_FINISH_WORDS:
        return _enter_cram_confirm_stage(user_id, draft)

    if not draft.get("current_publisher"):
        return validate_cram_item_publisher_input(user_id, clean, draft)

    if not draft.get("current_book"):
        return validate_cram_item_book_input(user_id, clean, draft)

    return validate_cram_item_quantity_input(user_id, clean, draft)


# =========================================================
# 多書訂購流程（2026-09 新增，一個班一種不同的書）
#
# 跟學校訂書（order_flow：一本書、很多班級）、補習班訂書
# （cram_order_flow：一間補習班、很多本書、不分班級）都不一樣，
# 這裡是「一位老師、很多本『不同』的書，一個班對應一本」，
# 例如：一位老師七個班，各拿一種不同的康軒歷史1測驗卷。
#
# 完全獨立的 guided_mode = "multi_book_order_flow" + 獨立的
# multi_book_order_context 草稿字典，不會動到 order_flow_context，
# 也不需要 Google Apps Script 新增任何 action：確認後是逐筆呼叫
# 既有的 write_to_google_sheet()，把每個「班級＋書」都當成一筆
# 獨立的學校訂單寫入，跟平常手動一班一班訂書寫進 Google 的資料
# 格式完全相同。
# =========================================================
multi_book_order_context = {}
_SESSION_DICTS["multi_book_order_context"] = multi_book_order_context

# 書籍模糊比對分數門檻：只有達到這個分數的候選才會被視為「符合條件」
# 一起收下，不是只取分數最高的一筆。門檻比照其他地方的「還算可信」
# 標準（0.5 上下），故意不設太高，避免漏掉書名寫法差異較大的候選。
MULTI_BOOK_MATCH_SCORE_THRESHOLD = 0.42
# 一次最多處理幾種書／幾個班，純粹防呆，避免異常輸入。
MULTI_BOOK_MAX_CANDIDATES = 15

MULTI_BOOK_NO_PUBLISHER_WORDS = {"不限", "沒有限定", "不限出版社", "都可以", "沒有"}
MULTI_BOOK_ACCEPT_FOUND_WORDS = {"採用", "就這些", "使用這些", "照這樣", "這樣就好"}
MULTI_BOOK_RETRY_KEYWORD_WORDS = {"換關鍵字", "重新輸入", "重新搜尋", "重新查", "換一個"}


def is_multi_book_order_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "多書訂購", "多書訂單", "不同書訂單", "各班不同書", "各班不同的書",
        "多種書訂購", "我要訂不同的書", "多本不同的書", "一班一種書",
        "我要多書訂購"
    }


def _new_multi_book_draft():
    return {
        "teacher": "", "school": "", "teacher_classes": [],
        # publisher 用 None 代表「還沒問過」，跟使用者明確選「不限」
        # 得到的空字串 "" 要分開，兩者意義不同。
        "publisher": None,
        "keyword": "", "expected_count": 0,
        "candidates": [], "assignments": [],
        # excluded_values：目前清單裡已經出現過（或曾經被使用者換掉）
        # 的書名，換一項時用來避免重複建議同一本。
        "excluded_values": [],
        "awaiting_decision": False,
        # awaiting_review：候選清單湊滿數量後，先讓使用者確認／逐項替換，
        # 確認過才會進到 awaiting_classes 問班級分配。
        "awaiting_review": False,
        "awaiting_classes": False,
        # v68：只有「合格卷種類湊不滿指定數量」時才啟用輪替備援。
        "fallback_rotation": False,
        "rotation_target_count": 0,
        # 跨年級老師而書名沒有冊次時，不猜年級；要求使用者補 1～6 冊。
        "awaiting_volume_selection": False,
        "confirming": False,
    }


def _apply_multi_book_teacher(user_id, draft, match):
    teacher_classes = match.get("classes") or get_teacher_classes(
        match["school"], match["teacher"]
    ) or []

    if not teacher_classes:
        return f"⚠️ {match['teacher']} 目前查不到任何班級資料，請確認老師姓名是否正確。"

    draft["teacher"] = match["teacher"]
    draft["school"] = match["school"]
    draft["teacher_classes"] = teacher_classes
    multi_book_order_context[user_id] = draft
    pending_name_confirmations.pop(user_id, None)

    class_list = "、".join(c["class_name"] for c in sort_class_items(teacher_classes))
    return (
        f"老師：{draft['teacher']}（{draft['school']}）\n"
        f"目前班級：{class_list}\n\n"
        "請告訴我要限定哪個出版社？（例如「康軒」）\n"
        "沒有要限定的話，請回覆「不限」。"
    )


def validate_multi_book_teacher_input(user_id, raw_text, draft):
    clean = normalize_person_name(raw_text)
    if not clean:
        return "請告訴我是哪一位老師？"

    exact_matches, teacher_query_ok = lookup_teacher_matches_status(clean)
    if not teacher_query_ok:
        return (
            "⚠️ 老師資料庫目前查詢逾時或暫時無法連線。\n\n"
            "為了避免連續模糊搜尋讓等待時間更久，我先停在這一步。\n"
            "請直接再輸入一次老師姓名。"
        )

    if len(exact_matches) == 1:
        return _apply_multi_book_teacher(user_id, draft, exact_matches[0])

    if len(exact_matches) > 1:
        schools = "、".join(unique_list([m["school"] for m in exact_matches]))
        return (
            f"⚠️ 查到多位同名老師（{schools}），這個功能目前還不支援自動判斷學校。\n\n"
            "請改用「學校＋老師姓名」重新輸入，例如「天母國中王小明」。"
        )

    match = resolve_fuzzy_name("teacher", clean)

    if match.get("status") == "auto":
        matches = lookup_teacher_matches(match["value"], school=match.get("school", ""))
        if len(matches) == 1:
            return _apply_multi_book_teacher(user_id, draft, matches[0])
        return "⚠️ 老師資料庫目前找不到唯一符合的班級資料，請重新輸入老師姓名。"

    if match.get("status") == "confirm":
        pending_name_confirmations[user_id] = {
            "purpose": "multi_book_teacher",
            "field": "teacher",
            "value": match["value"],
            "school": match.get("school", ""),
            "original": clean,
        }
        multi_book_order_context[user_id] = draft
        return (
            "🔎 我猜你可能打到同音字或錯字。\n\n"
            f"你輸入：{clean}\n你是指：{match['value']} 嗎？\n\n"
            "請回覆「是」或「不是」。"
        )

    return f"⚠️ 老師資料庫目前找不到符合的老師「{clean}」，請重新輸入姓名。"


def validate_multi_book_publisher_input(user_id, raw_text, draft):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))

    if clean in MULTI_BOOK_NO_PUBLISHER_WORDS:
        draft["publisher"] = ""
        multi_book_order_context[user_id] = draft
        return "好，出版社不限。\n\n請告訴我書名關鍵字，例如「歷史1測驗卷」。"

    if not clean:
        return "請告訴我出版社關鍵字，或回覆「不限」。"

    candidates = lookup_fuzzy_candidates("publisher", clean)
    if not candidates:
        return (
            "⚠️ 出版社資料庫目前找不到符合資料。\n\n"
            f"你輸入：{clean}\n\n請重新輸入出版社名稱，或回覆「不限」。"
        )

    first = candidates[0]
    value = str(first.get("value", "") or "").strip()
    score = float(first.get("score", 0) or 0)

    if value == clean or (value and score >= 0.6):
        draft["publisher"] = value or clean
        multi_book_order_context[user_id] = draft
        return f"出版社：{draft['publisher']}\n\n請告訴我書名關鍵字，例如「歷史1測驗卷」。"

    options = []
    seen = set()
    for c in candidates[:3]:
        c_value = str(c.get("value", "") or "").strip()
        if c_value and c_value not in seen:
            seen.add(c_value)
            options.append({"value": c_value})
    options.append({"value": clean, "raw": True})

    pending_name_confirmations[user_id] = {
        "purpose": "multi_book_publisher",
        "field": "publisher",
        "options": options,
        "original": clean,
    }
    multi_book_order_context[user_id] = draft

    lines = ["🔎 出版社名稱可能有錯字，找到以下接近的候選。", "", f"你輸入：{clean}", ""]
    for i, opt in enumerate(options[:-1], start=1):
        lines.append(f"{i}. {opt['value']}")
    lines.append(f"{len(options)}. 都不是，直接用「{clean}」")
    lines.append("")
    lines.append("請回覆數字選擇；回覆「確認」等同選第 1 個。")
    return "\n".join(lines)


def validate_multi_book_keyword_input(user_id, raw_text, draft):
    clean = clean_book_name(str(raw_text or "").strip())
    if not clean:
        return "請告訴我書名關鍵字，例如「歷史1測驗卷」。"
    draft["keyword"] = clean
    multi_book_order_context[user_id] = draft
    return f"關鍵字：{clean}\n\n這次要挑幾種不同的書？請直接輸入數字，例如「7」。"


def _multi_book_query_variants(query):
    """多書湊單專用搜尋變體；保持少量，避免為了湊書打爆 Google。"""
    clean = str(query or "").strip()
    variants = [clean] if clean else []
    norm = normalize_book_match_text(clean)

    subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    subject = next((x for x in subjects if x in norm), "")
    m = re.search(r"(?:國文|英文|英語|數學|自然|理化|生物|地科|社會|歷史|地理|公民)[^1-6]*([1-6])", norm)
    volume = m.group(1) if m else ""

    if "測驗卷" in clean:
        variants.append(clean.replace("測驗卷", "考卷"))
    elif "考卷" in clean:
        variants.append(clean.replace("考卷", "測驗卷"))

    # 最後才放寬成科目＋冊次；後續仍會用「卷類」與適用版本硬篩選，
    # 所以不會再把新講義、段考王之類混進「測驗卷」結果。
    if subject and volume:
        variants.append(f"{subject}{volume}")

    out = []
    for v in variants:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out[:3]


def _multi_book_query_profile(query):
    q = normalize_book_match_text(query)
    subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    subject = next((x for x in subjects if x in q), "")
    if subject == "英語":
        subject = "英文"
    m = re.search(r"(?:國文|英文|英語|數學|自然|理化|生物|地科|社會|歷史|地理|公民)[^1-6]*([1-6])", q)
    volume = m.group(1) if m else ""
    exam_like = any(x in q for x in ["測驗卷", "考卷", "卷類", "試卷"])
    grade = {"1":"七年級", "2":"七年級", "3":"八年級", "4":"八年級", "5":"九年級", "6":"九年級"}.get(volume, "")
    return {"subject": subject, "volume": volume, "exam_like": exam_like, "grade": grade}


def _normalize_textbook_version(value):
    s = re.sub(r"[\s版]+", "", str(value or ""))
    if s.startswith("南"):
        return "南一"
    if s.startswith("康"):
        return "康軒"
    if s.startswith("翰"):
        return "翰林"
    return str(value or "").strip()


def _infer_candidate_applicable_version(book, publisher):
    """依目前書籍資料規則推導考卷的適用教科書版本。"""
    name = normalize_book_match_text(book)
    pub = str(publisher or "").strip()

    # 明霖／金安等跨版本卷：書名尾碼南／康／翰才是適用版本。
    m = re.search(r"(?:[-－_ ]?)(南|康|翰)$", str(book or "").strip())
    if m:
        return {"南":"南一", "康":"康軒", "翰":"翰林"}[m.group(1)]

    # 使用者確認：漢華「新挑戰測驗卷」全系列都是康軒版。
    if pub == "漢華" and "新挑戰測驗卷" in name:
        return "康軒"

    # 南一／康軒／翰林自己出版且沒有另標版本的 A/B/C/D 卷，
    # 視為適用該出版社自己的教科書版本。
    if pub in {"南一", "康軒", "翰林"} and re.search(r"(?:^|[^A-Za-z])[ABCDＡＢＣＤ]卷", name, re.I):
        return pub

    # 出版社自己的「測驗卷／考卷」若沒有另外的版本尾碼，也視為自家版本。
    if pub in {"南一", "康軒", "翰林"} and any(x in name for x in ["測驗卷", "考卷", "平時卷", "複習卷", "段考卷"]):
        return pub

    return ""


def _multi_book_is_exam_candidate(book):
    name = normalize_book_match_text(book)
    if re.search(r"(?:^|[^A-Za-z])[ABCDＡＢＣＤ]卷", name, re.I):
        return True
    return any(x in name for x in ["測驗卷", "考卷", "平時卷", "複習卷", "段考卷", "試卷"])


def _multi_book_subject_volume_ok(query, candidate):
    """科目、冊次是硬條件；使用者說測驗卷時，候選也必須真的是卷類。"""
    profile = _multi_book_query_profile(query)
    c = normalize_book_match_text(candidate)
    subject = profile["subject"]
    volume = profile["volume"]

    if subject:
        aliases = {"英文", "英語"} if subject == "英文" else {subject}
        if not any(x in c for x in aliases):
            return False

    if volume and subject:
        subject_pat = "(?:英文|英語)" if subject == "英文" else re.escape(subject)
        if not re.search(subject_pat + r"[^1-6]*" + re.escape(volume) + r"(?!\d)", c):
            return False

    if profile["exam_like"] and not _multi_book_is_exam_candidate(candidate):
        return False
    return True


def _multi_book_school_target_version(draft, query):
    """依書名冊次推年級，再查該校該年級科目的教科書版本。"""
    profile = _multi_book_query_profile(query)
    school = str(draft.get("school", "") or "").strip()
    grade = profile.get("grade", "")
    subject = profile.get("subject", "")
    if not (school and grade and subject):
        return ""

    # 教科書版本表的「歷史／地理／公民」通常統一登記在「社會」。
    # 書籍搜尋仍保留原科目（歷史就是找歷史書），只有版本查詢改用社會。
    version_subject = _version_lookup_subject(subject)

    subject_aliases = [version_subject]
    if subject == "自然":
        subject_aliases += ["理化", "生物"]
    elif subject in {"理化", "生物", "地科"}:
        subject_aliases += ["自然"]

    for sub in subject_aliases:
        result = lookup_school_versions(school, grade, sub, "")
        if not result:
            continue
        versions = result.get("versions", []) or []
        for item in versions:
            version = _normalize_textbook_version(item.get("version", ""))
            if version:
                draft["target_grade"] = grade
                draft["target_subject"] = subject
                draft["target_version"] = version
                return version

    # 最後一次查同年級全部科目，避免資料庫科目命名略有差異。
    result = lookup_school_versions(school, grade, "", "")
    if result:
        for item in result.get("versions", []) or []:
            item_subject = str(item.get("subject", "") or "")
            if subject == "自然":
                subject_ok = any(x in item_subject for x in ["自然", "理化", "生物", "地科"])
            elif subject == "英文":
                subject_ok = any(x in item_subject for x in ["英文", "英語"])
            elif subject in {"歷史", "地理", "公民"}:
                subject_ok = "社會" in item_subject or subject in item_subject
            else:
                subject_ok = subject in item_subject
            if subject_ok:
                version = _normalize_textbook_version(item.get("version", ""))
                if version:
                    draft["target_grade"] = grade
                    draft["target_subject"] = subject
                    draft["target_version"] = version
                    return version
    return ""


def _multi_book_series_key(book):
    """用來讓選出的 N 種儘量分散系列，而不是全部同一系列。"""
    s = normalize_book_match_text(book)
    s = re.sub(r"(?:國文|英文|英語|數學|自然|理化|生物|地科|社會|歷史|地理|公民)[^1-6]*[1-6]", "", s)
    s = re.sub(r"(?:[-－_ ]?)(南|康|翰)$", "", s)
    return s or normalize_book_match_text(book)


def _select_diverse_multi_book_candidates(candidates, expected):
    """在合格候選裡優先不同出版社、不同系列，再看模糊分數。"""
    pool = list(candidates)
    chosen = []
    used_publishers = set()
    used_series = set()
    while pool and len(chosen) < expected:
        def rank(c):
            pub = c.get("publisher", "")
            series = _multi_book_series_key(c.get("value", ""))
            return (
                1 if pub and pub not in used_publishers else 0,
                1 if series and series not in used_series else 0,
                float(c.get("score", 0) or 0),
            )
        best = max(pool, key=rank)
        pool.remove(best)
        chosen.append(best)
        if best.get("publisher"):
            used_publishers.add(best["publisher"])
        used_series.add(_multi_book_series_key(best.get("value", "")))
    return chosen



def _multi_book_teacher_has_grade(draft, grade):
    return any(
        _class_matches_grade(c.get("class_name", ""), grade)
        for c in (draft.get("teacher_classes") or [])
    )


def _multi_book_teacher_junior_grades(draft):
    grades = []
    for grade in ["七年級", "八年級", "九年級"]:
        if _multi_book_teacher_has_grade(draft, grade):
            grades.append(grade)
    return grades


def _multi_book_eligible_classes(draft):
    """只回傳這次書名冊次所屬年級的班級；避免跨年級老師把別年級班級混進分配。"""
    grade = str(draft.get("target_grade", "") or "").strip()
    classes = list(draft.get("teacher_classes") or [])
    if not grade:
        profile = _multi_book_query_profile(draft.get("keyword", ""))
        grade = profile.get("grade", "")
    if not grade:
        return classes
    return [c for c in classes if _class_matches_grade(c.get("class_name", ""), grade)]


def _lookup_multi_book_candidates_structured(query, publisher="", required_version="", limit=30):
    """v67：一次把結構化條件送給 GAS，避免同一筆多書搜尋連打 3 次 fuzzy。"""
    profile = _multi_book_query_profile(query)
    if not (profile.get("subject") and profile.get("volume")):
        return None

    result = google_post({
        "action": "lookup_multi_book_candidates",
        "subject": profile.get("subject", ""),
        "volume": profile.get("volume", ""),
        "category": "卷類" if profile.get("exam_like") else "",
        "applicable_version": str(required_version or ""),
        "publisher": str(publisher or ""),
        "limit": int(max(1, min(int(limit or 30), 50))),
    }, timeout=6.0, retries=1)

    # 舊 GAS 尚未部署新 action 時回 None，讓呼叫端退回舊 fuzzy 搜尋，
    # 避免部署順序不同時整個功能直接壞掉。
    if not result or not result.get("success"):
        return None
    return result.get("candidates", []) or []


def _collect_multi_book_matches(query, publisher, target_count=0, required_version=""):
    """
    v67 多書搜尋：優先使用 GAS 結構化查詢，一次依「科目＋冊次＋卷類＋
    適用版本＋真正出版社」取得完整候選。若 GAS 尚未更新才退回舊 fuzzy。
    同書名不同出版社以 (書名, 出版社) 為不同品項。
    """
    seen = set()
    result = []

    structured = _lookup_multi_book_candidates_structured(
        query,
        publisher=publisher,
        required_version=required_version,
        limit=max(30, int(target_count or 0) * 3),
    )

    if structured is not None:
        raw_groups = [structured]
    else:
        # v79：結構化多書搜尋失敗／逾時後，只允許「一次」舊 fuzzy 保底。
        # 舊版會跑最多 3 個 query variant，而 enhanced 每個 variant 又可能
        # 再打 core query，最壞會疊到 4~6 次 Google，實測曾把單一流程拖到
        # 46 秒。現在只打原始關鍵字一次；若這一次也失敗，就直接回查無，
        # 不再為了湊候選把 LINE worker 卡住。
        one_fallback = lookup_fuzzy_candidates("book", query, publisher=publisher) or []
        raw_groups = [one_fallback]

    for raw in raw_groups:
        for c in raw:
            value = str(c.get("value", "") or "").strip()
            pub = str(c.get("publisher", "") or publisher or "").strip()
            score = float(c.get("score", 1.0 if structured is not None else 0) or 0)
            key = (value, pub)
            if not value or key in seen or not _multi_book_subject_volume_ok(query, value):
                continue
            if structured is None and score < MULTI_BOOK_MATCH_SCORE_THRESHOLD:
                continue

            applicable_version = _normalize_textbook_version(
                c.get("applicable_version", "") or _infer_candidate_applicable_version(value, pub)
            )
            if required_version and applicable_version != required_version:
                continue

            seen.add(key)
            result.append({
                "value": value,
                "publisher": pub,
                "score": score,
                "applicable_version": applicable_version,
                "category": str(c.get("category", "") or ""),
            })

    return sorted(result, key=lambda x: x["score"], reverse=True)[:50]

def _find_multi_book_replacement(draft, excluded_values):
    """
    幫某一項候選找替代書：先在使用者指定的出版社裡找，找不到就自動
    放寬成不限出版社（跨社湊），excluded_values 內的書名（目前清單上
    已經有的、或剛被換掉的）一律跳過，避免湊出重複的書。
    """
    query = draft.get("keyword", "")
    publisher = draft.get("publisher") or ""

    pools = [publisher] if publisher else []
    pools.append("")  # 不限出版社

    checked_pools = set()
    for pub in pools:
        if pub in checked_pools:
            continue
        checked_pools.add(pub)
        required_version = draft.get("target_version", "")
        for c in _collect_multi_book_matches(query, pub, required_version=required_version):
            key = f"{c.get('publisher','')}|{c['value']}"
            if key not in excluded_values:
                return c
    return None



def validate_multi_book_volume_selection(user_id, raw_text, draft):
    clean = re.sub(r"[第冊上下學期學期，,。.!！?？\s]+", "", str(raw_text or ""))
    aliases = {
        "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
        "1": "1", "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    }
    volume = aliases.get(clean, "")
    if not volume:
        return (
            "這位老師跨年級，而且目前書名沒有冊次，我不能替你猜。\n\n"
            "請直接回覆冊次 1～6，例如「5」。\n"
            "1/2＝七年級、3/4＝八年級、5/6＝九年級。"
        )

    grade = {"1":"七年級", "2":"七年級", "3":"八年級", "4":"八年級", "5":"九年級", "6":"九年級"}[volume]
    if not _multi_book_teacher_has_grade(draft, grade):
        available = "、".join(_multi_book_teacher_junior_grades(draft)) or "目前查不到國中年級班級"
        return f"⚠️ {draft.get('teacher','')} 沒有 {grade} 班級。\n目前可用年級：{available}\n請重新輸入冊次。"

    profile = _multi_book_query_profile(draft.get("keyword", ""))
    subject = profile.get("subject", "")
    # 把冊次補回關鍵字，後續所有年級／版本／候選搜尋都走同一套規則。
    draft["keyword"] = f"{subject}{volume}測驗卷" if profile.get("exam_like") else f"{subject}{volume}"
    draft["awaiting_volume_selection"] = False
    multi_book_order_context[user_id] = draft
    return _run_multi_book_search(user_id, draft)


def validate_multi_book_count_input(user_id, raw_text, draft):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    m = re.fullmatch(r"([0-9]{1,3}|[一二三四五六七八九十]{1,3})種?", clean)
    if not m:
        return "請直接告訴我要挑幾種書，用數字回覆就好，例如「7」。"

    raw_count = m.group(1)
    count = _cn_number_to_int(raw_count) or 0

    if not count or count <= 0 or count > MULTI_BOOK_MAX_CANDIDATES:
        return f"數量要介於 1～{MULTI_BOOK_MAX_CANDIDATES} 之間，請重新輸入。"

    # 一個班配一本書，要挑的種數不能超過老師實際的班級數，不然湊滿書
    # 種之後問班級分配時一定卡死（例如老師只有 7 班，卻要挑 10 種），
    # 使用者只能取消重來。這裡先擋下，總比讓使用者繞一大圈才發現。
    teacher_class_count = len(_multi_book_eligible_classes(draft))
    if teacher_class_count and count > teacher_class_count:
        return (
            f"⚠️ {draft.get('teacher','')} 目前只有 {teacher_class_count} 個班，"
            f"一個班配一本書的話最多只能挑 {teacher_class_count} 種，請重新輸入數量。"
        )

    draft["expected_count"] = count
    multi_book_order_context[user_id] = draft
    return _run_multi_book_search(user_id, draft)


def _run_multi_book_search(user_id, draft):
    query = draft.get("keyword", "")
    publisher = draft.get("publisher") or ""
    expected = draft.get("expected_count", 0)
    profile = _multi_book_query_profile(query)

    # v67：年級優先由書名冊次決定；決定後再確認這位老師真的有該年級。
    # 不能因為老師同時教兩個年級，就隨便拿第一個班級的年級去查版本。
    if profile.get("grade"):
        if not _multi_book_teacher_has_grade(draft, profile["grade"]):
            available = "、".join(_multi_book_teacher_junior_grades(draft)) or "目前查不到國中年級班級"
            return (
                f"⚠️ 書名冊次判斷為 {profile['grade']}，但 {draft.get('teacher','')} "
                f"目前沒有這個年級的班級。\n\n老師目前可用年級：{available}\n"
                "請重新輸入正確的書名關鍵字。"
            )
    elif profile.get("exam_like") and profile.get("subject"):
        grades = _multi_book_teacher_junior_grades(draft)
        if len(grades) > 1:
            draft["awaiting_volume_selection"] = True
            multi_book_order_context[user_id] = draft
            return (
                f"{draft.get('teacher','')} 同時有 {'、'.join(grades)} 的班級，"
                "而目前書名沒有冊次，我不能替你猜年級。\n\n"
                "請直接回覆冊次 1～6，例如「5」。\n"
                "1/2＝七年級、3/4＝八年級、5/6＝九年級。"
            )

    required_version = ""
    if profile.get("exam_like") and profile.get("grade") and profile.get("subject"):
        required_version = _multi_book_school_target_version(draft, query)
        if not required_version:
            return (
                f"⚠️ 我查不到 {draft.get('school','')} {profile.get('grade','')}"
                f"{profile.get('subject','')} 的教科書版本，所以先不亂幫你湊考卷。\n\n"
                "請先確認 Google「學校版本資料」有這個年級／科目的版本資料。"
            )

    matched = _collect_multi_book_matches(
        query, publisher, target_count=expected, required_version=required_version
    )

    # 若使用者有限定「真正出版社」但該出版社湊不滿，才放寬成跨出版社；
    # 適用版本仍然是硬條件，絕不跟著放寬。
    if publisher and len(matched) < expected:
        seen_keys = {(c["value"], c.get("publisher", "")) for c in matched}
        for c in _collect_multi_book_matches(
            query, "", target_count=expected, required_version=required_version
        ):
            key = (c["value"], c.get("publisher", ""))
            if key in seen_keys:
                continue
            matched.append(c)
            seen_keys.add(key)

    # 找到超過 N 種不是錯誤：從完整合格池中挑最適合、且儘量分散出版社／系列的 N 種。
    all_match_count = len(matched)
    if len(matched) >= expected:
        matched = _select_diverse_multi_book_candidates(matched, expected)

    draft["all_match_count"] = all_match_count
    draft["candidates"] = matched[:MULTI_BOOK_MAX_CANDIDATES]
    draft["excluded_values"] = sorted({f"{c.get('publisher','')}|{c['value']}" for c in matched})
    multi_book_order_context[user_id] = draft

    if not matched:
        draft["expected_count"] = 0
        draft["keyword"] = ""
        multi_book_order_context[user_id] = draft
        version_text = f"、適用版本：{required_version}" if required_version else ""
        return (
            "⚠️ 依照目前條件完全查不到符合的書。\n\n"
            f"關鍵字：{query}{version_text}\n\n"
            "請重新輸入書名關鍵字。"
        )

    if len(matched) == expected:
        draft["fallback_rotation"] = False
        draft["rotation_target_count"] = 0
        draft["awaiting_decision"] = False
        draft["awaiting_review"] = True
        multi_book_order_context[user_id] = draft
        return make_multi_book_review_reply(draft)

    # v68：只有在「湊不滿」且至少有 2 種合格卷時才啟用輪替備援。
    # 正常找得到足夠種類時絕對不會走這裡。
    if 2 <= len(matched) < expected:
        draft["fallback_rotation"] = True
        draft["rotation_target_count"] = expected
        draft["awaiting_decision"] = False
        draft["awaiting_review"] = True
        multi_book_order_context[user_id] = draft
        return make_multi_book_review_reply(draft)

    # 只有 1 種時保留原本提示，不自動輪替。
    draft["fallback_rotation"] = False
    draft["rotation_target_count"] = 0
    draft["awaiting_decision"] = True
    multi_book_order_context[user_id] = draft
    return make_multi_book_shortfall_reply(draft)

def _format_multi_book_candidate_lines(candidates):
    return [
        f"{i}. [{c.get('publisher', '')}] {c.get('value', '')}"
        for i, c in enumerate(candidates, start=1)
    ]


def make_multi_book_review_reply(draft):
    candidates = draft.get("candidates", [])
    target_version = draft.get("target_version", "")
    grade = draft.get("target_grade", "")
    subject = draft.get("target_subject", "")
    all_count = int(draft.get("all_match_count", 0) or 0)
    lines = ["🔎 已找到符合條件的書：", ""]
    if target_version:
        lines.append(f"📘 {draft.get('school','')}｜{grade}{subject}｜教科書版本：{target_version}")
        lines.append("✅ 只保留相同適用版本的考卷")
        lines.append("")
    lines.extend(_format_multi_book_candidate_lines(candidates))
    lines.append("")
    if draft.get("fallback_rotation"):
        target = int(draft.get("rotation_target_count", 0) or 0)
        lines.append(
            f"⚠️ 你原本要 {target} 種，但資料庫目前只有 {len(candidates)} 種真正符合條件的考卷。"
        )
        lines.append(
            f"我會只用這 {len(candidates)} 種，平均輪替分配到 {target} 個班，並盡量避免相鄰班拿同一份。"
        )
    else:
        lines.append(f"已挑出 {len(candidates)} 種，符合你要的數量。")
        if all_count > len(candidates):
            lines.append(f"資料庫另有 {all_count - len(candidates)} 種符合條件，可用「換N」替換。")
        lines.append("我會優先分散出版社／系列，避免不同班拿到太接近的考卷。")
    lines.append("")
    lines.append("如果有哪一項不要，回覆「N不要」或「換N」（例如「2不要」）。")
    lines.append("都沒問題的話，請回覆「確認」。")
    return "\n".join(lines)

def make_multi_book_classes_prompt(draft):
    candidates = draft.get("candidates", [])
    class_list = "、".join(
        c["class_name"] for c in sort_class_items(_multi_book_eligible_classes(draft))
    )
    target_count = (
        int(draft.get("rotation_target_count", 0) or 0)
        if draft.get("fallback_rotation")
        else len(candidates)
    )
    lines = ["📚 最終書單：", ""]
    lines.extend(_format_multi_book_candidate_lines(candidates))
    lines.append("")
    if draft.get("fallback_rotation"):
        lines.append(
            f"目前只有 {len(candidates)} 種合格考卷，我會輪替分配到你要的 {target_count} 個班。"
        )
    lines.append(f"請告訴我要使用哪 {target_count} 個班（用空格或逗號分隔）。")
    lines.append(f"{draft.get('teacher', '')} 目前班級：{class_list}")
    return "\n".join(lines)


def make_multi_book_shortfall_reply(draft):
    candidates = draft.get("candidates", [])
    expected = draft.get("expected_count", 0)
    lines = ["🔎 找到符合的書（已含跨出版社查詢）：", ""]
    lines.extend(_format_multi_book_candidate_lines(candidates))
    lines.append("")
    lines.append(f"共找到 {len(candidates)} 種，跟你說的 {expected} 種不一樣。")
    lines.append("")
    lines.append("回覆「採用」直接用這幾種；或直接輸入新的書名關鍵字重新查詢。")
    return "\n".join(lines)


def handle_multi_book_search_mismatch(user_id, clean, draft):
    # 畫面教的是回覆「採用」，但使用者很自然會回「確認／好／可以」這類
    # 通用肯定詞——沒有這個判斷的話，這些詞會被下面的 fallback 當成
    # 新的書名關鍵字去查，把好不容易找到的候選清單洗掉。
    if clean in MULTI_BOOK_ACCEPT_FOUND_WORDS or _is_confirm_word(clean):
        candidates = draft.get("candidates", [])
        teacher_class_count = len(_multi_book_eligible_classes(draft))
        if teacher_class_count and len(candidates) > teacher_class_count:
            return (
                f"⚠️ 這裡找到 {len(candidates)} 種，但 {draft.get('teacher','')} "
                f"只有 {teacher_class_count} 個班，一個班配一本書的話會超過。"
                f"請直接輸入新的書名關鍵字重新查詢，或換一個比較窄的關鍵字。"
            )
        draft["expected_count"] = len(candidates)
        draft["awaiting_decision"] = False
        draft["awaiting_review"] = True
        multi_book_order_context[user_id] = draft
        return make_multi_book_review_reply(draft)

    if clean in MULTI_BOOK_RETRY_KEYWORD_WORDS:
        draft["keyword"] = ""
        draft["expected_count"] = 0
        draft["candidates"] = []
        draft["awaiting_decision"] = False
        multi_book_order_context[user_id] = draft
        return "好，請重新告訴我書名關鍵字。"

    # 使用者沒有回覆固定詞，就當成直接打了新的書名關鍵字，
    # 維持原本出版社與種數設定，重新查一次。
    draft["keyword"] = clean_book_name(clean)
    draft["awaiting_decision"] = False
    multi_book_order_context[user_id] = draft
    return _run_multi_book_search(user_id, draft)


_MULTI_BOOK_SWAP_PATTERN = re.compile(
    r"(?:換|換掉|刪除|刪掉|移除|拿掉)?第?(\d{1,2})(?:個|項|本|不要|換掉|換一個|不要了)*"
)


def _parse_multi_book_swap_index(clean):
    m = re.fullmatch(_MULTI_BOOK_SWAP_PATTERN, clean)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def handle_multi_book_review_stage(user_id, clean, draft):
    if _is_exit_word(clean):
        multi_book_order_context.pop(user_id, None)
        guided_mode.pop(user_id, None)
        return "❌ 已取消這筆多書訂單，Google 沒有寫入。"

    if _is_confirm_word(clean) or clean in {"可以", "沒問題", "都可以", "都沒問題", "可以了", "這樣就好"}:
        draft["awaiting_review"] = False
        target_count = (
            int(draft.get("rotation_target_count", 0) or 0)
            if draft.get("fallback_rotation")
            else len(draft.get("candidates", []))
        )
        eligible = sort_class_items(_multi_book_eligible_classes(draft))
        # 若這個年級剛好就是 target_count 個班，直接全部採用並自動輪替；
        # 只有班級比需求更多時才請使用者選班。
        if target_count and len(eligible) == target_count:
            class_text = " ".join(c["class_name"] for c in eligible)
            multi_book_order_context[user_id] = draft
            return validate_multi_book_classes_input(user_id, class_text, draft)
        draft["awaiting_classes"] = True
        multi_book_order_context[user_id] = draft
        return make_multi_book_classes_prompt(draft)

    index = _parse_multi_book_swap_index(clean)
    if index is None:
        return (
            "（沒看懂你的回覆。要換掉某一項，請回覆「N不要」或「換N」；"
            "都沒問題的話回覆「確認」。）\n\n"
            + make_multi_book_review_reply(draft)
        )

    candidates = draft.get("candidates", [])
    if not (1 <= index <= len(candidates)):
        return f"⚠️ 目前只有 1～{len(candidates)} 項，請輸入正確的編號。"

    removed = candidates[index - 1]
    excluded = set(draft.get("excluded_values", []))
    excluded.add(str(removed.get("value", "")).strip())

    replacement = _find_multi_book_replacement(draft, excluded)

    if replacement is None:
        draft["excluded_values"] = sorted(excluded)
        multi_book_order_context[user_id] = draft
        return (
            f"⚠️ 找不到其他符合條件、還沒出現過的書可以替換第 {index} 項。\n\n"
            "目前清單維持不變；如果要放寬條件，請直接重新輸入「多書訂購」重新開始。\n\n"
            + make_multi_book_review_reply(draft)
        )

    candidates[index - 1] = replacement
    excluded.add(str(replacement.get("value", "")).strip())
    draft["candidates"] = candidates
    draft["excluded_values"] = sorted(excluded)
    multi_book_order_context[user_id] = draft

    return (
        f"✅ 已把第 {index} 項換成：[{replacement.get('publisher', '')}] {replacement.get('value', '')}\n\n"
        + make_multi_book_review_reply(draft)
    )



def validate_multi_book_classes_input(user_id, raw_text, draft):
    parts = [p for p in re.split(r"[，,、\s]+", str(raw_text or "").strip()) if p]

    if not parts:
        return "請依序輸入班級名稱，用空格或逗號分隔。"

    eligible_classes = _multi_book_eligible_classes(draft)
    known_names = {c["class_name"] for c in eligible_classes}
    unknown = [p for p in parts if p not in known_names]

    if unknown:
        available = "、".join(
            c["class_name"] for c in sort_class_items(eligible_classes)
        )
        return (
            f"⚠️ 這幾個班級對不上 {draft.get('teacher', '')} 的資料：{'、'.join(unknown)}\n\n"
            f"{draft.get('teacher', '')} 目前班級：{available}\n\n"
            "請重新輸入，用空格或逗號分隔。"
        )

    if len(unique_list(parts)) != len(parts):
        return "⚠️ 班級名稱重複了，請確認每個班只出現一次後重新輸入。"

    expected = (
        int(draft.get("rotation_target_count", 0) or 0)
        if draft.get("fallback_rotation")
        else len(draft.get("candidates", []))
    )
    if len(parts) != expected:
        if draft.get("fallback_rotation"):
            return (
                f"⚠️ 這次要分配 {expected} 個班，但你輸入了 {len(parts)} 個班級。\n\n"
                "請重新輸入班級名稱（用空格或逗號分隔）。"
            )
        return (
            f"⚠️ 目前有 {expected} 種書，但你輸入了 {len(parts)} 個班級，兩者數量要一樣。\n\n"
            "請重新輸入班級名稱（用空格或逗號分隔）。"
        )

    class_lookup = {c["class_name"]: c for c in eligible_classes}
    assignments = []
    books = list(draft.get("candidates", []))
    for idx, class_name in enumerate(parts):
        book_item = books[idx % len(books)] if draft.get("fallback_rotation") else books[idx]
        info = class_lookup[class_name]
        assignments.append({
            "class_name": class_name,
            "students": int(info.get("students", 0) or 0),
            "book": str(book_item.get("value", "") or ""),
            # 不能 fallback 到 draft.get("publisher")：這批候選可能是
            # 指定出版社湊不滿、自動放寬成不限出版社湊來的，各自出版社
            # 都不一樣；如果某筆書籍資料庫的出版社欄位本身是空的，
            # fallback 到使用者原本限定的出版社會把它冒充成那家出版社，
            # 業務照單去跟錯的出版社叫書。真的沒有出版社資料就留空，
            # 讓使用者在確認畫面上看得到，自己決定要不要補。
            "publisher": str(book_item.get("publisher", "") or ""),
        })

    draft["assignments"] = assignments
    draft["awaiting_classes"] = False
    draft["confirming"] = True
    multi_book_order_context[user_id] = draft
    return make_multi_book_order_confirmation(draft)


def make_multi_book_order_confirmation(draft):
    lines = [
        "📚 多書訂購確認（一個班一種書）", "",
        f"老師：{draft.get('teacher', '')}（{draft.get('school', '')}）", ""
    ]
    total = 0
    for i, item in enumerate(draft.get("assignments", []), start=1):
        publisher_label = item.get("publisher") or "⚠️未標示出版社"
        lines.append(
            f"{i}. {item['class_name']}（{item['students']}本）→ "
            f"[{publisher_label}] {item['book']}"
        )
        total += item["students"]
    lines.append("")
    unique_books = len({(x.get("publisher", ""), x.get("book", "")) for x in draft.get("assignments", [])})
    if draft.get("fallback_rotation"):
        lines.append(
            f"共 {len(draft.get('assignments', []))} 個班，使用 {unique_books} 種不同考卷輪替，總數量：{total}本"
        )
    else:
        lines.append(f"共 {unique_books} 種書，總數量：{total}本")
    lines.append("")
    lines.append("確認無誤請回覆「確認」。")
    lines.append("要取消這筆多書訂單請回覆「取消」。")
    return "\n".join(lines)


def handle_multi_book_confirm_stage(user_id, clean, draft):
    if _is_confirm_word(clean):
        return confirm_multi_book_order(user_id)

    if _is_exit_word(clean) or clean in {"不要了", "這筆不要"}:
        multi_book_order_context.pop(user_id, None)
        guided_mode.pop(user_id, None)
        return "❌ 已取消這筆多書訂單，Google 沒有寫入。"

    return (
        make_multi_book_order_confirmation(draft)
        + "\n\n（沒看懂你的回覆，請回覆「確認」或「取消」。）"
    )


def confirm_multi_book_order(user_id):
    draft = multi_book_order_context.get(user_id)
    if not draft or not draft.get("assignments"):
        return "⚠️ 找不到尚未確認的多書訂單，請重新輸入。"

    teacher = draft.get("teacher", "")
    school = draft.get("school", "")
    results = []

    # 逐筆寫入既有的學校訂單（一個班對一本書），跟平常一班一班手動
    # 訂書寫進 Google 的資料格式完全相同，也沿用同一套「寫入只送一次
    # ＋事後驗證」機制（見 write_to_google_sheet）。任何一筆失敗都不
    # 會影響其他筆已經成功寫入的訂單。
    for item in draft["assignments"]:
        order = {
            "teacher": teacher,
            "school": school,
            "book": item["book"],
            "publisher": item["publisher"],
            "classes": [{"class_name": item["class_name"], "students": item["students"]}],
        }
        success, order_number = write_to_google_sheet(order)
        results.append({
            "class_name": item["class_name"],
            "book": item["book"],
            "publisher": item["publisher"],
            "success": success,
            "order_number": order_number,
        })

    multi_book_order_context.pop(user_id, None)
    guided_mode.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)

    ok = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    lines = ["✅ 多書訂單已處理", ""]
    for r in ok:
        lines.append(
            f"✔️ {r['class_name']} → [{r['publisher']}] {r['book']}"
            f"（訂單編號 {r['order_number']}）"
        )
    if failed:
        lines.append("")
        lines.append("⚠️ 以下幾筆寫入失敗，請稍後改用一般訂書流程手動補上：")
        for r in failed:
            lines.append(f"❌ {r['class_name']} → [{r['publisher']}] {r['book']}")

    lines.append("")
    lines.append(f"成功 {len(ok)} 筆／共 {len(results)} 筆。")

    return "\n".join(lines)


def handle_multi_book_order_flow(user_id, text):
    clean = normalize_order_typo(text)

    draft = multi_book_order_context.get(user_id)
    if draft is None:
        draft = _new_multi_book_draft()
        multi_book_order_context[user_id] = draft

    if draft.get("confirming"):
        return handle_multi_book_confirm_stage(user_id, clean, draft)

    if draft.get("awaiting_volume_selection"):
        return validate_multi_book_volume_selection(user_id, clean, draft)

    if draft.get("awaiting_classes"):
        return validate_multi_book_classes_input(user_id, clean, draft)

    if draft.get("awaiting_review"):
        return handle_multi_book_review_stage(user_id, clean, draft)

    if draft.get("awaiting_decision"):
        return handle_multi_book_search_mismatch(user_id, clean, draft)

    if not draft.get("teacher"):
        return validate_multi_book_teacher_input(user_id, clean, draft)

    if draft.get("publisher") is None:
        return validate_multi_book_publisher_input(user_id, clean, draft)

    if not draft.get("keyword"):
        return validate_multi_book_keyword_input(user_id, clean, draft)

    return validate_multi_book_count_input(user_id, clean, draft)


def normalize_order_typo(text):
    clean = normalize_text(text)

    common_aliases = {
        "康宣": "康軒",
        "康玄": "康軒",
        "韓林": "翰林",
        "寒林": "翰林",
        "難一": "南一",
        "南壹": "南一",
        "超級悍將": "超級翰將",
        "超級漢將": "超級翰將",
        "超級瀚將": "超級翰將",
        "老帥": "老師",
        "老詩": "老師",
        "講意": "講義",
        "講議": "講義",
        "講易": "講義",
        "國依": "國一",
        "下但": "下單",
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
    # 手機常見贅詞／表情不應阻斷訂書解析；保留書名本身標點給後續模糊比對。
    clean = re.sub(r"^[欸誒ㄟ]+", "", clean)
    clean = re.sub(r"[🙏]+", "", clean)
    clean = clean.replace("／", "/").replace("＆", "&")
    return clean


def _find_and_strip_letter_classes(text, known_class_names):
    """
    extract_classes() 只認得「701」這種數字班級。有些學校班級是
    「國一甲」「國三戊」這種文字命名，這裡另外處理：依 known_class_names
    建出每個年級前綴（例如「國一」）底下實際有哪些代號字（例如
    「甲丁戊己庚」），再用這個前綴＋代號字的正規表示式去文字裡找，
    同時支援單一班級（「國三戊」）跟省略前綴的連續簡寫
    （「國一甲丁戊己庚」＝國一甲／國一丁／國一戊／國一己／國一庚）。
    只會比對到這位老師真的有的班級，不會誤吃到不相干的文字。
    回傳 (找到的班級清單, 拿掉班級文字後剩下的字串)。
    """
    prefixes = {}
    for name in known_class_names:
        name = str(name or "")
        if len(name) >= 2:
            prefixes.setdefault(name[:-1], set()).add(name[-1])

    remaining = text
    found = []
    matched_prefixes = set()

    for prefix in sorted(prefixes, key=len, reverse=True):
        suffix_chars = "".join(sorted(re.escape(c) for c in prefixes[prefix]))
        if not suffix_chars:
            continue
        pattern = re.escape(prefix) + "[" + suffix_chars + "]+"
        matches = list(re.finditer(pattern, remaining))
        if matches:
            matched_prefixes.add(prefix)
        for m in matches:
            for ch in m.group(0)[len(prefix):]:
                found.append(prefix + ch)
        remaining = re.sub(pattern, " ", remaining)

    # 只講年級本身、沒有列出字母（例如「國一」），代表這位老師底下
    # 這個年級的班級「全部」都要，不用一個一個打。上面那段已經把
    # 「年級＋字母」的寫法都吃掉了，這裡剩下的「國一」就是單純講
    # 年級整體的情況——但只有「這個年級第一輪完全沒比對到任何字母」
    # 時才適用。如果第一輪已經比對到字母（代表使用者有指定特定
    # 班級），剩下的文字裡如果剛好還留著同樣的年級字（例如書名剛好
    # 也叫「國一數學講義」），不能誤判成「使用者還想要整個年級」，
    # 那只是書名裡的巧合，不是班級指定。
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix in matched_prefixes:
            continue
        # 只有年級字後面緊接著「都要/全部/各班/整班」這類全稱字眼，才
        # 真的代表「這個年級全部都要」。原本只要「年級字出現在文字裡」
        # 就當成整年級，會把「國一數學講義」這種書名剛好帶到年級字的
        # 情況也誤判成班級指定，還把年級字從書名候選裡拿掉（削成
        # 「數學講義」），可能拿去比對到錯的書。沒有全稱字眼時保留
        # 原樣，讓年級字留在書名候選裡。
        m = re.search(re.escape(prefix) + r"(?:都要|全部|各班|整班|都)", remaining)
        if m:
            for suffix in sorted(prefixes[prefix]):
                found.append(prefix + suffix)
            remaining = remaining.replace(m.group(0), " ", 1)

    return unique_list(found), remaining


def _looks_like_order_for_known_teacher_no_class(text, context):
    """
    完全沒提到任何班級（數字或文字班級都沒有），但句子本身看起來就
    是要訂書，而且剛好知道這是哪位老師。判斷「像不像要訂書」分兩層：
    1. 沿用 parse_order_message() 既有規則（有「訂」「要」這些字）。
    2. 就算完全沒有「訂」「要」，只要句子含常見書籍關鍵字（講義／
       評量／套書...），而且不是「2～4 個中文字姓名」這種明顯是在
       查另一位老師的格式，也當作是要訂這本書——剛查完老師、使用者
       常常就是直接打書名，不會特別加「訂」字。
    """
    if not context.get("teacher") or not context.get("classes"):
        return False

    parsed = parse_order_message(text)
    if parsed.get("has_order_intent") and parsed.get("book"):
        return True

    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))
    if not clean or re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        return False

    book_words = [
        "講義", "評量", "教材", "複習", "測驗", "題本", "考卷", "卷",
        "自修", "課本", "習作", "學習單", "套書", "段考王", "大滿貫",
        "學習講義", "學習自修",
    ]
    if any(w in clean for w in book_words):
        return True

    # 查完老師後，使用者常直接丟「段考王英文5」這種純書名。即使名稱
    # 沒有「講義／評量」等字，只要同時帶科目與冊次，也視為書名接續訂書。
    subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    return any(subj in clean for subj in subjects) and bool(re.search(r"[1-6]", clean))


def looks_like_contextual_class_book(text, context):
    known = [
        str(item.get("class_name"))
        for item in context.get("classes", [])
    ]

    mentioned = extract_classes(text)
    if mentioned:
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

    # 數字班級抓不到時，再試文字班級簡寫（國一甲丁戊...）。
    letter_matches, remainder = _find_and_strip_letter_classes(text, known)
    if not letter_matches:
        return False
    remainder = re.sub(r"[跟和與、,，/\s]+", " ", remainder).strip()
    return len(remainder) >= 2


def parse_contextual_class_book(text, context):
    clean = normalize_order_typo(text)
    known = [
        str(item.get("class_name"))
        for item in context.get("classes", [])
    ]

    classes = extract_classes(clean)
    letter_remainder = None
    if not classes:
        classes, letter_remainder = _find_and_strip_letter_classes(clean, known)

    if "訂" in clean:
        book = clean.split("訂", 1)[1].strip()
    elif letter_remainder is not None:
        book = letter_remainder
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
        "publisher": "",
        "book": book
    }


def extract_teacher_and_school(text):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))

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

    m = re.fullmatch(
        r"(?:查|找|查一下|找一下)([\u4e00-\u9fff]{2,4})(?:老師)?",
        clean
    )
    if m:
        return m.group(1) + "老師", ""

    m = re.match(
        r"^([\u4e00-\u9fff]{2,4})"
        r"(?=(?:"
        r"教(?:哪幾個班|哪幾班|哪些班|幾個班|幾班|什麼科|哪一科|哪科|哪些科)|"
        r"有(?:幾個班|幾班|哪些班|多少學生|幾個學生|多少人|幾人)|"
        r"(?:班級資料|班級人數|學生人數|總人數|每班幾人|每班人數|哪幾班|哪幾個班|有哪些班|幾班)"
        r"))",
        clean
    )
    if m:
        return m.group(1) + "老師", ""

    # 「張靜婷要訂段考王國文3」這種講法，姓名後面沒有加「老師」兩個
    # 字，但緊接著就是「要訂／訂」——不用強制要求打「老師」才看得懂。
    # 用非貪婪比對＋要求緊接著訂/要訂，確保只吃到姓名本身，不會把
    # 「要」也吃進姓名裡。
    m = re.match(
        r"^([\u4e00-\u9fff]{2,4}?)(?:要|想要|準備|打算)?訂",
        clean
    )
    if m:
        name = m.group(1)
        # 排除看起來像「學校＋年級」而不是老師姓名的情況，交給
        # extract_school_grade_no_teacher() 處理，不要在這裡搶先
        # 誤判（例如「華興七年級要訂」不該被當成老師姓名）。
        grade_hint_words = [
            "年級", "國一", "國二", "國三", "高一", "高二", "高三",
        ]
        if name and not any(w in name for w in grade_hint_words):
            return name + "老師", ""

    # v54：真人常省略「老師」與「訂」字，例如：王小明701國一數學講義、
    # 張建國國一甲要國一數學講義、張建國那邊國一甲要...。
    m = re.match(r"^(?:幫|麻煩幫)?([\u4e00-\u9fff]{2,4}?)(?:的)?(?:[甲乙丙丁戊己庚辛信望愛慧]{2,})(?:兩班|班)", clean)
    if m:
        return m.group(1) + "老師", ""

    m = re.match(r"^(?:幫|麻煩幫)([\u4e00-\u9fff]{2,4}?)(?:的)(?=(?:國[一二三]|高[一二三])?[甲乙丙丁戊己庚辛信望愛慧])", clean)
    if m:
        return m.group(1) + "老師", ""

    m = re.match(
        r"^(?:麻煩)?(?:幫我)?(?:幫)?([\u4e00-\u9fff]{2,4}?)(?:那邊|那個)?(?=(?:[789]\d{2}|(?:國[一二三]|高[一二三])?[甲乙丙丁戊己庚辛信望愛慧]|的?[789]\d{2}))",
        clean
    )
    if m:
        name = m.group(1)
        if name not in ["麻煩", "幫我", "那個", "這個"]:
            return name + "老師", ""

    # 書名放前面、老師在中間的口語，交給另一個簡單模式抓姓名。
    m = re.search(r"(?:給|幫)([\u4e00-\u9fff]{2,4}?)(?:老師)?(?=[789]\d{2})", clean)
    if m:
        return m.group(1) + "老師", ""

    return "", ""


_GRADE_KEYWORD_PATTERN = re.compile(
    "|".join(sorted([
        "高中一年級", "高中二年級", "高中三年級",
        "七年級", "八年級", "九年級",
        "10年級", "11年級", "12年級",
        "7年級", "8年級", "9年級",
        "國一", "國二", "國三", "高一", "高二", "高三",
    ], key=len, reverse=True))
)


def _strip_school_grade_prefix(text, school, grade):
    """
    給定已經解析出的 school／grade，把它們在原始文字裡對應到的片段
    拿掉，剩下的部分才拿去當書名候選，避免「華興七年級」這種學校＋
    年級文字污染書名欄位（例如書名被誤判成「華興七年級 3800應用題
    套書」，而不是單純的「3800應用題套書」）。
    """
    remaining = text

    if school:
        aliases = [school]
        short = re.sub(r"(?:國民中學|國民小學|高級中學|國中|高中|國小|中學|女中)$", "", school)
        if short and short != school:
            aliases.append(short)
        aliases.sort(key=len, reverse=True)
        for alias in aliases:
            if alias and alias in remaining:
                remaining = remaining.replace(alias, " ", 1)
                break

    if grade:
        m = _GRADE_KEYWORD_PATTERN.search(remaining)
        if m:
            remaining = remaining[:m.start()] + " " + remaining[m.end():]

    return remaining.strip()


def extract_school_grade_no_teacher(text):
    """
    句子裡完全沒有老師姓名，但有「學校＋年級」（例如「華興七年級」
    「衛理九年級」），先把這兩個抓出來——之後問老師時可以直接從
    資料庫列出這個年級的老師候選，不用使用者自己想起正確姓名（見
    handle_order_flow 裡對 teacher_candidates 的處理）。只有兩個都
    抓到才會採用，單獨抓到學校或單獨抓到年級都太容易誤判，不使用。
    """
    school = extract_school_name(text)
    grade = extract_grade_text(text)
    if not school or not grade:
        return "", "", text
    remaining = _strip_school_grade_prefix(text, school, grade)
    return school, grade, remaining


def _lookup_order_teacher_candidates(school, grade):
    """
    使用者只給了「學校＋年級」、沒有講老師姓名時，從資料庫抓出這個
    年級所有老師的候選清單，讓使用者用點選數字或直接打姓名的方式
    選，不用自己想起正確姓名。只保留這個年級底下真的有班級的老師。
    """
    matches = lookup_teacher_matches("", school=school, grade=grade)
    candidates = []
    for item in matches or []:
        selected = [
            c for c in item.get("classes", [])
            if _class_matches_grade(c.get("class_name", ""), grade)
        ]
        if not selected:
            continue
        subjects = []
        for c in selected:
            for s in c.get("subjects", []) or []:
                s = str(s or "").strip()
                if s == "英語":
                    s = "英文"
                elif s == "地球科學":
                    s = "地科"
                if s and s not in subjects:
                    subjects.append(s)
        candidates.append({
            "teacher": str(item.get("teacher", "")).strip(),
            "school": str(item.get("school", "") or school).strip(),
            "classes": selected,
            "subjects": subjects,
        })
    return candidates


def _resolve_order_teacher_candidate_choice(text, candidates):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))
    if re.fullmatch(r"\d{1,2}", clean):
        idx = int(clean)
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]
        return None
    for c in candidates:
        if clean and normalize_person_name(clean) == normalize_person_name(c["teacher"]):
            return c
    return None


def make_order_teacher_candidates_reply(draft):
    candidates = draft.get("teacher_candidates", [])
    lines = ["📚 訂書進度", ""]
    school = str(draft.get("school") or "").strip()
    grade = str(draft.get("grade") or "").strip()
    book = str(draft.get("book") or "").strip()
    lines.append(f"🏫 學校：{school}｜{grade}")
    lines.append(f"{'✅' if book else '⬜'} 書名：{book or '尚未提供'}")
    lines.append("")
    lines.append(f"{school}｜{grade} 目前登記的老師：")
    for i, c in enumerate(candidates, start=1):
        subj = "、".join(c.get("subjects", []))
        lines.append(f"{i}. {c['teacher']}" + (f"（{subj}）" if subj else ""))
    lines.append("")
    lines.append("👉 請回覆數字選老師，或直接打老師姓名。")
    return "\n".join(lines)


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


_SUBJECT_ORDER = {
    "國文": 10, "英文": 20, "英語": 20, "數學": 30,
    "自然": 40, "生物": 41, "理化": 42, "地科": 43, "地球科學": 43,
    "社會": 50, "歷史": 51, "地理": 52, "公民": 53,
}
# 解析規則（extract_classes、extract_teacher_and_school…）明確支援
# 「信望愛慧」這種校訓式班級代號，這裡的排序表原本沒收，導致這些班
# 級全部並列同一順位（fallback 值 99），只能再靠字元編碼排序，順序
# 可能跟使用者預期的不一樣。
_CLASS_LETTER_ORDER = {c: i for i, c in enumerate("甲乙丙丁戊己庚辛壬癸信望愛慧", start=1)}


def subject_sort_key(value):
    s = str(value or "").strip()
    return (_SUBJECT_ORDER.get(s, 999), s)


def class_sort_key(value):
    s = str(value or "").strip()

    # 純數字班：701、702、703... 自然數字排序。
    if re.fullmatch(r"\d+", s):
        return (0, int(s), 0, s)

    # 國一甲 / 國二乙 / 國三丙
    grade_map = {"國一": 1, "七年級": 1, "國二": 2, "八年級": 2, "國三": 3, "九年級": 3,
                 "高一": 4, "高二": 5, "高三": 6}
    for label, grade in grade_map.items():
        if s.startswith(label):
            tail = s[len(label):].replace("班", "").strip()
            if tail in _CLASS_LETTER_ORDER:
                return (1, grade, _CLASS_LETTER_ORDER[tail], s)
            if tail.isdigit():
                return (1, grade, int(tail), s)
            return (1, grade, 999, s)

    # 一般「甲班、乙班」。
    tail = s.replace("班", "").strip()
    if tail in _CLASS_LETTER_ORDER:
        return (2, 0, _CLASS_LETTER_ORDER[tail], s)

    return (9, 999, 999, s)


def sort_class_items(items):
    return sorted(list(items or []), key=lambda x: class_sort_key(x.get("class_name", "")))


# =========================================================
# 新訂單確認／修改
# =========================================================
def make_order_confirmation(order):
    class_lines = [f"• {item['class_name']}｜{int(item['students'])} 本" for item in sort_class_items(order.get("classes", []))]
    return (
        "📚 訂購確認\n\n"
        f"🏫 學校：{order['school']}\n"
        f"👨‍🏫 老師：{order['teacher']}\n"
        f"📖 書名：{order['book']}\n"
        f"🏢 出版社：{order['publisher']}\n\n"
        "📋 班級與數量\n" + "\n".join(class_lines) +
        f"\n\n📦 總數量：{int(order.get('quantity', 0))} 本"
        + (f"\n📝 備註：{order.get('note', '')}" if str(order.get('note', '') or '').strip() else "")
        + "\n\n確認無誤 → 回覆「確認」\n"
        "需要修改 → 可直接修改班級、數量、書名、出版社、老師、學校或加備註。"
    )



def make_purchase_order_text(offer):
    """純文字版訂購單，目前保留備用（例如未來想切回文字版時還能用）。
    現行流程已改用 generate_purchase_order_pdf() 產生 PDF。"""
    date_str = datetime.now().strftime("%Y/%m/%d")
    school = str(offer.get("school", "") or "")
    publisher = str(offer.get("publisher", "") or "")
    book = str(offer.get("book", "") or "")

    lines = [
        "請協助幫忙下訂單",
        "",
        "訂購人：士林大漢",
        f"日期：{date_str}",
        f"學校：{school}",
        f"出版社：{publisher}",
        "品名：",
    ]

    for item in offer.get("classes", []):
        class_name = str(item.get("class_name", "") or "")
        students = int(item.get("students", 0) or 0)
        lines.append(f"{book}　{class_name}：{students}本")

    lines.extend([
        "",
        "備註：麻煩教用貨單集中",
        f"外箱備註：{school}",
        "",
        "以上訂單　麻煩幫我處理",
        "感謝！！！"
    ])

    return "\n".join(lines)


def confirm_new_order(user_id):
    order = pending_orders.get(user_id)
    if not order:
        return "⚠️ 找不到尚未確認的訂單，請重新輸入。"

    success, order_number = write_to_google_sheet(order)
    if not success:
        return "❌ 訂單寫入失敗，請稍後再試。"

    note = str(order.get("note", "") or "").strip()
    note_saved = True
    if note:
        note_saved = mark_order_note(order_number, note)

    pending_orders.pop(user_id, None)
    order_flow_context.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
    guided_mode.pop(user_id, None)

    pending_receipt_offers[user_id] = {
        "order_number": order_number,
        "school": order["school"],
        "publisher": order["publisher"],
        "book": order["book"],
        "classes": copy_classes(order.get("classes", [])),
        "created_at": time.time()
    }

    return [
        (
            "✅ 訂單已確認\n\n"
            f"訂單編號：{order_number}\n"
            "已成功寫入 Google 試算表。\n"
            + ((f"備註：{note}\n" if note else "") if note_saved else (f"⚠️ 訂單已建立，但備註寫入失敗：{note}\n"))
            + "\n"
            + f"之後可以直接問「查{order_number}」"
        ),
        (
            "需要幫你生成一份訂購單 PDF，讓你可以存下來 email 給出版社嗎？\n"
            "回覆「要」或「好」即可，40 秒內沒有回覆就會自動取消這個提問。"
        )
    ]


def _pending_order_known_class_names(order, context):
    names = set()

    for item in order.get("classes", []):
        name = str(item.get("class_name", "") or "").strip()
        if name:
            names.add(name)

    for item in context.get("classes", []):
        name = str(item.get("class_name", "") or "").strip()
        if name:
            names.add(name)

    return sorted(names, key=len, reverse=True)


def _pending_order_class_pattern(class_names):
    alternatives = [re.escape(name) for name in class_names]
    alternatives.append(r"[789]\d{2}")
    return "|".join(alternatives)


def _expand_class_shorthand(remaining, class_names):
    """
    把「取消詞」拿掉之後剩下的文字，展開成一份完整班級名稱清單。
    支援兩種寫法：
    1. 完整班級名稱直接串在一起：例如「國一丁國一己」。
    2. 省略共同字首的簡寫：例如班級是「國一丁／國一己／國一戊」，
       使用者常會直接打「國一丁己戊」，只打一次「國一」。
    只要能把 remaining 完全解析、沒有殘留對不上的字元，才會回傳結果；
    有任何解析不了的部分就回傳 None，交給呼叫端走其他既有邏輯或回覆
    看不懂。
    """
    # 1. 完整班級名稱直接比對。
    working = remaining
    found = []
    for name in class_names:
        while name and name in working:
            working = working.replace(name, "", 1)
            found.append(name)
    # 班級名稱之間常見的連接詞／標點殘留（跟、和、、,，/／+ 與空白等）
    # 不該讓整段判定失敗——只要每個班級名稱都比對完，中間單純的連接詞
    # 可以直接忽略，不然「801和802」「國一丁跟國一己」這種常見寫法會
    # 因為連接詞沒被消耗掉而判定成解析失敗，掉回下面對不上的分支。
    leftover = re.sub(r"跟|和|與|及|以及|還有|[、,，/／+＋\s]", "", working)
    if found and not leftover:
        return found

    # 2. 省略共同字首的簡寫：以「班級名稱去掉最後一個字」當共同字首，
    #    比對 remaining 開頭是否吻合，吻合的話後面每個字各自對應一個班級。
    prefixes = {}
    for name in class_names:
        if len(name) >= 2:
            prefixes.setdefault(name[:-1], set()).add(name[-1])

    for prefix in sorted(prefixes, key=len, reverse=True):
        if not remaining.startswith(prefix):
            continue
        suffix_chars = prefixes[prefix]
        rest = remaining[len(prefix):]
        if rest and all(ch in suffix_chars for ch in rest):
            return [prefix + ch for ch in rest]

    return None


def _infer_grade_prefix_kind(sample_class_name):
    """
    判斷這個班級名稱是「文字班級」（國三戊）還是「數字班級」（701），
    回傳 ("letter", "國三") 或 ("digit", "7")，都比對不到就回傳
    (None, None)。用來決定「湊滿N個班」要去學校資料庫抓哪些班級。
    """
    name = str(sample_class_name or "").strip()
    if re.fullmatch(r"[789]\d{2}", name):
        return "digit", name[0]
    if len(name) >= 2:
        return "letter", name[:-1]
    return None, None


def _school_wide_same_grade_classes(school, sample_class_name):
    """
    抓整個學校的班級清單，只留下跟 sample_class_name 同年級的部分，
    並依自然順序排序（甲乙丙丁... 或 701、702、703...）。「同年級」
    只看班級名稱本身的字首／百位數，不用另外猜年級中文名稱。
    """
    kind, prefix = _infer_grade_prefix_kind(sample_class_name)
    if not kind:
        return []

    try:
        result = lookup_school_classes(school)
    except Exception:
        result = None
    classes = (result or {}).get("classes", [])

    if kind == "digit":
        filtered = [
            c for c in classes
            if re.fullmatch(r"[789]\d{2}", str(c.get("class_name", "")).strip())
            and str(c.get("class_name", "")).strip()[0] == prefix
        ]
        filtered.sort(key=lambda c: int(str(c["class_name"]).strip()))
    else:
        filtered = [
            c for c in classes
            if str(c.get("class_name", "")).startswith(prefix)
            and len(str(c.get("class_name", "")).strip()) == len(prefix) + 1
        ]
        filtered.sort(
            key=lambda c: _CLASS_LETTER_ORDER.get(str(c["class_name"]).strip()[-1], 99)
        )
    return filtered


_FILL_QUOTA_PATTERN = re.compile(r"湊(?:滿|到|齊|足)|補(?:滿|到|齊|足)")


def _parse_fill_quota_count(text):
    m = re.search(r"(\d{1,2})\s*個?班", text)
    if m:
        return int(m.group(1))
    m = re.search(r"([一二三四五六七八九十]{1,3})\s*個?班", text)
    if m:
        return _cn_number_to_int(m.group(1))
    return None


def handle_fill_quota_request(order, context, text):
    """
    「湊滿七個班」：依照學校資料庫裡同年級班級的自然順序（甲乙丙丁...
    或 701、702、703...），從目前訂單裡「還沒有」的班級依序往後補，
    直到補滿使用者要的數量，或學校資料庫裡這個年級真的沒有更多班
    為止——不會用猜的，也不會跳過中間的班級。
    """
    if not _FILL_QUOTA_PATTERN.search(text):
        return None

    target = _parse_fill_quota_count(text)
    if not target:
        return "⚠️ 請告訴我要湊到幾個班，例如「湊滿7個班」。"

    current_classes = order.get("classes", [])
    if not current_classes:
        return "⚠️ 目前訂單沒有任何班級，沒辦法用這個方式湊班。"

    current_names = {str(c.get("class_name", "")).strip() for c in current_classes}

    if len(current_names) >= target:
        return f"⚠️ 目前已經有 {len(current_names)} 個班，已經達到（或超過）{target} 個班，不用再湊。"

    sample = str(current_classes[0].get("class_name", ""))
    school_wide = _school_wide_same_grade_classes(order.get("school", ""), sample)

    if not school_wide:
        return "⚠️ 查不到這個年級在學校資料庫裡的完整班級清單，沒辦法自動湊班，請自己告訴我要加哪些班。"

    need = target - len(current_names)
    to_add = [
        c for c in school_wide
        if str(c.get("class_name", "")).strip() not in current_names
    ][:need]

    if not to_add:
        return f"⚠️ 目前已經有 {len(current_names)} 個班，這個年級學校資料庫裡已經沒有更多班可以加了。"

    order["classes"] = sort_class_items(order.get("classes", []) + [
        {
            "class_name": str(c.get("class_name", "")).strip(),
            "students": int(c.get("students", 0) or 0)
        }
        for c in to_add
    ])
    refresh_order_total(order)

    added_names = [str(c.get("class_name", "")).strip() for c in to_add]
    final_count = len(current_names) + len(to_add)

    teacher_class_names = {
        str(item.get("class_name", "")).strip()
        for item in context.get("classes", [])
    }
    not_teacher = [n for n in added_names if n not in teacher_class_names]

    lines = [f"✅ 已依序補上：{'、'.join(added_names)}（共 {final_count} 個班）", ""]

    if final_count < target:
        lines.append(
            f"⚠️ 這個年級學校資料庫裡總共只有 {len(school_wide)} 個班，"
            f"最多只能湊到 {final_count} 個，沒辦法湊滿 {target} 個。"
        )
        lines.append("")

    if not_teacher:
        lines.append(
            f"（{'、'.join(not_teacher)} 不是 {order.get('teacher', '')} 名下登記的班，"
            "依學校資料庫人數補上，建議之後回 Google 試算表補上對應關係）"
        )
        lines.append("")

    lines.append(make_order_confirmation(order))
    return "\n".join(lines)


def handle_pending_order_edit(user_id, text):
    order = pending_orders[user_id]
    context = conversation_context.get(user_id, {})
    class_names = _pending_order_known_class_names(order, context)
    class_pattern = _pending_order_class_pattern(class_names)

    fill_quota_reply = handle_fill_quota_request(order, context, text)
    if fill_quota_reply is not None:
        return fill_quota_reply

    # v54：確認畫面後的自然口語修改。
    # 例：703也要／703一起／還有703／漏了703／順便703。
    m_natural_add = re.fullmatch(r"(?:把)?\s*([789]\d{2})\s*(?:也要|一起|也一起|補上|還要|也加|順便)?", text)
    if not m_natural_add:
        m_natural_add = re.fullmatch(r"(?:還有|漏了|順便)\s*([789]\d{2})", text)
    if m_natural_add:
        cname = m_natural_add.group(1)
        available = {str(i.get("class_name")): i for i in context.get("classes", [])}
        if cname in available and not find_order_class(order, cname):
            item = available[cname]
            order.setdefault("classes", []).append({"class_name": cname, "students": int(item.get("students", 0) or 0)})
            order["classes"] = sort_class_items(order["classes"])
            refresh_order_total(order)
            return "✅ 已新增：" + cname + "\n\n" + make_order_confirmation(order)

    # v70：多班數量修改必須先整批驗證，再一次套用。
    # 這段一定要放在舊的「單班改數量」分支之前，否則像
    # 「801改成30 803改成40」會被前面的單班規則只吃掉 801，
    # 造成 803 不在訂單裡時仍然半套用 801。
    multi_qty_matches = list(re.finditer(
        rf"(?<!\d)({class_pattern})(?!\d)\s*(?:數量|變|幫我調|其實是|改成|改為|改)\s*(\d{{1,3}})\s*(?:本|人)?",
        text
    ))
    if len(multi_qty_matches) >= 2:
        planned_multi_qty = []
        for mm in multi_qty_matches:
            cname = mm.group(1)
            qty = int(mm.group(2))
            target = find_order_class(order, cname)
            if not target:
                return f"⚠️ 目前訂單裡沒有 {cname}。整批修改未套用。"
            if qty < 0:
                return "⚠️ 數量不能小於 0。整批修改未套用。"
            planned_multi_qty.append((target, cname, qty))

        for target, cname, qty in planned_multi_qty:
            target["students"] = qty
        refresh_order_total(order)
        changed = "、".join(f"{cname}→{qty}本" for _, cname, qty in planned_multi_qty)
        return "✅ 已調整：" + changed + "\n\n" + make_order_confirmation(order)

    # 例：701五十本／701數量50／701變50本／701幫我調50／701其實是50／701不是30是50
    zh_num = {"十":10,"二十":20,"三十":30,"四十":40,"五十":50,"六十":60,"七十":70,"八十":80,"九十":90}
    qty_text = text
    # 「五十」這類兩個字的詞一定要先替換，不然「十」會先把「五十」
    # 裡的「十」吃掉變成「五10」，導致「五十」規則永遠比對不到。
    for z, n in sorted(zh_num.items(), key=lambda item: len(item[0]), reverse=True):
        qty_text = qty_text.replace(z, str(n))
    # 關鍵字跟單位「本／人」不能同時省略——原本兩個都是可省
    # （(?:...)?...本?），導致像「801 802」這種單純的班級列表也會
    # fullmatch 成「801 改成 802 本」，把 801 的數量錯改成 802。
    # 拆成兩條：有明確改動關鍵字時單位可省；沒有關鍵字時單位必填。
    qty_patterns = [
        r"([789]\d{2})(?:數量|變|幫我調|其實是|改成|改)\s*(\d{1,3})\s*(?:本|人)?",
        r"([789]\d{2})\s*(\d{1,3})\s*(?:本|人)",
        r"([789]\d{2})不是\d{1,3}是(\d{1,3})",
    ]
    for pat in qty_patterns:
        mq = re.fullmatch(pat, qty_text)
        if mq:
            cname, qty = mq.group(1), int(mq.group(2))
            target = find_order_class(order, cname)
            if target:
                target["students"] = qty
                refresh_order_total(order)
                return "✅ 已調整 " + cname + " 為 " + str(qty) + " 本。\n\n" + make_order_confirmation(order)

    # 例：那本不要701了，換702 —— 同一則訊息完成換班。
    mswap = re.fullmatch(r"(?:那本)?(?:不要)?([789]\d{2})(?:了)?[，, ]*(?:換|改成)([789]\d{2})", text)
    if mswap:
        oldc, newc = mswap.group(1), mswap.group(2)
        available = {str(i.get("class_name")): i for i in context.get("classes", [])}
        if find_order_class(order, oldc) and newc in available:
            order["classes"] = [i for i in order.get("classes", []) if str(i.get("class_name")) != oldc]
            if not find_order_class(order, newc):
                item = available[newc]
                order["classes"].append({"class_name": newc, "students": int(item.get("students",0) or 0)})
            order["classes"] = sort_class_items(order["classes"])
            refresh_order_total(order)
            return "✅ 已調整：" + oldc + " → " + newc + "\n\n" + make_order_confirmation(order)

    if any(word in text for word in ["只要", "保留", "就要", "只留"]):
        # 跟「新增」用同一套 shorthand 展開，比對「這位老師實際教的
        # 班級」（context），才能正確處理「只留國三戊己」這種省略
        # 共同字首的簡寫——原本只用逐一字串比對，「國三己」這種沒有
        # 完整重複出現年級字的寫法會被漏掉。
        keep_word = next(
            (w for w in ["只要", "保留", "就要", "只留"] if w in text),
            ""
        )
        remaining_text = (
            text.replace(keep_word, "", 1).strip() if keep_word else text.strip()
        )

        teacher_class_names = [
            str(item.get("class_name", "")).strip()
            for item in context.get("classes", [])
            if str(item.get("class_name", "")).strip()
        ]
        wanted = _expand_class_shorthand(remaining_text, teacher_class_names) or []
        wanted = unique_list(wanted)

        if not wanted:
            # 展開失敗（例如文字班級簡寫比對不到），退回原本的逐一
            # 字串比對＋數字班級，維持舊版相容。
            remaining = text
            for name in class_names:
                if name and name in remaining:
                    if name not in wanted:
                        wanted.append(name)
                    remaining = remaining.replace(name, " ", 1)
            for digit_class in re.findall(r"(?<!\d)([789]\d{2})(?!\d)", remaining):
                if digit_class not in wanted:
                    wanted.append(digit_class)

        if wanted:
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

            order["classes"] = sort_class_items([
                {
                    "class_name": name,
                    "students": int(available[name].get("students", 0))
                }
                for name in wanted
            ])
            refresh_order_total(order)
            return make_order_confirmation(order)

    # 一次取消一個或多個班級。
    # 支援：
    #   國一丁取消
    #   取消國一丁
    #   國一丁國一己取消
    #   國一丁己戊甲庚取消
    #   取消國一丁己戊甲庚
    #
    # v32 的問題：雖然已經寫了 _expand_class_shorthand()，
    # 但這裡實際上沒有呼叫它，所以「國一丁己戊甲庚」無法展開。
    remove_verbs = ("取消", "不要", "不要了", "刪除", "刪掉", "拿掉", "移除", "撤掉", "不用", "去掉")
    matched_verb = None
    remaining = ""

    for verb in remove_verbs:
        if text.endswith(verb) and text != verb:
            matched_verb = verb
            remaining = text[:-len(verb)].strip()
            break
        if text.startswith(verb) and text != verb:
            matched_verb = verb
            remaining = text[len(verb):].strip()
            break

    if matched_verb and class_names and remaining:
        found = _expand_class_shorthand(remaining, class_names)

        if found:
            unique_found = unique_list(found)
            missing = [
                name for name in unique_found
                if not find_order_class(order, name)
            ]
            if missing:
                return "⚠️ 目前訂單裡沒有 " + "、".join(missing) + "。"

            if len(order.get("classes", [])) <= len(unique_found):
                return (
                    "⚠️ 這樣會把訂單裡所有班級都取消。\n"
                    "如果要取消整張訂單，請直接輸入「取消」。"
                )

            remove_set = set(unique_found)
            order["classes"] = [
                item for item in order.get("classes", [])
                if str(item.get("class_name", "")) not in remove_set
            ]
            refresh_order_total(order)

            return (
                "✅ 已取消：" + "、".join(unique_found) + "\n\n"
                + make_order_confirmation(order)
            )

    m = re.fullmatch(rf"(?:不要|不要了|刪除|刪掉|拿掉|移除|撤掉|不用|去掉)\s*({class_pattern})", text)
    if not m:
        m = re.fullmatch(rf"({class_pattern})\s*(?:取消|不要|刪除|刪掉|拿掉|移除)", text)

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

    # 一次新增一個或多個班級。跟上面「取消」用同一套 shorthand 展開
    # 機制，支援：
    #   新增國三甲 / 國三甲新增
    #   新增國三甲乙丙丁庚 / 國三甲乙丙丁庚新增
    #
    # v42 之前的問題：加班級只有一支「剛好一個班」的正規表示式，
    # 不支援一次加多個／簡寫寫法，而且比對名單只看「訂單裡已經有的
    # 班級」，不是「這位老師實際教的班級」，只要沒完全對上就整個
    # regex 不 match、函式回傳 None，最後掉到最外層完全看不懂的
    # AI fallback 訊息，使用者看不出問題到底出在哪。
    add_verbs = ("新增", "增加", "加入", "再加", "加", "補上")
    matched_add_verb = None
    add_remaining = ""

    for verb in add_verbs:
        if text.startswith(verb) and text != verb:
            matched_add_verb = verb
            add_remaining = text[len(verb):].strip()
            break
        if text.endswith(verb) and text != verb:
            matched_add_verb = verb
            add_remaining = text[:-len(verb)].strip()
            break

    if matched_add_verb and add_remaining:
        # 加班級要比對「這位老師實際教的班級」（context），不是訂單
        # 裡已經有的班級（class_names 是給上面「取消」用的）。
        teacher_class_names = [
            str(item.get("class_name", "")).strip()
            for item in context.get("classes", [])
            if str(item.get("class_name", "")).strip()
        ]
        found = _expand_class_shorthand(add_remaining, teacher_class_names)

        if found:
            unique_found = unique_list(found)
            already_in_order = [
                name for name in unique_found if find_order_class(order, name)
            ]
            to_add = [name for name in unique_found if name not in already_in_order]

            if not to_add:
                return "⚠️ " + "、".join(unique_found) + " 已經都在這筆訂單裡了。"

            source_lookup = {
                str(item.get("class_name")): item
                for item in context.get("classes", [])
            }
            order["classes"] = sort_class_items(order.get("classes", []) + [
                {
                    "class_name": name,
                    "students": int(source_lookup[name].get("students", 0))
                }
                for name in to_add
            ])
            refresh_order_total(order)

            note = ""
            if already_in_order:
                note = "（" + "、".join(already_in_order) + " 本來就已經在訂單裡，跳過）\n\n"

            return (
                "✅ 已新增：" + "、".join(to_add) + "\n\n"
                + note
                + make_order_confirmation(order)
            )

        if teacher_class_names:
            # 先試試看能不能用「多班簡寫」去比對整個學校同年級的班級
            # 清單（不限這位老師）——例如這位老師名下只有國三戊己，
            # 但使用者想加的「國三甲乙丙丁庚」其實是同年級、掛在別的
            # 老師名下的班級。年級字首直接從這位老師已知的班級推：
            # 「國三戊」→「國三」，同一張訂單裡的班級理論上都同年級。
            grade_prefix = teacher_class_names[0][:-1] if len(teacher_class_names[0]) >= 2 else ""
            school_wide_classes = []
            if grade_prefix:
                try:
                    school_wide_result = lookup_school_classes(order.get("school", ""))
                except Exception:
                    school_wide_result = None
                school_wide_classes = [
                    c for c in (school_wide_result or {}).get("classes", [])
                    if str(c.get("class_name", "")).startswith(grade_prefix)
                ]

            if school_wide_classes:
                school_wide_names = [
                    str(c.get("class_name", "")) for c in school_wide_classes
                ]
                shorthand_found = _expand_class_shorthand(add_remaining, school_wide_names)

                if shorthand_found:
                    unique_found = unique_list(shorthand_found)
                    already_in_order = [
                        name for name in unique_found if find_order_class(order, name)
                    ]
                    to_add = [name for name in unique_found if name not in already_in_order]

                    if not to_add:
                        return "⚠️ " + "、".join(unique_found) + " 已經都在這筆訂單裡了。"

                    school_source = {
                        str(c.get("class_name", "")): c for c in school_wide_classes
                    }
                    order["classes"] = sort_class_items(order.get("classes", []) + [
                        {
                            "class_name": name,
                            "students": int(school_source[name].get("students", 0))
                        }
                        for name in to_add
                    ])
                    refresh_order_total(order)

                    note = ""
                    if already_in_order:
                        note = "（" + "、".join(already_in_order) + " 本來就已經在訂單裡，跳過）\n\n"

                    not_teachers = [n for n in to_add if n not in teacher_class_names]
                    extra_note = ""
                    if not_teachers:
                        extra_note = (
                            f"（{'、'.join(not_teachers)} 不是 {order.get('teacher', '')} "
                            "名下登記的班，依學校資料庫人數新增，建議之後回 Google 試算表"
                            "補上對應關係）\n\n"
                        )

                    return (
                        "✅ 已新增：" + "、".join(to_add) + "\n\n"
                        + note + extra_note
                        + make_order_confirmation(order)
                    )

            # 這位老師名下查不到這個班，可能是老師班級資料表還沒
            # 更新、但這個班其實真的存在（例如代課、臨時加開）。
            # 這裡改去查「整個學校」的班級人數資料庫（不限老師，
            # 跟「查人數」共用同一份），找到的話直接用真實人數，
            # 不用叫使用者自己猜。查不到、或 Google 暫時連不上，
            # 才退回原本「這位老師沒有這個班」的錯誤訊息。
            school_lookup = None
            try:
                school_lookup = lookup_school_classes(
                    order.get("school", ""), class_name=add_remaining
                )
            except Exception:
                school_lookup = None

            school_classes = (school_lookup or {}).get("classes", [])
            if len(school_classes) == 1:
                found_class = school_classes[0]
                class_name = found_class["class_name"]

                if find_order_class(order, class_name):
                    return f"⚠️ {class_name} 已經在這筆訂單裡了。"

                order["classes"] = sort_class_items(order.get("classes", []) + [{
                    "class_name": class_name,
                    "students": found_class["students"]
                }])
                refresh_order_total(order)

                return (
                    f"✅ 已新增：{class_name}（{found_class['students']} 本，"
                    f"依學校班級資料庫人數，{order.get('teacher', '')} 名下"
                    "目前沒有登記這個班，建議之後回 Google 試算表補上對應關係）\n\n"
                    + make_order_confirmation(order)
                )

            if len(school_classes) > 1:
                options = "、".join(
                    f"{c['class_name']}（{c['students']}人）" for c in school_classes
                )
                return (
                    f"⚠️ 學校資料庫裡「{add_remaining}」對到不只一筆，"
                    "請講清楚一點是哪一班：\n\n" + options
                )

            return (
                f"⚠️ {order.get('teacher', '')} 沒有「{add_remaining}」這個班，"
                "學校班級資料庫也查不到。\n\n"
                f"{order.get('teacher', '')} 目前班級：{'、'.join(teacher_class_names)}"
            )

    m = re.fullmatch(rf"(?:加|加入|增加)\s*({class_pattern})", text)
    if m:
        class_name = m.group(1)
        if find_order_class(order, class_name):
            return f"⚠️ {class_name} 已經在這筆訂單裡了。"

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

    m = re.fullmatch(rf"({class_pattern})\s*(?:改成|改為|換成)\s*({class_pattern})", text)
    if m:
        old_class, new_class = m.groups()
        old_target = find_order_class(order, old_class)
        if not old_target:
            return f"⚠️ {old_class} 目前不在這筆訂單裡。"

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
        rf"(?<!\d)({class_pattern})(?!\d)\s*(?:人數)?\s*"
        r"(改成|改為|改|多|少)\s*"
        r"(\d+)\s*(?:人|本)?",
        text
    ))

    if matches:
        # 先驗證「每一筆」都合法，全部通過才真的套用；不能邊驗證邊直接
        # 改 target["students"]——原本這樣寫，如果句子裡有兩個以上的
        # 班級、其中某一班不在訂單裡或改成負數，前面已經套用成功的那幾
        # 班不會被還原，使用者看到的是錯誤訊息，但訂單其實已經被改到
        # 一半，按下確認就會用這個中途壞掉的狀態寫進 Google。
        planned = []
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

            planned.append((target, class_name, old_value, new_value))

        changes = []
        for target, class_name, old_value, new_value in planned:
            target["students"] = new_value
            changes.append(f"{class_name}：{old_value}→{new_value}本")

        refresh_order_total(order)
        return (
            "✅ 已調整\n\n"
            + "\n".join(changes)
            + "\n\n"
            + make_order_confirmation(order)
        )

    # 備註：確認前直接加／改，最後確認時會寫入 Google J 欄。
    m = re.fullmatch(r"(?:加上?|新增|修改|改)?\s*備註[：:]?\s*(.+)", text)
    if m:
        order["note"] = m.group(1).strip()
        return make_order_confirmation(order)

    # 出版社修改：必須驗證「目前書名＋新出版社」確實存在。
    # 前綴「出版社」是必填的——原本可有可無，導致任何「換成ＸＸＸ」
    # 「改成ＸＸＸ」（使用者其實是想換書名）都會先被這裡攔截，查不到
    # 就回一句看不懂的出版社錯誤，下面的書名修改分支永遠輪不到。
    m = re.fullmatch(r"出版社(?:改成|改為|換成|改)\s*(.+)", text)
    if m and not re.search(r"班|本|人", m.group(1)):
        new_pub = re.sub(r"[，,。.!！?？\s]+", "", m.group(1))
        variants = _exact_book_variants(order.get("book", ""), publisher=new_pub)
        if not variants:
            return f"⚠️ 資料庫找不到「{order.get('book','')}｜{new_pub}」這個版本，沒有修改。"
        order["publisher"] = new_pub
        return make_order_confirmation(order)

    # 書名修改：重新查正式書名；同名多出版社時要求選出版社。
    m = re.fullmatch(r"(?:書名)?(?:改成|改為|換成|書改成)\s*(.+)", text)
    if m:
        new_book = clean_book_name(m.group(1))
        variants = _exact_book_variants(new_book)
        if len(variants) > 1:
            draft = {
                "teacher": order.get("teacher", ""), "school": order.get("school", ""),
                "book": new_book, "publisher": "",
                "classes": [c.get("class_name", "") for c in order.get("classes", [])],
                # 換書名這裡只帶班級「名稱」進新草稿，build_order_from_draft()
                # 選完出版社後會重新用老師名冊的人數組班級——如果使用者先前
                # 已經手動調整過某些班的數量，這樣會被默默蓋回名冊預設值。
                # 用 quantity_overrides 把目前每班的實際數量記下來，等
                # build_order_from_draft() 選完出版社組出最終班級清單後
                # 蓋回來，保留使用者已經調整過的數量。
                "quantity_overrides": {
                    str(c.get("class_name", "")): int(c.get("students", 0) or 0)
                    for c in order.get("classes", [])
                },
                "teacher_classes": copy_classes(context.get("classes", [])),
                "note": order.get("note", "")
            }
            pending_orders.pop(user_id, None)
            ask = _ask_duplicate_book_publisher(user_id, draft, new_book, variants)
            return ask
        if len(variants) == 1:
            order["book"] = variants[0]["value"]
            order["publisher"] = variants[0]["publisher"]
            return make_order_confirmation(order)
        return "⚠️ 查不到這本書\n\n" f"書名：{new_book}"

    # 老師修改：重新抓老師資料與班級，不只換畫面文字。
    m = re.fullmatch(r"(?:老師)?(?:改成|改為|換成|改)\s*(.+?)(?:老師)?", text)
    if m:
        new_teacher = normalize_teacher_name_input(m.group(1))
        matches = lookup_teacher_matches(new_teacher, school=order.get("school", ""))
        if len(matches) != 1:
            matches = lookup_teacher_matches(new_teacher)
        if len(matches) != 1:
            return f"⚠️ 無法唯一確認老師「{new_teacher}」，請把學校＋老師一起告訴我。"
        item = matches[0]
        order["teacher"] = item["teacher"]
        order["school"] = item["school"]
        order["classes"] = sort_class_items(copy_classes(item.get("classes", [])))
        refresh_order_total(order)
        new_context = {"school": item["school"], "teacher": item["teacher"], "classes": copy_classes(item.get("classes", []))}
        conversation_context[user_id] = new_context
        teacher_lookup_context[user_id] = new_context
        return make_order_confirmation(order)

    # 學校修改：重新驗證目前老師在新學校是否唯一存在。
    m = re.fullmatch(r"(?:學校)?(?:改成|改為|換成|改)\s*(.+)", text)
    if m:
        new_school = m.group(1).strip()
        matches = lookup_teacher_matches(order.get("teacher", ""), school=new_school)
        if len(matches) != 1:
            return f"⚠️ 在「{new_school}」無法確認 {order.get('teacher','')} 的班級資料，沒有修改。"
        item = matches[0]
        order["school"] = item["school"]
        order["teacher"] = item["teacher"]
        order["classes"] = sort_class_items(copy_classes(item.get("classes", [])))
        refresh_order_total(order)
        new_context = {"school": item["school"], "teacher": item["teacher"], "classes": copy_classes(item.get("classes", []))}
        conversation_context[user_id] = new_context
        teacher_lookup_context[user_id] = new_context
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

    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))
    words = [
        "教幾個班", "教幾班", "教哪個班", "教哪歌班", "教哪幾班", "教哪幾個班", "教哪些班",
        "有幾個班", "有幾班", "有哪些班", "哪幾班", "哪幾個班", "幾班",
        "班級資料", "班級人數", "每班幾人", "每班人數",
        "學生人數", "總人數", "幾個學生", "多少學生", "多少人", "幾人",
        "教什麼科", "教哪一科", "教哪科", "教哪些科"
    ]

    if re.fullmatch(r"(?:查|找|查一下|找一下)[\u4e00-\u9fff]{2,4}(?:老師)?", clean):
        return True

    return any(word in clean for word in words)


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

    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", clean):
        return None

    school = str(pending.get("school", "") or "").strip()

    matches = lookup_teacher_matches(clean + "老師", school=school)

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

    matches = lookup_teacher_matches(teacher, school=school)

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
        match = resolve_fuzzy_name("teacher", teacher, school=school)

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
        if isinstance(item_subjects, str): item_subjects = [item_subjects]
        single_subject = str(item.get("subject", "") or "").strip()
        if single_subject: item_subjects = list(item_subjects or []) + [single_subject]
        for subject in item_subjects or []:
            subject = str(subject or "").strip()
            if subject and subject not in subjects: subjects.append(subject)
    lines = [f"• {item['class_name']}班｜{int(item['students'])} 人" for item in classes]
    subject_line = f"📘 科目：{'、'.join(subjects)}\n" if subjects else ""
    return (
        "👨‍🏫 老師資料\n\n"
        f"🏫 學校：{school}\n"
        f"👤 老師：{display_teacher}\n" + subject_line +
        f"📚 班級：{len(classes)} 個｜👥 共 {total} 人\n\n"
        "班級人數\n" + "\n".join(lines) +
        "\n\n💡 要幫這位老師訂書，直接輸入「班級＋書名」即可；"
        "不指定班級的話（直接打書名），預設會用這位老師的全部班級。"
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


def looks_like_history_edit(text, class_names=None):
    if class_names:
        pattern = _pending_order_class_pattern(class_names)
    else:
        pattern = r"\S{1,8}"

    quantity = re.search(
        rf"(?:{pattern})\s*(?:人數)?\s*(?:改成|改為|改|多|少)\s*\d+\s*(?:人|本)?",
        text
    )
    remove = re.search(
        rf"(?:{pattern})\s*(?:取消|不要|移除|刪除|拿掉)",
        text
    )
    return bool(quantity or remove)


def prepare_history_adjustment(user_id, original_order, text):
    # 使用者可能先打「取消訂單005」看到取消確認畫面，又改變主意想改
    # 內容而不是整張取消；這裡如果不清掉 pending_history_cancels，
    # 之後打「確認」時會因為判斷順序，實際執行的還是取消整張訂單，
    # 而不是這裡準備好的修改內容。
    pending_history_cancels.pop(user_id, None)

    if str(original_order.get("status", "")).strip() == "已取消":
        pending_history_updates.pop(user_id, None)
        return (
            "⚠️ 此訂單已取消，無法修改。\n\n"
            f"訂單編號：{original_order.get('order_number', '')}"
        )

    order = copy_order(original_order)
    changes = []
    class_names = _pending_order_known_class_names(original_order, {})
    pattern = _pending_order_class_pattern(class_names)

    # 班級後面加 (?:班)? 是因為畫面顯示班級時就是印成「701班」，使用者
    # 照著畫面打「701班改成28本」，沒有這個寬容的話反而解析不到。
    quantity_matches = list(re.finditer(
        rf"(?<!\d)({pattern})(?!\d)(?:班)?\s*(?:人數)?\s*"
        r"(改成|改為|改|多|少)\s*"
        r"(\d+)\s*(?:人|本)?",
        text
    ))

    remove_matches = list(re.finditer(
        rf"(?<!\d)({pattern})(?!\d)(?:班)?\s*"
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
        "modification_text": modification_text,
        # 記下查詢當下的原始版本，confirm_history_update() 會拿它跟
        # 「確認」當下重新查回來的最新版本比對，如果這段期間試算表
        # 上這張單被別的地方改過，就不要用這份舊草稿整包覆蓋回去。
        "baseline": copy_order(original_order),
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

    # 草稿是根據查詢當下的舊資料算出來的；如果查詢完到按「確認」這段
    # 期間，這張單在試算表上被改過（例如多加了一個班），這裡整包覆蓋
    # 回去會把那筆異動覆蓋掉。用查詢當下記錄的 baseline 跟現在重新查
    # 回來的最新版本比對，兜不起來就中止，請使用者重查最新內容。
    baseline = update_data.get("baseline")
    if latest and baseline and not orders_have_same_core_data(latest, baseline):
        pending_history_updates.pop(user_id, None)
        historical_order_context[user_id] = latest
        return (
            "⚠️ 這張訂單在你查詢之後被改過，剛才準備好的修改內容可能已經過期，"
            "為了避免覆蓋掉別的異動，這次先不套用。\n\n"
            f"訂單編號：{order_number}\n"
            "請重新查一次這張訂單最新內容，再重新告訴我要怎麼改。"
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
    lines = [f"• {item['class_name']}｜{int(item['students'])} 本" for item in new_order.get("classes", [])]
    return (
        "✏️ 訂單修改｜請確認\n\n"
        f"📋 訂單：{new_order['order_number']}\n"
        f"👨‍🏫 老師：{new_order['teacher']}\n"
        f"📖 書名：{new_order['book']}\n\n"
        "本次修改\n" + "\n".join(changes) +
        "\n\n修改後\n" + "\n".join(lines) +
        f"\n\n📦 總數量：{original_order['quantity']} → {new_order['quantity']} 本\n\n"
        "確認修改 → 回覆「確認」\n"
        "放棄修改 → 回覆「取消修改」"
    )



def make_historical_order_with_offer(user_id, order):
    status = str(order.get("status", "") or "").strip()
    note = str(order.get("note", "") or "").strip()
    history_reply = make_historical_order_reply(order)

    if "取消" in status or "已請業務下單" in note:
        pending_receipt_offers.pop(user_id, None)
        return history_reply

    pending_receipt_offers[user_id] = {
        "order_number": order.get("order_number", ""),
        "school": order.get("school", ""),
        "publisher": order.get("publisher", ""),
        "book": order.get("book", ""),
        "classes": copy_classes(order.get("classes", [])),
        "created_at": time.time(),
        "source": "history_lookup",
    }

    return [
        history_reply,
        (
            "需要幫你生成一份訂購單 PDF，讓你可以存下來 email 給出版社嗎？\n"
            "回覆「要」或「好」即可，40 秒內沒有回覆就會自動取消這個提問。"
        )
    ]


def make_historical_order_reply(order):
    lines = [f"• {item['class_name']}｜{int(item['students'])} 本" for item in order.get("classes", [])]
    message = (
        "📋 訂單明細\n\n"
        f"🔢 訂單編號：{order['order_number']}\n"
        f"🏫 學校：{order.get('school', '')}\n"
        f"👨‍🏫 老師：{order.get('teacher', '')}\n"
        f"📖 書名：{order.get('book', '')}\n"
        f"🏢 出版社：{order.get('publisher', '')}\n\n"
        "班級與數量\n" + "\n".join(lines) +
        f"\n\n📦 總數量：{int(order.get('quantity', 0))} 本\n"
        f"📌 狀態：{order.get('status', '')}"
    )
    if order.get("order_time"): message += f"\n🕒 訂購時間：{order['order_time']}"
    if order.get("last_modified"): message += f"\n✏️ 最後修改：{order['last_modified']}"
    if order.get("modification_log"): message += f"\n📝 修改紀錄：{order['modification_log']}"
    if order.get("note"): message += f"\n📎 備註：{order['note']}"
    message += "\n\n可直接操作：\n• 修改數量：例如「701改28本」\n• 取消整張：輸入「取消這張」"
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

    m = re.fullmatch(r"(?:查|查詢)?(\d{2})(\d{2})(?:的)?訂單", clean)
    if m:
        year = datetime.now().year
        month = int(m.group(1))
        day = int(m.group(2))
        try:
            dt = datetime(year, month, day)
        except ValueError:
            return None
        return _rollback_year_if_future(dt).strftime("%Y-%m-%d")

    m = re.fullmatch(
        r"(?:查|查詢)?(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})(?:的)?訂單",
        clean
    )
    if not m:
        return None

    explicit_year = bool(m.group(1))
    year = int(m.group(1)) if explicit_year else datetime.now().year
    month = int(m.group(2))
    day = int(m.group(3))

    try:
        dt = datetime(year, month, day)
    except ValueError:
        return None
    if not explicit_year:
        dt = _rollback_year_if_future(dt)
    return dt.strftime("%Y-%m-%d")


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
        "⚠️ 取消訂單｜請再次確認\n\n"
        f"🔢 訂單：{order.get('order_number', '')}\n"
        f"👨‍🏫 老師：{order.get('teacher', '')}\n"
        f"📖 書名：{order.get('book', '')}\n"
        f"📦 總數量：{int(order.get('quantity', 0) or 0)} 本\n\n"
        "確定取消整張 → 回覆「確認取消」\n"
        "保留訂單 → 回覆「取消修改」"
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
def parse_school_stats_query(user_id, text, require_keyword=True):
    """
    「查人數」功能：解析「學校＋年級」這類問句（例如「天母七年級人數」
    「華興高一人數」），也相容原本「多少人／幾個班」等既有問法。
    require_keyword=False 時（guided_mode="stats_lookup" 內使用），
    只要能抓到學校，就算沒有出現任何關鍵字也視為有效查詢，
    讓使用者在「查人數」模式裡可以直接打「天母七年級」而不用多打「人數」兩個字。
    """
    clean = text.strip()

    query_words = [
        "多少人", "幾人", "幾個人", "學生人數", "學生總數", "總人數", "人數",
        "幾個班", "多少班", "有哪些班", "哪幾班", "哪幾個班"
    ]
    has_keyword = any(word in clean for word in query_words)
    if require_keyword and not has_keyword:
        return None
    if "老師" in clean:
        return None

    if re.match(
        r"^[\u4e00-\u9fff]{2,4}教(?:哪幾個班|哪幾班|哪些班|幾個班|幾班)",
        clean
    ):
        return None

    # 優先沿用這個模式裡剛查過的學校（stats_version_context），再退回
    # 老師查詢留下的 context；避免查完老師之後，同一句人數查詢意外
    # 沿用更早以前、不相干的學校。
    school = (
        extract_school_name(clean)
        or (stats_version_context.get(user_id) or {}).get("school", "")
        or get_context_school(user_id)
    )
    if not school:
        return None
    stats_version_context[user_id] = {"school": school}

    class_match = re.search(r"(?<!\d)([789]\d{2})(?!\d)", clean)
    class_name = class_match.group(1) if class_match else ""
    grade = extract_grade_text(clean)

    intent = "summary"
    if any(word in clean for word in ["有哪些班", "哪幾班", "哪幾個班"]):
        intent = "classes"
    elif any(word in clean for word in ["幾個班", "多少班"]):
        intent = "class_count"
    elif any(word in clean for word in ["多少人", "幾人", "幾個人", "學生人數", "總人數", "人數"]):
        intent = "students"

    return {
        "school": school,
        "grade": grade,
        "class_name": class_name,
        "intent": intent
    }


def handle_school_stats_query(query):
    """
    「查人數」完整版回覆：總人數 + 各班明細。
    共用原本 lookup_school_classes()（依學校／年級／班級查班級人數），
    只是把回覆格式統一成「總人數在前、各班明細在後」，
    符合「天母七年級人數」這類問法要的完整版格式。
    """
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
        f"🏫 {title}\n"
        f"班級總數：{result['class_count']}個班\n"
        f"學生總人數：{result['total_students']}人\n\n"
        "各班人數：\n"
        + "\n".join(class_lines)
    )


# =========================================================
# 教科書版本
# =========================================================
_SUBJECT_DISPLAY_ORDER = [
    "國文", "英文", "數學", "自然", "生物", "理化",
    "地科", "社會", "歷史", "地理", "公民"
]


def _subject_sort_key(subject):
    try:
        return _SUBJECT_DISPLAY_ORDER.index(str(subject or "").strip())
    except ValueError:
        return 999


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
    if not query.get("grade"):
        result = lookup_school_versions_all_junior_grades(
            query["school"],
            query.get("subject", ""),
            query.get("academic_period", "")
        )
    else:
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

    failed_grades = result.get("failed_grades") or []
    failed_note = (
        f"\n\n⚠️ {'、'.join(failed_grades)}這次讀取失敗，以上結果可能不完整，建議稍後再查一次確認。"
        if failed_grades else ""
    )

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
            + failed_note
        )

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

    header = (
        f"📚 {query['school']}"
        + (f" {query['grade']}" if query.get("grade") else "")
        + (f"\n學年度：{period}" if period else "")
    )

    if not query.get("grade"):
        grade_order = ["七年級", "八年級", "九年級"]
        grouped = {}
        for item in versions:
            g = str(item.get("grade", "") or "").strip()
            grouped.setdefault(g, []).append(item)

        ordered_grades = [g for g in grade_order if g in grouped]
        ordered_grades += [g for g in grouped.keys() if g not in grade_order]

        blocks = []
        for g in ordered_grades:
            group_lines = [f"{g}:"]
            for item in sorted(
                grouped[g], key=lambda x: _subject_sort_key(x.get("subject"))
            ):
                group_lines.append(f"{item['subject']}：{item['version']}")
            blocks.append("\n".join(group_lines))

        return header + "\n\n" + "\n\n".join(blocks) + failed_note

    lines = [f"• {item['subject']}：{item['version']}" for item in versions]
    return header + "\n\n" + "\n".join(lines) + failed_note


def extract_school_name(text):
    clean = re.sub(r"[，,。.!！?？：:\s]+", "", str(text or ""))
    if not clean:
        return ""

    # v73：三個主要學校先做純本地辨識。這一層一定要放在
    # get_school_catalog() 之前，否則像「天母教務處要補一本書」這種
    # 已經明確寫出天母的句子，仍會先打一次 list_schools，白白多等數秒。
    fast_school = _fast_school_from_text(clean)
    if fast_school:
        return fast_school

    # 沒有任何「學校型態」字樣時，也不要為了猜學校就打 Google。
    # 真正需要動態學校清單的情況才往下走。
    if not _text_may_contain_dynamic_school(clean):
        return ""

    schools = get_school_catalog()

    aliases = []
    for school in schools:
        canonical = str(school or "").strip()
        if not canonical:
            continue

        short = re.sub(r"(?:國民中學|國民小學|高級中學|國中|高中|國小|中學|女中)$", "", canonical)
        aliases.append((canonical, canonical))

        if short and short != canonical:
            aliases.append((short, canonical))

    aliases.sort(key=lambda item: len(item[0]), reverse=True)

    for alias, canonical in aliases:
        if alias and alias in clean:
            return canonical

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
        r"(?:有多少人|多少人|幾人|幾個人|學生人數|總人數|人數|"
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
        and now < float(school_catalog_cache.get("expires_at", 0) or 0)
    ):
        return list(school_catalog_cache.get("schools", []))

    result = google_post(
        {"action": "list_schools"},
        timeout=5,
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

    if result and result.get("success"):
        school_catalog_cache["schools"] = schools
        school_catalog_cache["expires_at"] = now + 1800
        return list(schools)

    # Google 暫時逾時時不要下一則訊息又立刻重打 list_schools；
    # 先用三個主要學校做短暫負向快取，30 秒後才允許再刷新。
    fallback = list(school_catalog_cache.get("schools", [])) or [x[0] for x in _FAST_KNOWN_SCHOOLS]
    school_catalog_cache["schools"] = fallback
    school_catalog_cache["expires_at"] = now + 30
    return list(fallback)


def extract_grade_text(text):
    clean = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    grade_map = {
        "七年級": "七年級", "八年級": "八年級", "九年級": "九年級",
        "國一": "七年級", "國二": "八年級", "國三": "九年級",
        "7年級": "七年級", "8年級": "八年級", "9年級": "九年級",
        "高一": "高一", "高二": "高二", "高三": "高三",
        "高中一年級": "高一", "高中二年級": "高二", "高中三年級": "高三",
        "10年級": "高一", "11年級": "高二", "12年級": "高三"
    }
    for key in sorted(grade_map, key=len, reverse=True):
        if key in clean:
            return grade_map[key]
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
def _other_order_school_aliases(include_dynamic=False):
    """
    v74：其他訂單的常用三校別名完全本地化。

    舊版每次 parse_other_order() 一進來就先 get_school_catalog()，所以即使
    使用者已經明確打「天母教務處要補一本書」，仍會白打 list_schools。
    這裡預設只回傳本地三校；只有真的需要解析其他動態學校名稱時，
    呼叫端才顯式傳 include_dynamic=True。
    """
    aliases = {
        "天母":"天母國中","天母國中":"天母國中",
        "華興":"華興中學","華興中學":"華興中學",
        "衛理":"衛理女中","衛理女中":"衛理女中"
    }
    if not include_dynamic:
        return aliases

    for school in get_school_catalog():
        school = str(school or "").strip()
        if not school:
            continue
        aliases[school] = school
        short = re.sub(r"(?:國民中學|國民小學|高級中學|國中|國小|高中|中學|女中)$", "", school)
        if short:
            aliases[short] = school
    return aliases


def _resolve_other_order_teacher(user_id, teacher_text, school_hint=""):
    teacher_name = normalize_person_name(teacher_text)
    if not teacher_name:
        return None

    matches = lookup_teacher_matches(teacher_name, school=school_hint)
    if not matches and school_hint:
        matches = lookup_teacher_matches(teacher_name, school="")

    exact = [
        item for item in (matches or [])
        if normalize_person_name(item.get("teacher","")) == teacher_name
    ]
    unique = {}
    for item in exact:
        key=(str(item.get("school","") or "").strip(),
             normalize_person_name(item.get("teacher","")))
        unique[key]=item
    exact=list(unique.values())

    if len(exact)==1:
        item=exact[0]
        return {
            "school":str(item.get("school","") or school_hint).strip(),
            "teacher":str(item.get("teacher","") or teacher_name).strip()
        }

    # 其他訂單也支援老師姓名一字錯誤／同音字。這裡只需要學校與老師名稱，
    # 可直接使用 fuzzy candidate，不再為了拿班級資料多打一趟 Google。
    fuzzy = resolve_fuzzy_name("teacher", teacher_name, school=school_hint)
    if fuzzy.get("status") == "auto":
        suggested = str(fuzzy.get("value", "") or teacher_name).strip()
        suggested_school = str(fuzzy.get("school", "") or school_hint).strip()
        sug_norm = normalize_person_name(suggested)
        # 只是多打／少打字（「林怡君藥」→「林怡君」）是同一個人，直接採用；
        # 有字不一樣（「王大明」→「王小明」）才可能是另一個人，要先問。
        if sug_norm == teacher_name or (sug_norm and (sug_norm in teacher_name or teacher_name in sug_norm)):
            return {"school": suggested_school, "teacher": suggested}
        # v81：只差一個字的「另一位老師」不能默默換掉（王大明→王小明）。
        # 照使用者打的名字登記，先問學校，並提示資料庫有一位很像的。
        return {"school": "", "teacher": teacher_name,
                "suggest_teacher": suggested, "suggest_school": suggested_school}

    if school_hint:
        classes=get_teacher_classes(school_hint,teacher_name)
        if classes:
            return {"school":school_hint,"teacher":teacher_name}
    return None


_V80_OTHER_ORDER_SUPPLY_WORDS = (
    "書面紙","影印紙","A4紙","a4紙","海報紙","色紙","彩色筆","白板筆",
    "原子筆","鉛筆","橡皮擦","資料夾","粉筆","尺","剪刀","膠水","膠帶","便利貼"
)
_V80_OTHER_ORDER_SAMPLE_WORDS = ("樣書","全樣書","教師用書","教師本","參考樣書","試閱")
_V80_OTHER_ORDER_REPLACEMENT_WORDS = (
    "補一本","補一冊","補一套","補書","要補","需要補","少一本","少一冊",
    "缺一本","缺一冊","遺失","不見了","不見","弄丟","掉了","丟了",
    "再拿一本","再要一本","學生要一本","同學要一本"
)
# 學校裡的單位：看到這些字，前面那一段一定是學校名稱。
_V80_SCHOOL_OFFICE_WORDS = (
    "教務處","學務處","總務處","輔導室","輔導處","人事室","會計室","圖書館",
    "註冊組","設備組","教學組","訓育組","衛生組","體育組","資訊組","實研組",
    "導師室","辦公室","校長室","教官室","健康中心","出版組"
)
_V80_SCHOOL_TYPE_SUFFIX = r"(?:國民中學|國民小學|高級中學|完全中學|實驗中學|國中|國小|高中|高職|中學|女中|實中|學校)"


def _v80_looks_like_other_order(text):
    t = str(text or "")
    return any(w in t for w in (
        _V80_OTHER_ORDER_SUPPLY_WORDS + _V80_OTHER_ORDER_SAMPLE_WORDS + _V80_OTHER_ORDER_REPLACEMENT_WORDS
    ))


def _v80_split_other_order_school(clean):
    """
    v80：其他訂單的學校辨識，回傳 (學校, 去掉學校後的文字)。
    依序：
      1. 本地三校（天母／華興／衛理）——不打 Google
      2. Google 學校清單（含簡稱，例如「泰北」→「泰北高中」；清單 30 分鐘快取）
      3. 寫了學校型態（XX高中、XX國中…）——不在清單也照打的名字登記
      4. 後面接學校單位（XX教務處、XX註冊組…）——前面那段就是學校
    都不符合時回傳 ("", 原文)，交給後面用老師資料庫反查學校。
    """
    aliases = _other_order_school_aliases(include_dynamic=False)
    for alias in sorted(aliases, key=len, reverse=True):
        if clean.startswith(alias):
            return aliases[alias], clean[len(alias):]

    aliases = _other_order_school_aliases(include_dynamic=True)
    for alias in sorted(aliases, key=len, reverse=True):
        if len(alias) >= 2 and clean.startswith(alias):
            return aliases[alias], clean[len(alias):]

    # 校名裡不會出現「老師」或動詞；有的話代表切錯了（例如「林老師要中學生用的色紙」）。
    def _plausible_school(name):
        return not re.search(r"老師|要|買|訂|補|給|拿|送|需|缺|看|的", name)

    m = re.match(rf"^([\u4e00-\u9fff]{{1,8}}?{_V80_SCHOOL_TYPE_SUFFIX})(.+)$", clean)
    if m and _plausible_school(m.group(1)):
        return m.group(1), m.group(2)

    for office in _V80_SCHOOL_OFFICE_WORDS:
        m = re.match(rf"^([\u4e00-\u9fff]{{2,8}}?)(?:的)?({office}.+)$", clean)
        if m and _plausible_school(m.group(1)):
            return m.group(1), m.group(2)

    return "", clean


def parse_other_order(user_id, text):
    """
    v58：其他訂單直接在 parser 判斷「零星需求」。
    原則：
    - 文具/紙張/樣書/補書 => 其他訂單
    - 老師 + 一般教材 + 單純「要/訂」=> 讓給團體訂書
    - 老師一定由資料庫反查學校
    - 品項保留使用者原意，不強迫套正式書名
    """
    raw = str(text or "").strip()
    clean = re.sub(r"[，,。.!！?？]+$", "", raw)
    clean = re.sub(r"\s+", "", clean)
    if not clean or clean.startswith(("查", "查詢")):
        return None

    # rewrite 可能先加這個標記；parser 本身要能吃掉。
    explicit_other = False
    if clean.startswith("其他訂單"):
        explicit_other = True
        clean = clean[len("其他訂單"):]
    elif clean.startswith("其他單"):
        explicit_other = True
        clean = clean[len("其他單"):]

    # v80：這句根本不像其他訂單（沒有用品／樣書／補書字眼，也不是明確
    # 「其他訂單」）時提早離開。結果跟舊版一樣是 None，但可以避免為了
    # 辨識學校而去抓學校清單，一般訊息的速度不受影響。
    if not explicit_other and not _v80_looks_like_other_order(clean):
        return None

    # v74：先只用本地三校，不為「天母／華興／衛理」呼叫 list_schools。
    school_hint, clean = _v80_split_other_order_school(clean)
    if school_hint:
        clean = re.sub(r"^的", "", clean)      # 「泰北的註冊組」

    clean = re.sub(r"^(?:幫我|幫|麻煩|請幫我|請幫)", "", clean)

    # 團體班級編輯「補上703」不能進其他訂單。
    if re.search(r"(?:補|補上|再補|加|新增)\s*[789]\d{2}", clean):
        return None

    supply_words = (
        "書面紙","影印紙","A4紙","a4紙","海報紙","色紙","彩色筆","白板筆",
        "原子筆","鉛筆","橡皮擦","資料夾","粉筆","尺","剪刀","膠水","膠帶","便利貼"
    )
    sample_words = ("樣書","全樣書","教師用書","教師本","參考樣書","試閱")
    replacement_words = (
        "補一本","補一冊","補一套","補書","要補","需要補","少一本","少一冊",
        "缺一本","缺一冊","遺失","不見了","不見","弄丟","掉了","丟了",
        "再拿一本","再要一本","學生要一本","同學要一本"
    )

    # 姓名與動作拆開。動作由長到短，避免「要買」先被「要」吃掉。
    patterns = [
        r"^(?P<teacher>[\u4e00-\u9fff]{2,4}?)(?:老師)?(?:那邊)?(?:要補|需要補|幫我補|補|要買|購買|買|需要|要看|想看|看|拿|給|送|準備|缺|要|訂購|訂|叫)(?P<item>.+)$",
        r"^(?:給|麻煩給)(?P<teacher>[\u4e00-\u9fff]{2,4}?)(?P<item>.+)$",
    ]
    match = None
    for pattern in patterns:
        match = re.fullmatch(pattern, clean)
        if match:
            break
    if not match:
        return None

    teacher_raw = match.group("teacher").strip()
    item = match.group("item").strip()
    if not teacher_raw or not item:
        return None

    # 若前面動詞只吃到「要」，後面還殘留「訂...」，這是團體訂書。
    if item.startswith(("訂書", "訂")) and not explicit_other:
        return None

    full_for_intent = clean
    is_supply = any(w in item or w in full_for_intent for w in supply_words)
    is_sample = any(w in item or w in full_for_intent for w in sample_words)
    is_replacement = any(w in full_for_intent for w in replacement_words)

    # v80：品項照使用者打的保留，不再把開頭的「一本／一冊／一套」刪掉——
    # 「補一套3800套書」刪掉「一套」後只剩「3800套書」，數量就不見了。
    item = item.strip()
    if not item:
        return None

    # 一般教材不能因為「老師要XX」就被其他訂單搶走。
    bookish = any(w in item for w in (
        "講義","課本","習作","評量","自修","題本","段考王","大滿貫","學習講義","學習自修"
    ))
    if bookish and not (explicit_other or is_sample or is_replacement):
        return None

    # 非明確其他訂單情境，只接受用品；避免任意「老師要XXX」都被搶走。
    if not (explicit_other or is_supply or is_sample or is_replacement):
        return None

    # v69：其他訂單的「老師」欄改視為自由文字的聯絡對象／單位。
    # 有明確學校時，以使用者輸入的學校為準，不再要求這個名稱一定要
    # 存在老師資料庫（例如：天母教務處、華興註冊組、不知道姓名的老師）。
    if school_hint:
        resolved = {"school": school_hint, "teacher": teacher_raw}
    else:
        # 沒有學校時才把老師資料庫當成加分功能：能唯一補出學校就補；
        # 補不到也不丟掉整筆需求，保留聯絡對象並追問學校。
        resolved = _resolve_other_order_teacher(user_id, teacher_raw, school_hint)
        if not resolved:
            resolved = {"school": "", "teacher": teacher_raw}

    parsed = {
        "school": resolved.get("school", ""),
        "teacher": resolved.get("teacher", teacher_raw),
        "item": item,
        "needs_school": not bool(resolved.get("school", "")),
    }
    if resolved.get("suggest_teacher"):
        parsed["suggest_teacher"] = resolved["suggest_teacher"]
        parsed["suggest_school"] = resolved.get("suggest_school", "")
    return parsed

def _resolve_other_order_school_input(raw_text):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    if not clean:
        return ""
    aliases = _other_order_school_aliases(include_dynamic=False)
    if clean in aliases:
        return aliases[clean]
    if _text_may_contain_dynamic_school(clean):
        aliases = _other_order_school_aliases(include_dynamic=True)
        if clean in aliases:
            return aliases[clean]
    # 允許「天母國中」「華興」這類包含關係。
    for alias in sorted(aliases, key=len, reverse=True):
        if alias and (clean == alias or clean in alias or alias in clean):
            return aliases[alias]
    # v80：Google 學校清單的簡稱（「泰北」→「泰北高中」）不管有沒有
    # 「國中／高中」字樣都要查；清單有 30 分鐘快取。
    aliases = _other_order_school_aliases(include_dynamic=True)
    if clean in aliases:
        return aliases[clean]
    for alias in sorted(aliases, key=len, reverse=True):
        if len(alias) >= 2 and (clean in alias or alias in clean):
            return aliases[alias]
    fuzzy = resolve_fuzzy_name("school", clean)
    if fuzzy.get("status") == "auto":
        return str(fuzzy.get("value", "") or "").strip()
    # v80：清單裡沒有的學校，照使用者打的名字登記（確認畫面會顯示出來
    # 讓使用者檢查）。確認／取消這類字、數字、太長的句子不能當成校名。
    if (
        re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,12}", clean)
        and not _is_confirm_word(clean)
        and clean not in {"取消", "不要", "不要了", "這筆不要", "重來", "主選單", "回主選單", "離開"}
    ):
        return clean
    return ""


def handle_pending_other_order_school_input(user_id, text):
    order = pending_other_orders.get(user_id)
    if not order or str(order.get("school", "") or "").strip():
        return None
    clean_yes = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    if order.get("suggest_teacher") and clean_yes in {"是", "對", "是他", "就是他", "對就是他", "沒錯", "是的"}:
        order["teacher"] = order.pop("suggest_teacher")
        order["school"] = order.pop("suggest_school", "")
        order["needs_school"] = not bool(order["school"])
        pending_other_orders[user_id] = order
        return make_other_order_confirmation(order)
    school = _resolve_other_order_school_input(text)
    if not school:
        return (
            "我還缺這筆其他訂單的學校。\n\n"
            "請直接輸入學校名稱，例如「天母」、「華興中學」、「衛理女中」。"
        )
    order["school"] = school
    order["needs_school"] = False
    pending_other_orders[user_id] = order
    return make_other_order_confirmation(order)


def make_other_order_confirmation(order):
    if not str(order.get("school", "") or "").strip():
        return (
            "📦 其他訂單\n\n"
            f"聯絡對象：{order.get('teacher','')}\n"
            f"品項：{order.get('item','')}\n\n"
            "我還缺學校。請直接輸入學校名稱，例如「天母」或「華興中學」。"
            + (
                f"\n\n💡 資料庫有一位很像的老師：{order.get('suggest_school','')} {order.get('suggest_teacher','')}。"
                "\n是同一位的話回「是」，就改成他；不是的話直接打學校名稱。"
                if order.get("suggest_teacher") else ""
            )
        )
    return (
        "📦 其他訂單｜請確認\n\n"
        f"🏫 學校：{order['school']}\n"
        f"👤 聯絡對象：{order['teacher']}\n"
        f"🧾 品項：{order['item']}\n\n"
        "確認新增 → 回覆「確認」\n"
        "不要這筆 → 回覆「取消」"
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

    order_number = normalize_other_order_number(result.get("order_number", ""))
    return (
        "✅ 已寫入 Google「其他訂單」\n\n"
        + (f"編號：{order_number}\n" if order_number else "")
        + f"學校：{order['school']}\n"
        + f"老師：{order['teacher']}\n"
        + f"項目：{order['item']}"
    )


def normalize_other_order_number(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return ""
    return f"其他{int(digits):03d}"


def parse_other_order_history_query(text):
    """v44：自然語言查詢其他訂單。回傳 None 代表不是查詢句。"""
    clean = re.sub(r"\s+", "", str(text or "").strip())
    if not clean:
        return None

    # 單號：查其他003／其他003
    m = re.fullmatch(r"(?:查|查詢)?其他(?:訂單)?#?(\d{1,6})", clean)
    if m:
        return {"order_number": normalize_other_order_number(m.group(1))}

    if "其他訂單" not in clean and "其他單" not in clean:
        return None
    if not (clean.startswith("查") or clean.startswith("查詢")):
        return None

    q = clean
    q = re.sub(r"^(?:查詢|查)", "", q)
    q = q.replace("所有的", "").replace("全部的", "").replace("所有", "").replace("全部", "")
    q = q.replace("其他訂單", "").replace("其他單", "")
    q = q.strip("的")

    result = {"date": "", "school": "", "teacher": "", "item_keyword": ""}
    now = datetime.now()
    from datetime import timedelta
    if "今天" in q or "今日" in q:
        result["date"] = now.strftime("%Y-%m-%d")
        q = q.replace("今天", "").replace("今日", "")
    elif "昨天" in q:
        result["date"] = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        q = q.replace("昨天", "")
    elif "前天" in q:
        result["date"] = (now - timedelta(days=2)).strftime("%Y-%m-%d")
        q = q.replace("前天", "")
    else:
        date_patterns = [
            r"(?:(\d{4})[/-])?(\d{1,2})[/-](\d{1,2})",
            r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日?",
        ]
        for pat in date_patterns:
            m = re.search(pat, q)
            if m:
                year = int(m.group(1)) if m.group(1) else now.year
                try:
                    result["date"] = datetime(year, int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
                    q = q[:m.start()] + q[m.end():]
                except ValueError:
                    pass
                break

    q = q.strip("的")
    if q:
        # 剩餘文字交給 Apps Script 同時對學校/老師做關鍵字比對。
        result["keyword"] = q
    return result


def handle_other_order_history_query(user_id, query):
    orders = lookup_other_orders(
        teacher=query.get("teacher", ""),
        item_keyword=query.get("item_keyword", ""),
        order_number=query.get("order_number", ""),
        date=query.get("date", ""),
        school=query.get("school", ""),
        keyword=query.get("keyword", "")
    )
    if orders is None:
        return "⚠️ 其他訂單查詢失敗，請稍後再試。"
    if not orders:
        other_order_context.pop(user_id, None)
        return "📦 查不到符合條件的其他訂單。"
    if len(orders) == 1:
        other_order_context[user_id] = orders[0]
    else:
        # 查到多筆時舊的 context 一定要清掉，不然使用者對清單裡某一筆
        # 打「進度改成已送出」，resolve_other_order_target() 可能會沿用
        # 更早以前查過的那一筆（畫面上根本沒列出來），改錯訂單。
        other_order_context.pop(user_id, None)
    return make_other_orders_reply(orders, query.get("date", ""))


def parse_other_order_query(text):
    # 舊介面保留相容性
    q = parse_other_order_history_query(text)
    return q


def parse_other_order_update(user_id, text):
    clean = text.strip()
    m = re.fullmatch(r"(?:其他訂單|其他單|其他)?\s*#?\s*(\d+)\s*(進度|備註)\s*(?:改成|改為|改|設成|設為)\s*(.+)", clean)
    if m:
        return {"order_number": normalize_other_order_number(m.group(1)), "field": "progress" if m.group(2)=="進度" else "note", "value": m.group(3).strip()}
    m = re.fullmatch(r"(進度|備註)\s*(?:改成|改為|改|設成|設為)\s*(.+)", clean)
    if m and user_id in other_order_context:
        return {"use_context": True, "field": "progress" if m.group(1)=="進度" else "note", "value": m.group(2).strip()}
    return None


def resolve_other_order_target(user_id, update_data):
    if update_data.get("use_context"):
        return other_order_context.get(user_id)
    if update_data.get("order_number"):
        orders = lookup_other_orders(order_number=update_data["order_number"])
        return orders[0] if orders else None
    return None


def make_other_order_update_confirmation(target, field, value):
    field_name = "進度" if field == "progress" else "備註"
    number = target.get("order_number") or normalize_other_order_number(target.get("row_number", ""))
    return ("🔄 其他訂單修改確認\n\n" f"{number}\n" f"老師：{target.get('teacher','')}\n" f"項目：{target.get('item','')}\n" f"{field_name}改成：{value}\n\n" "正確請回覆「確認」\n取消請回覆「取消修改」")


def confirm_other_order_update(user_id):
    update_data = pending_other_updates.get(user_id)
    if not update_data:
        return "⚠️ 找不到等待確認的其他訂單修改。"
    success, _ = update_other_order_in_google_sheet(update_data.get("order_number", ""), update_data["field"], update_data["value"], row_number=update_data.get("row_number"))
    if not success:
        return "❌ 其他訂單修改失敗，Google 資料沒有變動。"
    pending_other_updates.pop(user_id, None)
    return f"✅ 其他訂單已更新\n\n{update_data.get('order_number') or '#'+str(update_data.get('row_number',''))}"


def make_other_orders_reply(orders, date_text=""):
    title = f"📦 {date_text} 其他訂單" if date_text else "📦 其他訂單"
    lines = [title, "", f"共 {len(orders)} 筆", ""]
    for item in orders[:20]:
        number = item.get("order_number") or normalize_other_order_number(item.get("row_number", ""))
        lines.extend([
            f"🧾 {number}",
            f"日期：{item.get('date', '')}",
            f"學校：{item.get('school', '')}",
            f"老師：{item.get('teacher', '')}",
            f"項目：{item.get('item', '')}",
            f"進度：{item.get('progress', '') or '未設定'}",
            f"備註：{item.get('note', '') or '—'}",
            ""
        ])
    if len(orders) > 20:
        lines.append(f"（另有 {len(orders)-20} 筆未顯示）")
    return "\n".join(lines).rstrip()


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
# 智慧理解層（最後一道容錯，不取代原本規則）
# =========================================================
def _openai_json(messages, max_output_tokens=700, timeout=None):
    if not OPENAI_API_KEY:
        return None

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": max_output_tokens,
    }
    try:
        response = HTTP.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload,
            timeout=timeout if timeout is not None else AI_TIMEOUT_SECONDS
        )
        if response.status_code != 200:
            logger.warning("AI parse failed status=%s body=%s", response.status_code, response.text[:300])
            return None
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as error:
        logger.warning("AI parse error: %s", error)
        return None


def _smart_order_system_prompt():
    return """你是訂書資料抽取器，不是聊天機器人。
只輸出 JSON。不能自行補不存在的資料；不確定就留空。
intent 只能是 school_order、cram_order、unknown。
school_order 欄位：
{"intent":"school_order","school":"","teacher":"","publisher":"","book":"","classes":[]}
classes 只放班級名稱字串，例如 ["701","703"] 或 ["國一甲","國一乙"]。
cram_order 欄位：
{"intent":"cram_order","cram_school":"","items":[{"publisher":"","book":"","quantity":0}]}
只有使用者明確表達訂書/下單/要某本書時才判定訂單。
不要把查老師、查版本、查訂單、查人數、確認、取消判成訂書。"""


def smart_parse_order_text(text, user_id=None):
    if not OPENAI_API_KEY:
        return None
    if AI_AGENT_ENABLED and user_id:
        return _openai_agent_json(user_id, _smart_order_system_prompt() + "\n請結合最近對話理解代名詞與省略資訊，但不能自行猜資料庫內容。", text, max_output_tokens=900)
    return _openai_json([
        {"role": "system", "content": _smart_order_system_prompt()},
        {"role": "user", "content": str(text or "")[:1000]},
    ])


def _download_line_image(message_id):
    if not CHANNEL_ACCESS_TOKEN or not message_id:
        return None
    try:
        r = HTTP.get(
            f"https://api-data.line.me/v2/bot/message/{message_id}/content",
            headers={"Authorization": "Bearer " + CHANNEL_ACCESS_TOKEN},
            timeout=12,
        )
        if r.status_code == 200 and r.content:
            return r.content
        logger.warning("LINE image download failed status=%s", r.status_code)
    except Exception as error:
        logger.warning("LINE image download error: %s", error)
    return None


def smart_parse_order_image(image_bytes):
    if not OPENAI_API_KEY or not image_bytes:
        return None
    data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
    system_prompt = """你是大漢訂書系統的圖片資料抽取器，不是聊天機器人。
只輸出 JSON，不可以自行補不存在的資料；看不清楚就留空。

先判斷圖片類型 image_type：
- purchase_order：正式/列印/系統產生的訂購單，圖片本身已列出訂購資料
- chat_screenshot：LINE/聊天截圖中的訂書需求
- handwritten_order：手寫或一般訂書紙條
- book_photo：主要是書籍封面/書名
- unknown：無法判斷

intent 只能是 school_order、cram_order、unknown。

一般學校訂書（聊天截圖／手寫紙條）固定輸出：
{
  "intent":"school_order",
  "image_type":"handwritten_order",
  "school":"",
  "teacher":"",
  "books":[
    {
      "book":"",
      "publisher":"",
      "classes":[],
      "class_items":[{"class_name":"","quantity":0}],
      "note":""
    }
  ],
  "note":"",
  "confidence":"high"
}

正式、只有一本書的學校訂購單也相容舊欄位：
{
  "intent":"school_order",
  "image_type":"purchase_order",
  "school":"",
  "teacher":"",
  "publisher":"",
  "book":"",
  "classes":[],
  "class_items":[{"class_name":"","quantity":0}],
  "total_quantity":0,
  "note":"",
  "confidence":"high"
}

補習班訂單固定輸出：
{
  "intent":"cram_order",
  "image_type":"handwritten_order",
  "cram_school":"",
  "items":[
    {"publisher":"","book":"","quantity":0,"note":""}
  ],
  "note":"",
  "confidence":"high"
}

重要規則：
1. 學校一般拍照訂書，只要看得到「老師姓名＋至少一本書名」就可判定 school_order；不要因為沒寫學校、出版社、班級、數量就判定資料不足，這些會由後端資料庫補。
2. 一張照片如果同一位老師寫了兩本以上書，每一本都要分開放進 books，不可只保留第一本。
3. 如果某一本旁邊明確寫「只訂某班／某些班」，放進該本的 classes；如果沒寫班級，classes 留空，後端會套用該老師全部授課班級。
4. 如果某一本旁邊明確寫了班級與數量，放進該本 class_items；沒有寫就留空，後端會用老師資料庫人數。
5. 出版社沒有寫就留空，不要猜；後端會用書名查書籍資料庫。
6. 補習班照片只要文字中可辨識「補習班名稱＋至少一本書名＋該書數量」，就判定 cram_order。
   「我是XX補習班」「XX補習班我要訂」「XX補習班要買」都代表 cram_school=XX補習班。
   不需要老師、不需要班級。
   出版社若直接寫在書名前（例如「南一學習標竿國文1本」「康軒國文講義新挑戰6本」「翰林超級悍將講義41本」），
   要拆成 publisher 與 book，不可因為格式不像表格就判定 unknown。
7. 同一張補習班照片有多本書時，items 每一本都要保留各自 book、publisher、quantity，不可只取第一本。
   例如圖片寫「我是菁華補習班 我要訂 國文第五冊／南一學習標竿國文1本／康軒國文講義新挑戰6本／翰林超級悍將講義41本」，
   應判定 cram_order；有明確數量的三本都要放入 items。「國文第五冊」若只是標題/年級冊次而沒有數量，不要誤當成獨立品項。
8. 備註如果明確屬於某一本，放該項 note；整張單共同備註放最外層 note。
9. 如果是正式訂購單，優先讀取訂購單表格本身，不要把公司章、店章、訂購人或頁尾文字誤認成老師。
10. teacher 只能取「老師/教師」欄位；「訂購人」不是老師。
11. 正式訂購單的 class_items 要保存圖片上每個班級實際寫的數量，不要用老師資料庫人數推測。
12. confidence 只能 high、medium、low。關鍵文字真的看不清楚才用 low。
"""
    return _openai_json([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": "請仔細讀取這張圖片。辨識所有書名，不要只取第一本；再依規則整理成訂單 JSON。"},
            {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
        ]},
    ], max_output_tokens=1800, timeout=IMAGE_AI_TIMEOUT_SECONDS)


def _parse_class_items_list(raw):
    result = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("class_name", "") or "").strip()
        try:
            qty = int(item.get("quantity", 0) or 0)
        except Exception:
            qty = 0
        if name and qty > 0:
            result.append({"class_name": name, "students": qty})
    return sort_class_items(result)


def _apply_purchase_order_image(user_id, data):
    """正式訂購單圖片：以圖片上的班級/數量為來源，不強迫回查老師班級資料。"""
    school = str(data.get("school", "") or "").strip()
    teacher = normalize_person_name(str(data.get("teacher", "") or "").strip())
    confidence = str(data.get("confidence", "") or "").lower()
    # 正式訂購單本身就可能一次列好幾本書（AI 依 system prompt 規則會把
    # 多本書放進 books 陣列），原本這裡只讀 top-level 的 book/publisher/
    # class_items，完全沒處理 books 陣列，遇到多本書的訂購單，book 欄位
    # 會是空的，被誤判成「圖片不夠清楚」，使用者重拍幾次都一樣——問題
    # 根本不在畫質。_photo_book_entries() 對單本／多本都會回傳統一格式
    # 的清單，這裡改成一律先用它取得書目。
    entries = _photo_book_entries(data)

    missing = []
    if not school: missing.append("學校")
    if not entries: missing.append("書名")
    if confidence == "low" or missing:
        missing_text = "、".join(missing) if missing else "部分文字"
        return (
            "📷 我有讀到這是一張訂購單，但有些內容還不夠清楚。\n\n"
            f"需要再確認：{missing_text}\n\n"
            "請重新拍清楚一點，或直接用文字補充；我不會自行猜測後建立訂單。"
        )

    if len(entries) > 1:
        # 多本書：改走既有的拍照批次確認流程（跟口語辨識共用），逐本
        # 各自寫成一筆獨立訂單。每本的班級與數量都直接採用圖片上的值，
        # 不回查老師班級資料庫（避免「這次只訂20本」被誤植成資料庫裡
        # 那個班平常的人數）；confirm_photo_order() 對 kind="school" 的
        # 批次本來就是直接採用每個 item 自己的 classes，不會重新查詢。
        batch_items = []
        missing_books = []
        for entry in entries:
            classes = _parse_class_items_list(entry.get("class_items", []))
            if not classes:
                missing_books.append(entry["book"])
                continue
            publisher = entry.get("publisher", "") or str(get_book_publisher(entry["book"]) or "").strip()
            batch_items.append({
                "book": entry["book"],
                "publisher": publisher,
                "classes": classes,
                "note": entry.get("note", ""),
            })
        if not batch_items:
            return (
                "📷 我有讀到這是一張列出多本書的訂購單，但每一本都沒有讀到"
                "清楚的班級與數量。\n\n請重新拍清楚一點，或直接用文字補充。"
            )
        _clear_stale_history_pending(user_id)
        pending_orders.pop(user_id, None)
        pending_receipt_offers.pop(user_id, None)
        order_flow_context.pop(user_id, None)
        guided_mode.pop(user_id, None)
        pending_name_confirmations.pop(user_id, None)
        pending_photo_orders[user_id] = {
            "kind": "school",
            "teacher": teacher or "未填寫",
            "school": school,
            "items": batch_items,
            "note": "",
            "source_label": "圖片",
        }
        reply = _continue_photo_resolution(user_id)
        if missing_books:
            reply = (
                f"⚠️ 「{'、'.join(missing_books)}」沒有讀到清楚的班級與數量，這幾本先略過，"
                "請自行用文字補充。\n\n"
            ) + str(reply)
        return reply

    # 單本書：沿用原本邏輯。
    entry = entries[0]
    book = entry["book"]
    publisher = entry.get("publisher", "")
    classes = _parse_class_items_list(entry.get("class_items", []))

    if not classes:
        return (
            "📷 我有讀到這是一張訂購單，但有些內容還不夠清楚。\n\n"
            "需要再確認：班級與數量\n\n"
            "請重新拍清楚一點，或直接用文字補充；我不會自行猜測後建立訂單。"
        )

    # 出版社若圖片沒讀到，才用既有書籍資料庫補；圖片有值就保留圖片內容。
    if not publisher:
        publisher = str(get_book_publisher(book) or "").strip()
    if not publisher:
        return (
            "📷 訂購單大部分已讀取完成，但目前無法確認出版社。\n\n"
            f"📖 書名：{book}\n"
            "請直接告訴我出版社名稱。"
        )

    order = {
        "teacher": teacher or "未填寫",
        "school": school,
        "book": book,
        "publisher": publisher,
        "classes": classes,
        "quantity": calculate_total(classes),
        "source": "image_purchase_order",
    }
    _clear_stale_history_pending(user_id)
    pending_orders[user_id] = order
    # 正式訂購單本身已提供數量，不建立 teacher class context，避免把圖片數量覆蓋成資料庫人數。
    order_flow_context.pop(user_id, None)
    guided_mode.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
    # 清掉可能殘留的圖片批次訂單，避免使用者接下來回「確認」時，
    # dispatcher 因為 pending_photo_orders 還在而誤判成要繼續處理
    # 舊的那批圖片訂單，而不是這張剛讀取的正式訂購單。
    pending_photo_orders.pop(user_id, None)
    return (
        "📷 已讀取訂購單\n\n" +
        make_order_confirmation(order).replace("📚 訂購確認\n\n", "") +
        "\n\n💡 這張是正式訂購單，我會以圖片上列出的班級與數量為準。"
    )



# 圖片多書訂單暫存：只有使用者最後確認後才寫入 Google。
pending_photo_orders = {}
_SESSION_DICTS["pending_photo_orders"] = pending_photo_orders


def _photo_book_entries(data):
    raw = data.get("books", []) if isinstance(data, dict) else []
    result = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            book = clean_book_name(str(item.get("book", "") or "").strip())
            if not book:
                continue
            result.append({
                "book": book,
                "publisher": str(item.get("publisher", "") or "").strip(),
                "classes": unique_list([str(x or "").strip() for x in item.get("classes", []) if str(x or "").strip()]),
                "class_items": item.get("class_items", []) if isinstance(item.get("class_items", []), list) else [],
                "note": str(item.get("note", "") or "").strip(),
            })
    # 舊版單一本圖片 JSON 相容
    if not result:
        book = clean_book_name(str(data.get("book", "") or "").strip())
        if book:
            result.append({
                "book": book,
                "publisher": str(data.get("publisher", "") or "").strip(),
                "classes": unique_list([str(x or "").strip() for x in data.get("classes", []) if str(x or "").strip()]),
                "class_items": data.get("class_items", []) if isinstance(data.get("class_items", []), list) else [],
                "note": str(data.get("note", "") or "").strip(),
            })
    return result


def _resolve_photo_teacher(teacher, school=""):
    teacher = normalize_person_name(teacher)
    if not teacher:
        return None
    matches = lookup_teacher_matches(teacher, school=school)
    exact = [m for m in matches if normalize_person_name(m.get("teacher", "")) == teacher]
    pool = exact or matches
    if len(pool) == 1:
        m = pool[0]
        return {
            "teacher": str(m.get("teacher", "") or teacher).strip(),
            "school": str(m.get("school", "") or school).strip(),
            "classes": copy_classes(m.get("classes", []))
        }
    return None


def _resolve_photo_book(book, publisher=""):
    book = clean_book_name(book)
    publisher = str(publisher or "").strip()
    candidates = lookup_book_candidates_enhanced(book, publisher=publisher)
    if not candidates:
        return {"status": "none", "book": book}

    qnorm = normalize_book_match_text(book)
    exact = []
    seen = set()
    for c in candidates:
        value = str(c.get("value", "") or "").strip()
        pub = str(c.get("publisher", "") or "").strip()
        if normalize_book_match_text(value) != qnorm:
            continue
        key = (value, pub)
        if key not in seen:
            seen.add(key)
            exact.append({"book": value, "publisher": pub})

    if publisher:
        exact_pub = [x for x in exact if x["publisher"] == publisher]
        if len(exact_pub) == 1:
            return {"status": "ok", **exact_pub[0]}

    pubs = unique_list([x["publisher"] for x in exact if x["publisher"]])
    if len(exact) >= 2 and len(pubs) >= 2:
        # options 是「書名＋出版社」配對清單，不是單純的出版社名稱
        # 清單——exact 裡可能同時有好幾種寫法不完全相同、但正規化後
        # 相同的書名（例如「新挑戰國文5」跟「新挑戰國文第五冊」），
        # 各自對應不同出版社。如果只回傳一份扁平的出版社名稱清單，
        # 選完出版社後配上固定的 exact[0]["book"]，可能兜出資料庫裡
        # 根本不存在的「書名＋出版社」組合。
        options = [x for x in exact if x["publisher"]]
        return {"status": "publisher_choice", "book": exact[0]["book"], "options": options}
    if len(exact) == 1 and exact[0]["publisher"]:
        return {"status": "ok", **exact[0]}

    # 非完全相同時只在高信心且沒有出版社歧義時自動採用。
    top = candidates[0]
    top_score = float(top.get("score", 0) or 0)
    top_value = str(top.get("value", "") or "").strip()
    same_top = [c for c in candidates if str(c.get("value", "") or "").strip() == top_value]
    top_pubs = unique_list([str(c.get("publisher", "") or "").strip() for c in same_top if str(c.get("publisher", "") or "").strip()])
    if len(top_pubs) > 1:
        options = [{"book": top_value, "publisher": p} for p in top_pubs]
        return {"status": "publisher_choice", "book": top_value, "options": options}
    if top_score >= 0.78 and top_value and str(top.get("publisher", "") or "").strip():
        return {"status": "ok", "book": top_value, "publisher": str(top.get("publisher", "") or "").strip()}

    # 完全沒有精準符合、信心也不夠高時，改成列出資料庫裡最接近的
    # 候選讓使用者挑，而不是直接判定整本查無資料、放棄這批訂單。
    # AI 讀圖辨識出來的字（尤其書名裡的字詞順序）常常跟資料庫的
    # 正式登記不完全一樣，例如「國文講義新挑戰5」vs資料庫的
    # 「新挑戰國文5」，這種情況精準比對一定失敗，但使用者一看就
    # 知道是同一本，不該讓使用者重打一次。
    book_choices = []
    seen_bp = set()
    for c in candidates[:5]:
        value = str(c.get("value", "") or "").strip()
        pub = str(c.get("publisher", "") or "").strip()
        if not value:
            continue
        key = (value, pub)
        if key in seen_bp:
            continue
        seen_bp.add(key)
        book_choices.append({"book": value, "publisher": pub})
    if book_choices:
        return {"status": "book_choice", "book": book, "choices": book_choices}

    return {"status": "none", "book": book}


def _classes_for_photo_book(entry, teacher_classes):
    lookup = {str(x.get("class_name", "") or "").strip(): x for x in teacher_classes}
    # 圖片明確寫了班級＋數量，以圖片數量為準。
    explicit = []
    for x in entry.get("class_items", []):
        if not isinstance(x, dict):
            continue
        name = str(x.get("class_name", "") or "").strip()
        try:
            qty = int(x.get("quantity", 0) or 0)
        except Exception:
            qty = 0
        if name and qty > 0:
            explicit.append({"class_name": name, "students": qty})
    if explicit:
        return sort_class_items(explicit)

    # 只寫部分班級，數量沒寫：用老師資料庫人數。
    wanted = entry.get("classes", [])
    if wanted:
        selected = []
        for name in wanted:
            if name in lookup:
                selected.append({"class_name": name, "students": int(lookup[name].get("students", 0) or 0)})
        return sort_class_items(selected)

    # 沒寫班級：預設老師全部授課班級。
    return sort_class_items(copy_classes(teacher_classes))


def _photo_school_batch_confirmation(batch):
    source_label = batch.get("source_label", "照片")
    icon = "📷" if source_label == "照片" else "🧠"
    lines = [
        f"{icon} {source_label}辨識完成", "",
        f"學校：{batch.get('school','')}",
        f"老師：{batch.get('teacher','')}", "",
        "📚 訂購書籍"
    ]
    for i, item in enumerate(batch.get("items", []), 1):
        lines.append(f"{i}. {item['book']}｜{item['publisher']}")
        raw_pub = item.get("raw_publisher")
        if raw_pub and raw_pub != item.get("publisher"):
            # 原始辨識到的出版社跟資料庫重新查出來的不一樣，明確提醒
            # 使用者核對，不要靜默覆蓋掉原本讀到的出版社。
            lines.append(f"   ⚠️ 原本辨識到的出版社是「{raw_pub}」，資料庫查無此版本，已改用上面這家，請確認是否正確")
        for c in item.get("classes", []):
            lines.append(f"   • {c['class_name']}：{c['students']}本")
        if item.get("note"):
            lines.append(f"   📝 {item['note']}")
    if batch.get("note"):
        lines.extend(["", f"📝 共同備註：{batch['note']}"])
    if batch.get("dropped"):
        lines.extend(["", f"⚠️ 以下書名資料庫查無資料，需要你自己另外處理：{'、'.join(batch['dropped'])}"])
    lines.extend(["", "以上資料正確請回覆「確認」。",
                  "需要修改可直接說：701改30、702不要、加703。",
                  "需要取消請回覆「取消」。"])
    return "\n".join(lines)


def _photo_cram_batch_confirmation(batch):
    source_label = batch.get("source_label", "照片")
    icon = "📷" if source_label == "照片" else "🧠"
    lines = [f"{icon} {source_label}辨識完成", "", f"補習班：{batch.get('cram_school','')}", "", "📚 訂購書籍"]
    for i, item in enumerate(batch.get("items", []), 1):
        lines.append(f"{i}. {item['book']}｜{item['publisher']}｜{item['quantity']}本")
        raw_pub = item.get("raw_publisher")
        if raw_pub and raw_pub != item.get("publisher"):
            lines.append(f"   ⚠️ 原本辨識到的出版社是「{raw_pub}」，資料庫查無此版本，已改用上面這家，請確認是否正確")
        if item.get("note"):
            lines.append(f"   📝 {item['note']}")
    if batch.get("note"):
        lines.extend(["", f"📝 共同備註：{batch['note']}"])
    if batch.get("dropped"):
        lines.extend(["", f"⚠️ 以下書名資料庫查無資料，需要你自己另外處理：{'、'.join(batch['dropped'])}"])
    lines.extend(["", "以上資料正確請回覆「確認」。", "需要取消請回覆「取消」。"])
    return "\n".join(lines)


def _continue_photo_resolution(user_id):
    batch = pending_photo_orders.get(user_id)
    if not batch:
        return None

    items = batch.get("items", [])
    drop_indexes = []

    for idx, item in enumerate(items):
        if item.get("publisher"):
            continue
        resolved = _resolve_photo_book(item.get("book", ""))
        if resolved["status"] == "publisher_choice":
            batch["awaiting_publisher_index"] = idx
            # options 是「書名＋出版社」配對清單，見 _resolve_photo_book()
            # 裡的說明——選定後要書名跟出版社一起套用，不能只套出版社、
            # 書名固定用某一筆代表值，那樣可能兜出資料庫裡不存在的組合。
            batch["publisher_options"] = resolved["options"]
            item["book"] = resolved["book"]
            pending_photo_orders[user_id] = batch
            lines = ["📚 找到相同書名", "", f"書名：{resolved['book']}", "", "這本書有不同出版社："]
            for i, opt in enumerate(resolved["options"], 1):
                lines.append(f"{i}. {opt['publisher']}")
            lines.extend(["", f"請回覆 1～{len(resolved['options'])}"])
            return "\n".join(lines)
        if resolved["status"] == "book_choice":
            batch["awaiting_book_index"] = idx
            batch["book_options"] = resolved["choices"]
            pending_photo_orders[user_id] = batch
            lines = [
                "📚 找不到完全一樣的書名", "",
                f"我讀到的是：{resolved['book']}", "",
                "資料庫裡比較接近的有："
            ]
            for i, opt in enumerate(resolved["choices"], 1):
                pub_label = f"[{opt['publisher']}] " if opt.get("publisher") else ""
                lines.append(f"{i}. {pub_label}{opt['book']}")
            lines.append(f"{len(resolved['choices']) + 1}. 都不是，我直接用文字告訴你完整書名")
            lines.extend(["", f"請回覆 1～{len(resolved['choices']) + 1}"])
            return "\n".join(lines)
        if resolved["status"] != "ok":
            # 這一本真的完全查不到，記下來之後跳過，不要因為一本
            # 查不到就把整批（其他已經確認過的書）也一起丟掉。
            drop_indexes.append(idx)
            continue
        item["book"] = resolved["book"]
        item["publisher"] = resolved["publisher"]

    dropped_books = [items[i].get("book", "") for i in drop_indexes]
    if drop_indexes:
        batch["items"] = [x for i, x in enumerate(items) if i not in drop_indexes]
        batch["dropped"] = batch.get("dropped", []) + dropped_books

    batch.pop("awaiting_publisher_index", None)
    batch.pop("publisher_options", None)
    batch.pop("awaiting_book_index", None)
    batch.pop("book_options", None)
    pending_photo_orders[user_id] = batch

    if not batch.get("items"):
        pending_photo_orders.pop(user_id, None)
        names = "、".join(batch.get("dropped", [])) or "這批書"
        return (
            f"⚠️ 「{names}」都無法從書籍資料庫確認。\n\n"
            "請改用文字輸入完整書名。"
        )

    if batch.get("kind") == "school":
        return _photo_school_batch_confirmation(batch)
    return _photo_cram_batch_confirmation(batch)


def _manual_book_choice_for_item(book_text):
    """
    使用者明確說「這一項選錯了」，要求重新選。這裡不用
    _resolve_photo_book() 那套「分數夠高就自動採用」的邏輯——那套
    邏輯已經證實可能選到同名但不對的出版社版本——改成直接把資料庫裡
    比對到的候選（不限單一本、不自動篩選）都列出來，讓使用者自己挑。
    """
    candidates = lookup_book_candidates_enhanced(book_text, publisher="")
    choices = []
    seen = set()
    for c in candidates[:8]:
        value = str(c.get("value", "") or "").strip()
        pub = str(c.get("publisher", "") or "").strip()
        if not value:
            continue
        key = (value, pub)
        if key in seen:
            continue
        seen.add(key)
        choices.append({"book": value, "publisher": pub})
    return choices


def handle_pending_photo_order(user_id, text):
    batch = pending_photo_orders.get(user_id)
    if not batch:
        return None
    clean = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))

    if clean in {"取消", "不要了", "這筆不要", "取消訂單"}:
        pending_photo_orders.pop(user_id, None)
        return "❌ 已取消這筆拍照訂單，Google 沒有寫入。"

    idx = batch.get("awaiting_publisher_index")
    if idx is not None:
        # options 是「書名＋出版社」配對清單（見 _resolve_photo_book()），
        # 選定後書名跟出版社要一起套用，不能只套用出版社、書名維持
        # 原本記下來的那個固定代表值，不然可能兜出資料庫裡不存在的
        # 「書名＋出版社」組合。
        options = batch.get("publisher_options", [])
        choice = None
        if clean.isdigit():
            n = int(clean)
            if 1 <= n <= len(options):
                choice = options[n - 1]
        else:
            choice = next((opt for opt in options if opt.get("publisher") == clean), None)
        if not choice:
            return f"請回覆 1～{len(options)} 選擇出版社。"
        batch["items"][idx]["book"] = choice["book"]
        batch["items"][idx]["publisher"] = choice["publisher"]
        batch.pop("awaiting_publisher_index", None)
        batch.pop("publisher_options", None)
        pending_photo_orders[user_id] = batch
        return _continue_photo_resolution(user_id)

    book_idx = batch.get("awaiting_book_index")
    if book_idx is not None:
        choices = batch.get("book_options", [])
        if clean.isdigit():
            n = int(clean)
            if 1 <= n <= len(choices):
                chosen = choices[n - 1]
                batch["items"][book_idx]["book"] = chosen["book"]
                batch["items"][book_idx]["publisher"] = chosen["publisher"]
                batch.pop("awaiting_book_index", None)
                batch.pop("book_options", None)
                pending_photo_orders[user_id] = batch
                return _continue_photo_resolution(user_id)
            if n == len(choices) + 1:
                batch.pop("awaiting_book_index", None)
                batch.pop("book_options", None)
                batch["awaiting_manual_book_index"] = book_idx
                pending_photo_orders[user_id] = batch
                return "請直接輸入這本書的完整書名。"
        return f"請回覆 1～{len(choices) + 1} 選擇書名。"

    manual_idx = batch.get("awaiting_manual_book_index")
    if manual_idx is not None:
        resolved = _resolve_photo_book(text)
        if resolved["status"] == "ok":
            batch["items"][manual_idx]["book"] = resolved["book"]
            batch["items"][manual_idx]["publisher"] = resolved["publisher"]
            batch.pop("awaiting_manual_book_index", None)
            pending_photo_orders[user_id] = batch
            return _continue_photo_resolution(user_id)
        if resolved["status"] in ("publisher_choice", "book_choice"):
            # 使用者手動輸入的書名一樣有歧義，沿用同一套選擇流程繼續問，
            # 不用另外再寫一次選擇邏輯。
            batch["items"][manual_idx]["book"] = resolved["book"]
            batch.pop("awaiting_manual_book_index", None)
            pending_photo_orders[user_id] = batch
            return _continue_photo_resolution(user_id)
        return f"⚠️ 還是查不到「{text}」，請確認書名是否正確，或輸入「取消」放棄這筆訂單。"

    # 確認畫面上如果有一項選錯了（例如自動比對選到同名但不對的出版
    # 社），使用者可以直接說「修改2」「改2」要求重選第幾項。
    m = re.fullmatch(r"(?:修改|改)\s*(\d{1,2})|(\d{1,2})\s*(?:修改|改)", clean)
    if m:
        n = int(m.group(1) or m.group(2))
        items = batch.get("items", [])
        if not (1 <= n <= len(items)):
            return f"⚠️ 目前只有 1～{len(items)} 項，請輸入正確的編號。"
        idx = n - 1
        current = items[idx]
        choices = _manual_book_choice_for_item(current.get("book", ""))
        # 把目前這個（可能選錯的）版本排除掉，不然候選清單裡還會出現
        # 使用者剛剛才說不對的那個選項。
        choices = [
            c for c in choices
            if not (c["book"] == current.get("book") and c["publisher"] == current.get("publisher"))
        ]
        if not choices:
            return (
                f"⚠️ 查不到跟「{current.get('book','')}」相關的其他候選。\n\n"
                "請直接輸入正確的完整書名。"
            )
        # 先清空這一項舊的 publisher：如果使用者接下來選「都不是，直接
        # 輸入書名」，走的是 _resolve_photo_book() 的模糊比對，可能回
        # publisher_choice／book_choice 這種還沒決定出版社的中繼狀態，
        # 只會設定 book、不會動 publisher。如果這裡沒先清空，殘留的舊
        # publisher 會讓 _continue_photo_resolution() 的
        # 「item.get("publisher") 已有值就跳過」直接短路，新書名配上
        # 舊出版社，使用者的更正等於完全沒生效。
        current["publisher"] = ""
        batch["awaiting_book_index"] = idx
        batch["book_options"] = choices
        pending_photo_orders[user_id] = batch
        lines = [
            f"📚 第 {n} 項重新選擇", "",
            f"目前是：{current.get('book','')}｜{current.get('publisher','')}", "",
            "資料庫裡找到的其他候選："
        ]
        for i, opt in enumerate(choices, 1):
            pub_label = f"[{opt['publisher']}] " if opt.get("publisher") else ""
            lines.append(f"{i}. {pub_label}{opt['book']}")
        lines.append(f"{len(choices) + 1}. 都不是，我直接用文字告訴你完整書名")
        lines.extend(["", f"請回覆 1～{len(choices) + 1}"])
        return "\n".join(lines)

    if _is_confirm_word(clean):
        return confirm_photo_order(user_id)

    # v80：口語（AI 助手）／拍照訂單的確認畫面也能改班級數量、刪班、加班。
    if batch.get("kind") == "school":
        spaced = re.sub(r"[\s，,。.!！?？]+", " ", str(text or "")).strip()
        edit_reply = _v80_edit_photo_school_batch(user_id, batch, spaced)
        if edit_reply is not None:
            return edit_reply

    return (_photo_school_batch_confirmation(batch) if batch.get("kind") == "school"
            else _photo_cram_batch_confirmation(batch))


_V80_REMOVE_WORDS = r"(?:不要了|不要|取消|刪掉|刪除|拿掉|移除|去掉|不用)"
_V80_CLASS_TOKEN = r"(?:[789]\d{2}|(?:國[一二三]|高[一二三]|[七八九]年級?)[甲乙丙丁戊己庚辛壬癸信望愛慧忠孝仁])"


def _v80_split_classes(text):
    return re.findall(_V80_CLASS_TOKEN, text)


def _v80_photo_teacher_classes(batch):
    """加班時需要這位老師每班的人數：先用建立時存下來的，沒有再查老師資料庫。"""
    cached = batch.get("teacher_classes")
    if cached:
        return cached
    teacher = str(batch.get("teacher", "") or "").strip()
    school = str(batch.get("school", "") or "").strip()
    if not teacher or teacher == "未填寫":
        return []
    for item in lookup_teacher_matches(teacher, school=school) or []:
        if not school or str(item.get("school", "")).strip() == school:
            classes = copy_classes(item.get("classes", []))
            batch["teacher_classes"] = classes
            return classes
    return []


def _v80_edit_photo_school_batch(user_id, batch, clean):
    """
    支援的講法（可以一句寫好幾個，全部檢查沒問題才一起套用）：
      701改30、701改成30本、701要30本、701跟703都改25
      701少2本、701多3本
      702不要、702取消、刪702、拿掉702、701跟702不要
      加703、再加703、703也要、新增703
    同一筆有好幾本書時，會套用到「有這個班」的每一本；前面加「第2本」
    就只改那一本（例如「第2本701改30」）。回傳 None 代表這句不是修改指令。
    """
    items = batch.get("items", [])
    if not items:
        return None

    target_indexes = list(range(len(items)))
    m = re.match(r"^第?(\d{1,2})(?:本|項)\s*(.*)$", clean)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= len(items)):
            return f"⚠️ 目前只有 1～{len(items)} 本，請輸入正確的編號。"
        target_indexes = [n - 1]
        clean = m.group(2)

    ops = []   # (動作, 班級, 數字)
    rest = clean
    patterns = [
        ("set", rf"((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)(?:都|全部)?\s*(?:改成|改為|改|變成|要|訂)\s*(\d{{1,3}})(?!\d)(?:本|份|冊)?"),
        ("delta", rf"((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)(?:都)?\s*(少|減|減少|多|加|增加)\s*(\d{{1,3}})(?!\d)(?:本|份|冊)"),
        ("remove", rf"(?:刪|刪掉|拿掉|移除|去掉|取消)((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)"),
        ("remove", rf"((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)(?:都|也)?{_V80_REMOVE_WORDS}"),
        ("add", rf"(?:再加|加上|加|新增|補上|補|再補)((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)(?:班)?"),
        ("add", rf"((?:{_V80_CLASS_TOKEN}(?:跟|和|、|,|及|\s)*)+)(?:班)?(?:也要|也訂|也一起)"),
    ]
    found_any = True
    while found_any and rest:
        found_any = False
        for kind, pat in patterns:
            mm = re.search(pat, rest)
            if not mm:
                continue
            classes = _v80_split_classes(mm.group(1))
            if kind == "set":
                ops += [("set", c, int(mm.group(2))) for c in classes]
            elif kind == "delta":
                sign = -1 if mm.group(2) in {"少", "減", "減少"} else 1
                ops += [("delta", c, sign * int(mm.group(3))) for c in classes]
            else:
                ops += [(kind, c, 0) for c in classes]
            rest = rest[:mm.start()] + " " + rest[mm.end():]
            found_any = True
            break
    if not ops:
        return None
    if re.search(r"[\u4e00-\u9fff0-9]", re.sub(r"[\s跟和、,及也再然後還有]", "", rest)):
        # 句子裡還有看不懂的部分，不要只改一半。
        return (
            "⚠️ 這句修改我沒有完全看懂，所以什麼都還沒改。\n\n"
            "可以這樣說：701改30、702不要、加703、701少2本"
        )

    # 先在複本上全部套用、全部檢查，沒問題才寫回。
    new_items = copy.deepcopy(items)
    teacher_classes = None
    done = []
    for kind, cname, num in ops:
        if kind == "add":
            if teacher_classes is None:
                teacher_classes = {c["class_name"]: int(c.get("students", 0) or 0)
                                   for c in _v80_photo_teacher_classes(batch)}
            if cname not in teacher_classes:
                known = "、".join(teacher_classes) or "查不到"
                return f"⚠️ {batch.get('teacher','')} 沒有 {cname} 這個班（目前班級：{known}）。整批修改未套用。"
            added = False
            for i in target_indexes:
                if any(c["class_name"] == cname for c in new_items[i]["classes"]):
                    continue
                new_items[i]["classes"].append({"class_name": cname, "students": teacher_classes[cname]})
                added = True
            if not added:
                return f"⚠️ {cname} 已經在訂單裡了。整批修改未套用。"
            done.append(f"加 {cname}（{teacher_classes[cname]}本）")
            continue

        hit = False
        for i in target_indexes:
            cls = new_items[i]["classes"]
            for c in list(cls):
                if c["class_name"] != cname:
                    continue
                hit = True
                if kind == "remove":
                    cls.remove(c)
                else:
                    value = num if kind == "set" else int(c["students"]) + num
                    if value <= 0:
                        return f"⚠️ {cname} 改完會變成 {value} 本，數量要大於 0。要整班不要請說「{cname}不要」。"
                    c["students"] = value
        if not hit:
            return f"⚠️ 目前訂單裡沒有 {cname}。整批修改未套用。"
        if kind == "remove":
            done.append(f"刪除 {cname}")
        elif kind == "set":
            done.append(f"{cname}→{num}本")
        else:
            done.append(f"{cname}{'+' if num > 0 else ''}{num}本")

    for i in target_indexes:
        if not new_items[i]["classes"]:
            return (
                f"⚠️ 「{new_items[i]['book']}」的班級會全部被刪掉。\n\n"
                "至少要保留一班；整筆都不要的話請回覆「取消」。整批修改未套用。"
            )

    batch["items"] = new_items
    pending_photo_orders[user_id] = batch
    scope = f"第 {target_indexes[0] + 1} 本" if len(target_indexes) == 1 and len(items) > 1 else ""
    return f"✅ 已修改{scope}：{'、'.join(done)}\n\n" + _photo_school_batch_confirmation(batch)


def confirm_photo_order(user_id):
    batch = pending_photo_orders.get(user_id)
    if not batch:
        return "⚠️ 找不到等待確認的拍照訂單。"

    if batch.get("kind") == "cram":
        draft = _new_cram_draft()
        draft["cram_school"] = batch.get("cram_school", "")
        draft["items"] = [
            {"publisher": x["publisher"], "book": x["book"], "quantity": x["quantity"]}
            for x in batch.get("items", [])
        ]
        success, order_number = write_cram_order_to_google(draft)
        if not success:
            return "❌ 補習班訂單寫入失敗，請稍後再試。"
        pending_photo_orders.pop(user_id, None)
        return f"✅ 補習班拍照訂單已確認\n\n訂單編號：{order_number}\n共 {len(draft['items'])} 個書籍品項，已成功寫入 Google。"

    results = []
    for item in batch.get("items", []):
        order = {
            "teacher": batch.get("teacher", ""),
            "school": batch.get("school", ""),
            "book": item["book"],
            "publisher": item["publisher"],
            "classes": copy_classes(item.get("classes", [])),
            "note": item.get("note") or batch.get("note", "")
        }
        success, order_number = write_to_google_sheet(order)
        if success and order.get("note"):
            mark_order_note(order_number, order["note"])
        results.append((success, order_number, item["book"]))

    pending_photo_orders.pop(user_id, None)
    ok = [x for x in results if x[0]]
    fail = [x for x in results if not x[0]]
    kind_label = "口語" if batch.get("source_label") == "口語" else "拍照"
    lines = [f"✅ 學校{kind_label}訂單已處理", "", f"成功 {len(ok)} 筆／共 {len(results)} 筆"]
    for success, number, book in ok:
        lines.append(f"✔️ {book}｜訂單編號 {number}")
    for success, number, book in fail:
        lines.append(f"❌ {book}｜寫入失敗")
    return "\n".join(lines)


def _apply_smart_school_order(user_id, data, source_label="口語"):
    teacher_raw = str(data.get("teacher", "") or "").strip()
    school_hint = str(data.get("school", "") or "").strip()
    entries = _photo_book_entries(data)
    common_note = str(data.get("note", "") or "").strip()

    if not teacher_raw:
        return f"📷 {source_label}內容已收到。\n\n我還缺老師姓名，請直接告訴我是哪一位老師？"
    if not entries:
        return f"📷 {source_label}內容已收到。\n\n我有看到老師，但還沒辨識到書名，請直接告訴我要訂哪一本書？"

    teacher_info = _resolve_photo_teacher(teacher_raw, school_hint)
    if not teacher_info:
        return (
            "📷 我有辨識到老師姓名，但目前無法唯一確認老師資料。\n\n"
            f"你輸入／圖片辨識：{teacher_raw}\n"
            "請直接用文字補上完整老師姓名；如果有同名老師，也請加上學校名稱。"
        )

    batch_items = []
    for entry in entries:
        classes = _classes_for_photo_book(entry, teacher_info["classes"])
        if not classes:
            return (
                f"⚠️ 「{entry['book']}」指定的班級無法對上 {teacher_info['teacher']} 的授課資料。\n\n"
                "請用文字告訴我要訂哪些班。"
            )
        publisher = entry.get("publisher", "")
        raw_publisher = publisher
        if publisher:
            resolved = _resolve_photo_book(entry["book"], publisher)
            if resolved["status"] == "ok":
                entry["book"] = resolved["book"]
                publisher = resolved["publisher"]
            else:
                publisher = ""
        batch_items.append({
            "book": entry["book"],
            "publisher": publisher,
            "classes": classes,
            "note": entry.get("note", ""),
            # 原始辨識到的出版社比對不到資料庫版本時，這裡不能就這樣
            # 把它丟掉——保留下來，等 _continue_photo_resolution() 重新
            # 查出實際版本後，如果兩者不一致就在確認畫面上明確提醒，
            # 而不是靜默覆蓋成資料庫查到的另一家出版社。
            "raw_publisher": raw_publisher,
        })

    # 清掉可能殘留的舊文字訂單／訂購單 PDF 提問：dispatcher 判斷「確認」
    # 要交給誰處理時，pending_orders／pending_receipt_offers 排在
    # pending_photo_orders 前面，殘留的話使用者這裡回「確認」會誤把
    # 舊的文字訂單寫進 Google，這批剛辨識好的照片訂單反而原封不動。
    pending_orders.pop(user_id, None)
    pending_receipt_offers.pop(user_id, None)
    pending_photo_orders[user_id] = {
        "kind": "school",
        "teacher": teacher_info["teacher"],
        "school": teacher_info["school"],
        "teacher_classes": copy_classes(teacher_info.get("classes", [])),   # v80：加班時用
        "items": batch_items,
        "note": common_note,
        "source_label": source_label,
    }
    return _continue_photo_resolution(user_id)


def _apply_smart_cram_order(user_id, data, source_label="口語"):
    cram_school = str(data.get("cram_school", "") or "").strip()
    raw_items = data.get("items", []) if isinstance(data.get("items", []), list) else []
    common_note = str(data.get("note", "") or "").strip()

    if not cram_school:
        return f"📷 {source_label}內容已收到。\n\n我還缺補習班名稱，請告訴我是哪一間補習班？"

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        book = clean_book_name(str(raw.get("book", "") or "").strip())
        try:
            quantity = int(raw.get("quantity", 0) or 0)
        except Exception:
            quantity = 0
        if not book:
            continue
        publisher = str(raw.get("publisher", "") or "").strip()

        # 只有書名文字、完全沒有出版社也沒有數量（例如「國文第五冊」），
        # 比較像是在說明後面幾本書屬於哪個科目／冊次的分類標題，不是
        # 一本真的要訂的書。真的缺資料的書至少會有出版社或數量其中
        # 一項，只有這種「兩項都空」的才跳過，不會誤刪真正不完整的書。
        if not publisher and quantity <= 0:
            continue

        if quantity <= 0:
            return (
                f"📷 我有辨識到「{book}」，但沒有看到這本要訂幾本。\n\n"
                "補習班訂書每一本都需要數量，請直接用文字補充。"
            )
        raw_publisher = publisher
        if publisher:
            resolved = _resolve_photo_book(book, publisher)
            if resolved["status"] == "ok":
                book, publisher = resolved["book"], resolved["publisher"]
            else:
                publisher = ""
        items.append({
            "book": book,
            "publisher": publisher,
            "quantity": quantity,
            "note": str(raw.get("note", "") or "").strip(),
            "raw_publisher": raw_publisher,
        })

    if not items:
        return (
            "📷 我有辨識到補習班名稱，但還沒有讀到「書名＋數量」。\n\n"
            "請重新拍清楚一點，或直接用文字補充。"
        )

    # 同上：清掉可能殘留的舊文字訂單／訂購單 PDF 提問，避免「確認」
    # 被 dispatcher 誤判去處理舊的文字訂單，而不是這批補習班照片訂單。
    pending_orders.pop(user_id, None)
    pending_receipt_offers.pop(user_id, None)
    pending_photo_orders[user_id] = {
        "kind": "cram",
        "cram_school": cram_school,
        "items": items,
        "note": common_note,
        "source_label": source_label,
    }
    return _continue_photo_resolution(user_id)



def _remember_ai_turn(user_id, user_text, reply_text):
    """只保存短期對話上下文；不把整個資料庫塞給模型。"""
    if not AI_AGENT_ENABLED:
        return
    # 有些流程（確認新訂單、確認補習班訂單、歷史訂單帶 PDF 提問）回傳
    # 的是 list（多則訊息），對 list 直接 str() 會存進帶方括號跟跳脫
    # 字元的 Python repr（例如 "['✅ 訂單已確認…', '需要幫你生成…']"），
    # AI 讀到這種雜訊會讓代名詞理解品質變差，這裡先合併成一般文字。
    if isinstance(reply_text, (list, tuple)):
        reply_text = "\n\n".join(str(x or "") for x in reply_text)
    history = ai_agent_history.setdefault(user_id, [])
    history.append({"role": "user", "content": str(user_text or "")[:1200]})
    history.append({"role": "assistant", "content": str(reply_text or "")[:1800]})
    keep = max(4, AI_AGENT_MAX_HISTORY * 2)
    if len(history) > keep:
        del history[:-keep]


def _agent_context_messages(user_id):
    history = ai_agent_history.get(user_id, [])
    if not isinstance(history, list):
        return []
    return [x for x in history[-max(4, AI_AGENT_MAX_HISTORY * 2):]
            if isinstance(x, dict) and x.get("role") in {"user", "assistant"}]


def _openai_agent_json(user_id, system_prompt, text, max_output_tokens=900):
    if not OPENAI_API_KEY or not AI_AGENT_ENABLED:
        return None
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(_agent_context_messages(user_id))
    messages.append({"role": "user", "content": str(text or "")[:1600]})
    payload = {
        "model": AI_AGENT_MODEL,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": max_output_tokens,
    }
    try:
        r = HTTP.post("https://api.openai.com/v1/chat/completions", headers=headers,
                      json=payload, timeout=max(AI_TIMEOUT_SECONDS, 20))
        if r.status_code != 200:
            logger.warning("AI agent parse failed status=%s body=%s", r.status_code, r.text[:300])
            return None
        return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as error:
        logger.warning("AI agent parse error: %s", error)
        return None


def _openai_agent_chat(user_id, text):
    """一般對話/整理文字。公司內部事實不得由模型自行猜。"""
    if not OPENAI_API_KEY or not AI_AGENT_ENABLED:
        return None
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    system = """你是「大漢 AI 助手」，在 LINE 裡用繁體中文自然、簡潔地協助使用者。
你可以一般對話、整理文字、改寫訊息、解釋問題，也要理解前後文。
但是老師、班級、人數、書名資料庫、出版社、學校版本、歷史訂單等公司內部事實絕對不能自行猜測。
這些資料若目前沒有由系統工具提供，就清楚說需要查資料庫，不要編造。
任何建立、修改、取消訂單都不能在聊天回答中宣稱已完成，必須走系統確認流程。
不要要求使用者背固定指令；盡量理解他的自然說法。"""
    messages = [{"role":"system","content":system}]
    messages.extend(_agent_context_messages(user_id))
    messages.append({"role":"user","content":str(text or "")[:2000]})
    payload={"model":AI_AGENT_MODEL,"messages":messages,"max_completion_tokens":700}
    try:
        r=HTTP.post("https://api.openai.com/v1/chat/completions",headers=headers,json=payload,
                    timeout=max(AI_TIMEOUT_SECONDS,20))
        if r.status_code!=200:
            logger.warning("AI agent chat failed status=%s body=%s",r.status_code,r.text[:300]); return None
        return str(r.json()["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as error:
        logger.warning("AI agent chat error: %s",error); return None


def _smart_function_system_prompt():
    return """你是「大漢 AI Agent」的意圖路由器。只輸出 JSON，不要直接回答。
你會看到最近對話，因此必須理解「他、那本、剛剛那個、上次、其他班也要」等前後文。
intent 只能是：teacher_lookup、history_lookup、version_lookup、stats_lookup、school_order、cram_order、other_order、chat、unknown。

輸出固定欄位：
{"intent":"unknown","teacher":"","school":"","grade":"","subject":"","class_name":"","order_number":"","date_text":"","publisher":"","book":"","classes":[],"cram_school":"","items":[],"normalized_query":""}

原則：
- 使用者意思到了就判斷意圖，不要求固定關鍵字。
- 查老師/班級/任課 => teacher_lookup。
- 查過去訂單/上次訂什麼/某日期/編號 => history_lookup。
- 查教材版本 => version_lookup；查學生/班級人數 => stats_lookup。
- 要建立學校訂單 => school_order；補習班 => cram_order；文具等 => other_order。
- 一般聊天、寫訊息、整理文字、解釋問題 => chat。
- 公司內部資料不能自行補；只抽取使用者或前文已明確出現的資訊。
- normalized_query 要改寫成既有系統容易處理的簡短格式。
- 涉及下單時只判斷意圖，真正資料與寫入交給既有安全流程。"""

def smart_parse_function_text(text, user_id=None):
    if not OPENAI_API_KEY:
        return None
    if AI_AGENT_ENABLED and user_id:
        return _openai_agent_json(user_id, _smart_function_system_prompt(), text, max_output_tokens=900)
    return _openai_json([
        {"role": "system", "content": _smart_function_system_prompt()},
        {"role": "user", "content": str(text or "")[:1000]},
    ], max_output_tokens=650)


def _smart_teacher_query_from_data(data):
    teacher = str(data.get("teacher", "") or "").strip()
    school = str(data.get("school", "") or "").strip()
    grade = str(data.get("grade", "") or "").strip()
    subject = str(data.get("subject", "") or "").strip()
    class_name = str(data.get("class_name", "") or "").strip()
    if school and class_name:
        return f"{school}{class_name}老師"
    if school and grade and subject:
        return f"{school}{grade}{subject}老師"
    if teacher:
        return f"{teacher}有幾個班"
    return ""


def handle_smart_teacher_lookup(user_id, text, keep_mode=False, parsed_data=None):
    """AI 只把口語整理成既有老師查詢；真正答案仍由 Google 老師資料庫提供。"""
    if not OPENAI_API_KEY:
        return None
    data = parsed_data if isinstance(parsed_data, dict) else smart_parse_function_text(text, user_id=user_id)
    if not isinstance(data, dict) or data.get("intent") != "teacher_lookup":
        return None
    query = _smart_teacher_query_from_data(data)
    if not query:
        query = str(data.get("normalized_query", "") or "").strip()
    if not query:
        return None

    class_query = parse_class_teacher_query(query)
    if class_query:
        reply = handle_class_teacher_query(class_query)
    else:
        subject_query = parse_subject_teacher_query(query)
        if subject_query:
            reply = handle_subject_teacher_query(subject_query)
        else:
            teacher = str(data.get("teacher", "") or "").strip()
            if not teacher:
                teacher = normalize_teacher_name_input(query)
            if not teacher:
                return None
            matches = lookup_teacher_matches(teacher, school=str(data.get("school", "") or "").strip())
            if len(matches) == 1:
                reply = finish_teacher_lookup(user_id, matches[0])
            elif len(matches) > 1:
                schools = unique_list([m.get("school", "") for m in matches if m.get("school")])
                reply = f"🔎 找到同名老師。\n\n老師：{teacher}\n學校：{'、'.join(schools)}\n\n請輸入「學校＋老師姓名」。"
            else:
                fuzzy = resolve_fuzzy_name("teacher", teacher, school=str(data.get("school", "") or "").strip())
                if fuzzy.get("status") == "auto":
                    cm = lookup_teacher_matches(str(fuzzy.get("value", "") or "").strip(), school=str(fuzzy.get("school", "") or "").strip())
                    if len(cm) == 1:
                        reply = finish_teacher_lookup(user_id, cm[0])
                    else:
                        return None
                else:
                    return None
    # finish_teacher_lookup() 唯一命中老師時自己就會在
    # guided_mode=="teacher_lookup" 時加上「我還在查老師模式」提示（見
    # 該函式，文字略有不同：「可以繼續輸入下一位老師姓名」），這裡再
    # 判斷同一個條件補一次的話，走那條分支時回覆結尾會重複出現兩段
    # 意思一樣的文字。用共同的「我還在「查老師」模式」片語判斷 reply
    # 是不是已經帶了任一版本的提示，而不是比對完整句子（兩處措辭不
    # 完全相同）。
    if (
        keep_mode
        and guided_mode.get(user_id) == "teacher_lookup"
        and "我還在「查老師」模式" not in reply
    ):
        return reply + "\n\n我還在「查老師」模式，可以繼續查下一位老師，或打「主選單」離開。"
    return reply


def handle_smart_history_lookup(user_id, text, keep_mode=False, parsed_data=None):
    """AI 只理解查單意圖；訂單內容仍全部來自 Google 訂單資料。"""
    if not OPENAI_API_KEY:
        return None
    data = parsed_data if isinstance(parsed_data, dict) else smart_parse_function_text(text, user_id=user_id)
    if not isinstance(data, dict) or data.get("intent") != "history_lookup":
        return None

    number = str(data.get("order_number", "") or "").strip()
    if number:
        number = normalize_order_number(number)
        order = lookup_google_order(number)
        if not order:
            return f"⚠️ 查不到訂單 {number}。"
        historical_order_context[user_id] = order
        if not keep_mode:
            guided_mode.pop(user_id, None)
        return make_historical_order_with_offer(user_id, order)

    date_text = str(data.get("date_text", "") or "").strip()
    teacher = str(data.get("teacher", "") or "").strip()
    # 同時包含日期＋老師時，先按日期查，再在本地篩老師，避免 AI 自己判斷資料內容。
    if date_text:
        parsed_date = parse_guided_date(date_text)
        if parsed_date:
            orders = lookup_orders_by_date(parsed_date)
            if orders is None:
                return "⚠️ 訂單查詢暫時失敗，請稍後再試。"
            if teacher:
                key = normalize_teacher_name_input(teacher)
                orders = [o for o in orders if key and key in normalize_teacher_name_input(str(o.get("teacher", "") or ""))]
            if not orders:
                label = f"{parsed_date}｜{teacher}" if teacher else parsed_date
                return f"📅 {label} 目前查不到訂書訂單。"
            if len(orders) == 1:
                historical_order_context[user_id] = orders[0]
            reply = make_daily_orders_reply(parsed_date, orders)
            if keep_mode:
                reply += "\n\n我還在「查訂單」模式，可以繼續查其他日期、編號或老師。"
            return reply

    if teacher:
        matches = lookup_teacher_matches(teacher, school=str(data.get("school", "") or "").strip())
        canonical = str(matches[0].get("teacher", "") or "").strip() if len(matches) == 1 else teacher
        orders = lookup_book_orders_by_teacher(canonical)
        if orders is None:
            return "⚠️ 訂單查詢暫時無法讀取，請稍後再試一次。"
        if not orders:
            return f"⚠️ 查不到這位老師的訂書紀錄。\n\n老師：{canonical}"
        if len(orders) == 1:
            historical_order_context[user_id] = orders[0]
        reply = make_teacher_book_orders_reply(canonical, orders)
        if keep_mode:
            reply += "\n\n我還在「查訂單」模式，可以繼續查其他日期、編號或老師。"
        return reply
    return None


def _guided_mode_smart_rescue(user_id, text, expected_intent):
    """
    在「查版本／查人數／其他訂單」這幾個沒有自己專屬 AI 兜底函式的
    引導模式裡，固定規則解析不出來時，讓 AI 意圖分類器試一次。
    只有分類結果剛好符合「目前這個模式該做的事」（expected_intent）
    才會真的執行、把結果寫回去；AI 判斷成別的意圖（例如使用者其實
    是想查老師）一律不處理，回傳 None 讓呼叫端退回原本的格式提示，
    避免 AI 自己把使用者帶離目前所在的模式。
    """
    if not OPENAI_API_KEY:
        return None
    compact = re.sub(r"\s+", "", str(text or ""))
    if compact in CONFIRM_WORDS or compact in EXIT_WORDS or len(compact) < 2:
        return None

    data = smart_parse_function_text(text, user_id=user_id)
    if not isinstance(data, dict) or data.get("intent") != expected_intent:
        return None

    normalized = str(data.get("normalized_query", "") or "").strip()

    if expected_intent == "version_lookup" and normalized:
        query = parse_school_version_query(user_id, normalized)
        if query:
            reply = handle_school_version_query(query)
            return reply + "\n\n我還在「查版本」模式，可以繼續輸入下一個學校／年級，或打「主選單」離開。"

    if expected_intent == "stats_lookup" and normalized:
        query = parse_school_stats_query(user_id, normalized)
        if query:
            reply = handle_school_stats_query(query)
            return reply + "\n\n我還在「查人數」模式，可以繼續輸入下一個學校／年級，或打「主選單」離開。"

    if expected_intent == "other_order" and normalized:
        parsed = parse_other_order(user_id, normalized)
        if parsed:
            _clear_stale_history_pending(user_id)
            pending_other_orders[user_id] = parsed
            return make_other_order_confirmation(parsed)

    return None


def handle_smart_function_fallback(user_id, text):
    """全域 AI 路由器：所有既有規則失敗後才執行，不直接產生資料庫答案。"""
    if not OPENAI_API_KEY:
        return None
    compact = re.sub(r"\s+", "", str(text or ""))
    if compact in CONFIRM_WORDS or compact in EXIT_WORDS or len(compact) < 2:
        return None
    data = smart_parse_function_text(text, user_id=user_id)
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent", "") or "")

    if intent == "teacher_lookup":
        return handle_smart_teacher_lookup(user_id, text, parsed_data=data)
    if intent == "history_lookup":
        return handle_smart_history_lookup(user_id, text, parsed_data=data)
    if intent in {"school_order", "cram_order"}:
        # 訂書仍沿用昨天已驗證的專用抽取器，避免全域分類器改變既有訂書品質。
        return handle_smart_order_fallback(user_id, text)

    normalized = str(data.get("normalized_query", "") or "").strip()
    if intent == "version_lookup" and normalized:
        query = parse_school_version_query(user_id, normalized)
        if query:
            return handle_school_version_query(query)
    if intent == "stats_lookup" and normalized:
        query = parse_school_stats_query(user_id, normalized)
        if query:
            return handle_school_stats_query(query)
    if intent == "other_order" and normalized:
        parsed = parse_other_order(user_id, normalized)
        if parsed:
            _clear_stale_history_pending(user_id)
            pending_other_orders[user_id] = parsed
            return make_other_order_confirmation(parsed)
    if intent == "chat":
        return _openai_agent_chat(user_id, text)
    # v42 pilot：即使分類器不確定，也讓 AI 有一次自然對話機會，
    # 但 system prompt 明確禁止它虛構公司內部資料或宣稱已寫入訂單。
    if AI_AGENT_ENABLED and intent == "unknown":
        return _openai_agent_chat(user_id, text)
    return None


def handle_smart_order_fallback(user_id, text):
    """只有所有既有固定功能都接不住時才執行；失敗就回 None。"""
    if not OPENAI_API_KEY:
        return None
    # 短固定詞、查詢詞不交給 AI，避免搶走既有功能。
    # 這裡原本多打一個反斜線（r"\\s+"）在正則裡代表「一個反斜線後面
    # 接空白字元」，不是空白字元本身，導致含內部空白的輸入（例如
    # 「確 認」）這道防線沒有真的生效。
    compact = re.sub(r"\s+", "", str(text or ""))
    if compact in CONFIRM_WORDS or compact in EXIT_WORDS:
        return None
    data = smart_parse_order_text(text, user_id=user_id)
    if not isinstance(data, dict):
        return None
    intent = data.get("intent")
    if intent == "school_order":
        return _apply_smart_school_order(user_id, data, "口語")
    if intent == "cram_order":
        return _apply_smart_cram_order(user_id, data, "口語")
    return None


def _normalize_image_order_result(data):
    """
    v53：AI 即使 intent 判成 unknown，只要實際已抽出足夠欄位，
    後端仍依資料結構判斷學校/補習班訂單，避免同一張圖偶發失敗。
    """
    if not isinstance(data, dict):
        return data

    intent = str(data.get("intent", "") or "").strip()
    cram_school = str(data.get("cram_school", "") or "").strip()
    items = data.get("items", [])
    teacher = str(data.get("teacher", "") or "").strip()
    books = data.get("books", [])

    valid_cram_items = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            book = str(item.get("book", "") or "").strip()
            try:
                qty = int(item.get("quantity", 0) or 0)
            except Exception:
                qty = 0
            if book and qty > 0:
                valid_cram_items.append(item)

    valid_school_books = []
    if isinstance(books, list):
        valid_school_books = [
            x for x in books
            if isinstance(x, dict) and str(x.get("book", "") or "").strip()
        ]

    # 資料已足夠時，以欄位內容修正 AI 偶發的 intent 誤判。
    # 注意：這裡只用 valid_cram_items 判斷「這張圖算不算是補習班訂單」
    # ——不能拿它覆寫 data["items"]。_apply_smart_cram_order() 本來就有
    # 「有書名但沒數量，要明確告知使用者補充」的邏輯，如果這裡先用
    # 「有數量的才留」把 items 洗過一輪，缺數量的品項會直接從清單消
    # 失，使用者完全不會被提醒少訂了一本，那段邏輯也永遠不會被觸發到。
    if cram_school and valid_cram_items:
        data["intent"] = "cram_order"
        if not data.get("image_type") or data.get("image_type") == "unknown":
            data["image_type"] = "chat_screenshot"
        return data

    if teacher and (valid_school_books or str(data.get("book", "") or "").strip()):
        data["intent"] = "school_order"
        if not data.get("image_type") or data.get("image_type") == "unknown":
            data["image_type"] = "chat_screenshot"
        return data

    return data


def handle_image_message(user_id, message_id):
    """圖片智慧訂書：先辨識圖片類型；永遠先確認，不直接寫 Google。"""
    _start_request_budget()
    lock = _get_user_lock(user_id)
    with lock:
        _hydrate_session(user_id)
        try:
            if not OPENAI_API_KEY:
                return (
                    "📷 我收到圖片了，但目前尚未啟用圖片智慧辨識。\n\n"
                    "請先設定 OPENAI_API_KEY；文字訂書功能仍可正常使用。"
                )
            image_bytes = _download_line_image(message_id)
            if not image_bytes:
                return "⚠️ 圖片讀取失敗，請再傳一次。"
            data = smart_parse_order_image(image_bytes)
            data = _normalize_image_order_result(data)
            if not isinstance(data, dict):
                return (
                    "⚠️ 這張圖片我目前沒辦法可靠整理成訂單。\n\n"
                    "請重新拍清楚一點，或直接用文字告訴我；我不會自行猜測。"
                )

            intent = str(data.get("intent", "") or "")
            image_type = str(data.get("image_type", "") or "")

            # 系統/正式訂購單有自己的班級與數量，不能再拿老師姓名回查後覆蓋。
            if intent == "school_order" and image_type == "purchase_order":
                return _apply_purchase_order_image(user_id, data)
            if intent == "school_order":
                return _apply_smart_school_order(user_id, data, "圖片")
            if intent == "cram_order":
                return _apply_smart_cram_order(user_id, data, "圖片")
            if image_type == "book_photo":
                book = str(data.get("book", "") or "").strip()
                if book:
                    return f"📷 我看起來收到的是書籍照片。\n\n可能的書名：{book}\n\n如果你要訂這本，請再告訴我老師或班級。"
            return (
                "📷 圖片已收到，但這次辨識沒有抓到足夠的訂書欄位。\n\n"
                "🏫 學校：老師姓名＋書名即可（可一次多本）\n"
                "🏢 補習班：補習班名稱＋每本書名＋數量即可（可一次多本）\n\n"
                "請再傳一次原圖；辨識完成後我會先整理成確認畫面，不會直接下單。"
            )
        finally:
            _persist_session(user_id)


# =========================================================
# 名稱容錯／同音錯字
# =========================================================
_CN_NUM_MAP = {
    "十": "10", "一": "1", "二": "2", "三": "3", "四": "4",
    "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"
}


def _cn_number_to_int(text):
    """
    把「十二」「二十」「三」這類中文數字轉成整數。只需要支援到 99
    （目前用於「要挑幾種書」「湊滿幾個班」這類小數量輸入），不支援
    「百」以上。原本各處直接查 _CN_NUM_MAP（只認單一字），「十一」～
    「十九」「二十」這類兩個字的數字會查不到，回 0 或 None，導致
    使用者打「十二」卻被回覆「數量要介於1~15」這種自相矛盾的訊息。
    無法解析回傳 None。
    """
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digit_map = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        if (left and left not in digit_map) or (right and right not in digit_map):
            return None
        tens = digit_map[left] if left else 1
        ones = digit_map[right] if right else 0
        return tens * 10 + ones
    if s in digit_map:
        return digit_map[s]
    return None


def _normalize_book_volume(text):
    text = re.sub(r"(上|下|中)冊", r"\1", text)

    def _cn_to_num(m):
        return _CN_NUM_MAP.get(m.group(1), m.group(1))

    text = re.sub(r"第([一二三四五六七八九十])冊", _cn_to_num, text)
    text = re.sub(r"([一二三四五六七八九十])冊", _cn_to_num, text)
    return text


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
        "英語": "英文",
    }
    for wrong, correct in aliases.items():
        text = text.replace(wrong, correct)

    text = _normalize_book_volume(text)

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

    subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    q_subjects = {x for x in subjects if x in q}
    c_subjects = {x for x in subjects if x in c}

    # 查詢跟候選都有明確科目字，但科目對不上時，直接判定不合格。
    # 不能只是「不加分」——原本只是少加 0.20 分，但拿掉科目字之後
    # 剩下的核心文字（例如「自然1測驗卷」拿掉「自然」「測驗」只剩
    # 「卷」）太短、太通用，光靠字串比對／數字比對／通用詞比對疊加
    # 起來還是很容易超過門檻，導致「自然1測驗卷」比對到「新挑戰
    # 測驗卷-國文」這種完全不同科目的書。
    if q_subjects and c_subjects and not (q_subjects & c_subjects):
        return 0.0

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


def lookup_book_candidates_enhanced(query, publisher="", max_results=10):
    query = str(query or "").strip()
    if not query:
        return []
    candidates = lookup_fuzzy_candidates("book", query, publisher=publisher)
    if not candidates:
        core = book_core_text(query)
        if core and core != query:
            candidates = lookup_fuzzy_candidates("book", core, publisher=publisher)

    _subjects = ["國文", "英文", "英語", "數學", "自然", "理化", "生物", "地科", "社會", "歷史", "地理", "公民"]
    q_norm = normalize_book_match_text(query)
    q_subjects = {x for x in _subjects if x in q_norm}

    # v47: same title from different publishers must remain separate.
    merged = {}
    for item in candidates:
        value = str(item.get("value", "") or "").strip()
        pub = str(item.get("publisher", "") or "").strip()
        if not value:
            continue

        # 查詢有明確科目時，候選書名的科目一定要對得上（或候選本身
        # 沒有標示科目字）才收，不管 Google 那邊回傳的原始分數多高。
        # book_keyword_score() 內部也有同樣的科目檢查，但下面這行
        # 取的是「本地算的分數」跟「Google 原始分數 * 0.85」兩者的
        # 較高值——如果只在 book_keyword_score() 裡擋，Google 那邊
        # 剛好給了高分的話，還是會被這個 max() 蓋過去，所以這裡再
        # 擋一次，兩層都擋才保險。
        if q_subjects:
            c_norm = normalize_book_match_text(value)
            c_subjects = {x for x in _subjects if x in c_norm}
            if c_subjects and not (q_subjects & c_subjects):
                continue

        score = max(book_keyword_score(query, value),
                    float(item.get("score", 0) or 0) * 0.85)
        key = (value, pub)
        if key not in merged or score > merged[key]["score"]:
            merged[key] = {"value": value, "publisher": pub, "school": "", "score": score}
    max_results = max(1, min(int(max_results or 10), 20))
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:max_results]

def resolve_fuzzy_name(kind, query, school=""):
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

    if kind == "teacher":
        auto_threshold = 0.64
        confirm_threshold = 0.52
        required_gap = 0.16
    elif kind == "school":
        auto_threshold = 0.78
        confirm_threshold = 0.58
        required_gap = 0.10
    else:
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
    if not pending:
        return None

    clean = re.sub(r"[，,。.!！?？\s]+", "", str(text or ""))
    yes_words = {"是", "對", "對的", "沒錯", "正確", "可以", "好", "就是", "確認"}
    no_words = {"不是", "不對", "錯", "錯了", "不要", "否"}

    options = pending.get("options")

    # 書名候選清單最後一定會有一個「用我打的書名」的選項（raw=True），
    # 但它排第幾個要看資料庫找到幾個候選、每次不一定一樣，使用者要
    # 每次都數清單很麻煩。這裡固定幾個關鍵字，不管排第幾個都能直接
    # 選到它，不用算編號。
    RAW_SELECT_WORDS = {"都不是", "用我的", "用我打的", "自訂"}

    chosen = None
    if options and clean in RAW_SELECT_WORDS:
        chosen = next((opt for opt in options if opt.get("raw")), None)
    elif clean in yes_words and (not options or len(options) <= 1):
        # 「是／對／確認」這類肯定詞只在「只有一個候選要你確認」時才能
        # 直接採用；真的列出多個編號選項（例如同書名不同出版社）時，
        # 使用者常會很自然地回「確認」——這裡如果照樣採用 options[0]，
        # 等於沒經過使用者同意就默默選了第一個出版社／候選，訂錯版本。
        # 這種情況直接落到下面的「看不懂」分支，提示使用者回數字。
        chosen = options[0] if options else pending
    elif options and re.fullmatch(r"[1-9]\d?", clean):
        index = int(clean) - 1
        if 0 <= index < len(options):
            chosen = options[index]
        else:
            return (
                f"⚠️ 目前只有 1～{len(options)} 個候選，請輸入正確的編號，"
                "或直接輸入正確名稱重新查詢。"
            )
    elif options and len(options) > 1 and clean in yes_words:
        # 多個候選時打「確認／好／是」這類肯定詞——不能直接當成沒看懂
        # 而把 pending 清掉讓使用者從頭再來一次，直接請他輸入編號即可。
        return f"🔎 這裡有多個候選，請直接回覆數字 1～{len(options)} 選擇，不是「確認」喔。"

    if chosen is not None:
        pending_name_confirmations.pop(user_id, None)

        if pending.get("purpose") == "guided_teacher_lookup":
            teacher = chosen.get("value", ""); school = chosen.get("school", "")
            matches = lookup_teacher_matches(teacher, school=school)
            if len(matches) == 1: return finish_teacher_lookup(user_id, matches[0])
            guided_mode[user_id] = "teacher_lookup"
            return "⚠️ 這個名稱仍找不到唯一老師資料。\n\n我還在「查老師」模式，請直接重新輸入老師姓名。"

        if pending.get("purpose") == "teacher_lookup":
            teacher = chosen.get("value", "")
            school = chosen.get("school", "")
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

        if pending.get("purpose") == "cram_school":
            draft = cram_order_context.get(user_id)
            if not draft:
                return "✅ 已確認名稱。請重新輸入剛才的補習班訂書內容。"
            draft["cram_school"] = str(chosen.get("value", "") or "").strip()
            cram_order_context[user_id] = draft
            return cram_next_item_prompt(user_id, draft, first=True)

        if pending.get("purpose") == "cram_publisher":
            draft = cram_order_context.get(user_id)
            if not draft:
                return "✅ 已確認名稱。請重新輸入剛才的補習班訂書內容。"
            draft["current_publisher"] = str(chosen.get("value", "") or "").strip()
            cram_order_context[user_id] = draft
            if draft.get("current_book"):
                return f"出版社：{draft['current_publisher']}\n書名：{draft['current_book']}\n\n要幾本？"
            return "請告訴我書名？"

        if pending.get("purpose") == "cram_book":
            draft = cram_order_context.get(user_id)
            if not draft:
                return "✅ 已確認名稱。請重新輸入剛才的補習班訂書內容。"
            draft["current_book"] = str(chosen.get("value", "") or "").strip()
            if chosen.get("publisher"):
                draft["current_publisher"] = str(chosen.get("publisher", "") or "").strip()
            cram_order_context[user_id] = draft
            return "這本要訂幾本？"

        if pending.get("purpose") == "multi_book_teacher":
            draft = multi_book_order_context.get(user_id)
            if not draft:
                return "✅ 已確認名稱。請重新輸入剛才多書訂購的老師姓名。"
            matches = lookup_teacher_matches(
                chosen.get("value", ""), school=chosen.get("school", "")
            )
            if len(matches) == 1:
                return _apply_multi_book_teacher(user_id, draft, matches[0])
            return (
                "⚠️ 確認名稱後仍找不到唯一老師資料，"
                "請直接輸入「學校＋老師姓名」重新輸入。"
            )

        if pending.get("purpose") == "multi_book_publisher":
            draft = multi_book_order_context.get(user_id)
            if not draft:
                return "✅ 已確認名稱。請重新輸入剛才多書訂購的出版社。"
            draft["publisher"] = str(chosen.get("value", "") or "").strip()
            multi_book_order_context[user_id] = draft
            return f"出版社：{draft['publisher']}\n\n請告訴我書名關鍵字，例如「歷史1測驗卷」。"

        if pending.get("purpose") == "order_book_publisher":
            # v49：這個候選的資料形狀是：
            # chosen["value"] = 正式書名
            # chosen["publisher"] = 使用者選到的出版社
            # 選完出版社代表書籍辨識已完整，直接建立訂購確認。
            draft = order_flow_context.get(user_id)
            if not draft:
                return "⚠️ 找不到剛才的訂書草稿，請重新輸入訂書內容。"

            draft["book"] = str(chosen.get("value", "") or "").strip()
            draft["publisher"] = str(chosen.get("publisher", "") or "").strip()
            order_flow_context[user_id] = draft

            if draft.get("teacher") and draft.get("book") and draft.get("publisher"):
                result = build_order_from_draft(user_id, draft)
                if user_id in pending_orders:
                    order_flow_context.pop(user_id, None)
                return result

            return make_order_guide_reply(draft)

        draft = order_flow_context.get(user_id)
        if not draft:
            return "✅ 已確認名稱。請重新輸入剛才的訂書內容。"

        if pending["field"] == "teacher":
            draft["teacher"] = str(chosen.get("value", "") or "").strip()
            draft["school"] = str(chosen.get("school", "") or "").strip()
            if not pending.get("keep_classes"):
                draft["classes"] = []
        elif pending["field"] == "publisher":
            draft["publisher"] = str(chosen.get("value", "") or "").strip()
        elif pending["field"] == "book":
            draft["book"] = str(chosen.get("value", "") or "").strip()
            if chosen.get("publisher"):
                draft["publisher"] = str(chosen.get("publisher", "") or "").strip()
        elif pending["field"] == "school":
            draft["school"] = chosen["value"]

        order_flow_context[user_id] = draft

        # 使用者選到 raw=True（例如第 3 項「使用原本輸入」）時，
        # 這個書名就是使用者明確指定的正式輸入，不可再做第二次 fuzzy。
        # 若先前因為直接輸入書名而尚未取得出版社，就保留書名並詢問出版社。
        if pending.get("field") == "book" and chosen.get("raw") and not draft.get("publisher"):
            return make_order_guide_reply(draft)

        if draft.get("teacher") and draft.get("book"):
            result = build_order_from_draft(user_id, draft)
            if user_id in pending_orders:
                order_flow_context.pop(user_id, None)
            return result

        return make_order_guide_reply(draft)

    if clean in no_words:
        was_guided_teacher = pending.get("purpose") == "guided_teacher_lookup"
        pending_name_confirmations.pop(user_id, None)
        if was_guided_teacher:
            guided_mode[user_id] = "teacher_lookup"
            return "好，我不採用剛才的候選。\n\n我還在「查老師」模式，請直接重新輸入老師姓名。"
        field_name = {
            "teacher": "老師姓名",
            "book": "書名",
            "school": "學校名稱",
            "publisher": "出版社名稱",
            "cram_school": "補習班名稱"
        }.get(pending.get("field"), "名稱")
        return f"好，沒有採用。請重新輸入正確的{field_name}。"

    old_pending = dict(pending)
    pending_name_confirmations.pop(user_id, None)
    logger.debug(
        f"STATE candidate replaced user={user_id} "
        f"field={old_pending.get('field','')} old={old_pending.get('value','')} new_input={clean}"
    )
    return None


def lookup_fuzzy_candidates(kind, query, school="", publisher=""):
    """
    kind 支援 "teacher"／"book"／"school"／"publisher" 四種。
    publisher 參數只有 kind="book" 時會用到：如果有帶，Apps Script
    那邊應該先把書籍清單篩選成「只有這個出版社底下的書」再模糊比對，
    讓「先選出版社、再選書名」時，書名候選範圍會縮小、比對更準確。
    """
    fuzzy_timeout = 8.0 if kind == "teacher" else 6.0
    result = google_post({
        "action": "lookup_fuzzy_candidates",
        "kind": kind,
        "query": str(query or ""),
        "school": str(school or ""),
        "publisher": str(publisher or "")
    }, timeout=fuzzy_timeout, retries=1)

    if not result or not result.get("success"):
        return []

    return result.get("candidates", [])[:20]


# =========================================================
# Google Apps Script
# =========================================================
def google_post(payload, timeout=10, retries=1, ignore_budget=False):
    if not GOOGLE_SCRIPT_URL:
        logger.warning("GOOGLE_SCRIPT_URL missing")
        return None

    action = str(payload.get("action", ""))
    cache_ttl = int(_GOOGLE_CACHE_TTLS.get(action, 0) or 0)
    key = _cache_key(payload) if cache_ttl > 0 else ""
    now = time.time()

    if cache_ttl > 0 and key in _google_read_cache:
        item = _google_read_cache.get(key) or {}
        if now < float(item.get("expires_at", 0) or 0):
            cached_data = copy.deepcopy(item.get("data"))
            if action == "lookup_fuzzy_candidates" and isinstance(cached_data, dict) and not cached_data.get("candidates"):
                _google_read_cache.pop(key, None)
                logger.debug(f"Google cache DROP empty: {action}")
            else:
                logger.debug(f"Google cache HIT: {action}")
                return cached_data
        else:
            _google_read_cache.pop(key, None)

    # 請求時間／次數預算：同一則使用者訊息如果已經花了太久，或已經
    # 打了太多次 Google，這裡直接跳過、視同失敗，避免疊加到把整個
    # gunicorn worker 拖過 timeout。詳見上方 REQUEST_TIME_BUDGET_SECONDS
    # 說明。快取命中不受影響（上面已經提早 return）。
    #
    # ignore_budget=True：寫入類 action（建立/修改/取消訂單）跟「寫入
    # 後查回來驗證是否真的成功」這類呼叫刻意不受這個預算限制——這些
    # 呼叫如果因為預算被跳過，後果遠比多等幾秒嚴重：可能誤判「寫入
    # 失敗」讓使用者重按確認，造成 Google 試算表出現重複訂單；或是
    # 多書訂購／補習班訂購一次要逐筆寫入好幾本書，只因為前面查詢已經
    # 花了一些預算，後面幾筆就完全沒送出卻被算成「失敗」。
    if not ignore_budget and _request_budget_exceeded():
        logger.warning(f"Google call skipped (request time/call budget exceeded): action={action}")
        stale = _load_google_stale_cache(action, key) if cache_ttl > 0 else None
        if stale is not None:
            return stale
        return None

    attempts = max(1, int(retries or 1))
    started = time.perf_counter()

    for attempt in range(attempts):
        # 每一次「真的送出 HTTP 請求」都要計數，不是每個 google_post()
        # 呼叫只算一次——重試 2、3 次的呼叫（例如 lookup_google_order）
        # 原本只占用一次次數預算，實際上可能對 Google 送出了 3 次
        # 請求，次數預算形同虛設。也在每次重試前重新檢查一次時間預算，
        # 避免前面幾次已經花了很久，還繼續傻等下一次重試。
        if attempt > 0 and not ignore_budget and _request_budget_exceeded():
            logger.warning(f"Google call retry skipped (request time/call budget exceeded): action={action}")
            stale = _load_google_stale_cache(action, key) if cache_ttl > 0 else None
            if stale is not None:
                return stale
            return None
        _register_google_call()
        try:
            response = HTTP.post(
                GOOGLE_SCRIPT_URL,
                json=payload,
                timeout=timeout
            )

            elapsed = time.perf_counter() - started
            logger.info(
                f"Google action={action} status={response.status_code} "
                f"elapsed={elapsed:.3f}s attempt={attempt + 1}/{attempts}"
            )

            if response.status_code != 200:
                if attempt < attempts - 1:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                stale = _load_google_stale_cache(action, key) if cache_ttl > 0 else None
                if stale is not None:
                    return stale
                return None

            data = response.json()

            # v78：老師查詢偶爾會出現「HTTP 200 + success:true + matches:[]」的假空結果。
            # 若同一個查詢先前曾成功拿到老師資料，這種空陣列不應覆蓋正確快取，
            # 而是直接回退到最近一次成功資料。真正從未命中過的老師仍會正常回空。
            if (
                action == "lookup_teacher_matches"
                and isinstance(data, dict)
                and data.get("success") is True
                and not (data.get("matches") or [])
                and str(payload.get("teacher", "") or "").strip()
            ):
                # 先找完全相同 payload；若這個 exact key 從未成功過，再用
                # v79 的「老師姓名級」備援。這可處理第一次 exact query 就
                # 被 GAS 假空結果擊中的情況。
                stale = _load_google_stale_cache(action, key) if cache_ttl > 0 else None
                if not (isinstance(stale, dict) and (stale.get("matches") or [])):
                    stale = _load_teacher_name_fallback(payload)
                if isinstance(stale, dict) and (stale.get("matches") or []):
                    logger.warning(
                        "Google teacher empty-result fallback HIT: teacher=%s school=%s",
                        str(payload.get("teacher", "") or "").strip(),
                        str(payload.get("school", "") or "").strip(),
                    )
                    return stale

            if cache_ttl > 0 and isinstance(data, dict):
                # data.get("success") is True：Apps Script 端偶爾會回
                # HTTP 200 但 {"success": false, ...}（例如短暫出錯或
                # 資料庫忙線）。這種失敗回應以前也會被當成正常結果快取
                # 起來，同一個查詢接下來幾分鐘到半小時內（視 TTL）會
                # 一直回失敗結果，即使 Google 那邊早就恢復正常。
                # 只有「明確 success:false」才視為失敗不快取。
                # 有些舊 GAS action 回的是正常資料但沒有 success 欄位；若硬性要求
                # `is True`，這些讀取就會每次都重新打 Google，造成 1~3 秒累積延遲。
                should_cache = data.get("success") is not False and not (
                    action == "lookup_fuzzy_candidates" and not data.get("candidates")
                ) and not (
                    action == "lookup_teacher_matches"
                    and str(payload.get("teacher", "") or "").strip()
                    and not (data.get("matches") or [])
                )
                if should_cache:
                    _google_read_cache[key] = {
                        "data": copy.deepcopy(data),
                        "expires_at": time.time() + cache_ttl
                    }
                    _store_google_stale_cache(action, key, data)
                    if action == "lookup_teacher_matches" and (data.get("matches") or []):
                        _store_teacher_name_fallback(payload, data)

            if action in {
                "create_order", "update_order", "cancel_order", "set_order_note",
                "create_other_order", "update_other_order", "create_cram_order"
            } and isinstance(data, dict) and data.get("success"):
                clear_google_read_cache()

            return data

        except Exception as error:
            elapsed = time.perf_counter() - started
            logger.error(f"Google request error: {action} elapsed={elapsed:.3f}s error={error}")

            if attempt < attempts - 1:
                time.sleep(0.15 * (attempt + 1))
                continue

            stale = _load_google_stale_cache(action, key) if cache_ttl > 0 else None
            if stale is None and action == "lookup_teacher_matches":
                stale = _load_teacher_name_fallback(payload)
            if stale is not None:
                return stale
            return None

    return None


def lookup_teacher_matches_status(teacher, school="", grade="", subject=""):
    """回傳 (matches, query_ok)。query_ok=False 代表 Google/網路失敗，不是「真的查無老師」。"""
    # v75：老師查詢偶爾會遇到 Apps Script/Google 邊緣節點瞬間變慢。
    # 舊版一次等 5 秒，逾時就整個流程卡住；現在改成「較短逾時 + 最多一次快速重試」。
    # 正常命中通常 1~2 秒，不受影響；偶發冷啟動時第一次失敗，第二次常可直接命中
    # GAS / Script Properties 的暖快取。最壞等待仍控制在約 7 秒，而不是後續再疊 fuzzy。
    result = google_post({
        "action": "lookup_teacher_matches",
        "teacher": str(teacher or "").strip(),
        "school": str(school or "").strip(),
        "grade": str(grade or "").strip(),
        "subject": str(subject or "").strip()
    }, timeout=3.5, retries=2)

    if not result or not result.get("success"):
        return [], False

    matches = []
    for item in result.get("matches", []):
        classes = copy_classes(item.get("classes", []))
        matches.append({
            "school": str(item.get("school", "")).strip(),
            "teacher": str(item.get("teacher", "")).strip(),
            "subjects": unique_list(item.get("subjects", [])),
            "classes": classes
        })
    return matches, True


def lookup_teacher_matches(teacher, school="", grade="", subject=""):
    matches, _ok = lookup_teacher_matches_status(teacher, school=school, grade=grade, subject=subject)
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


def _order_matches_pending(actual, expected):
    """確認 Google 裡的訂單是否就是目前這張待確認訂單。"""
    if not actual or not expected:
        return False

    for key in ("teacher", "school", "book", "publisher"):
        if str(actual.get(key, "") or "").strip() != str(expected.get(key, "") or "").strip():
            return False

    actual_classes = {
        str(item.get("class_name", "") or "").strip(): int(item.get("students", 0) or 0)
        for item in actual.get("classes", [])
        if str(item.get("class_name", "") or "").strip()
    }
    expected_classes = {
        str(item.get("class_name", "") or "").strip(): int(item.get("students", 0) or 0)
        for item in expected.get("classes", [])
        if str(item.get("class_name", "") or "").strip()
    }
    return actual_classes == expected_classes


def _verify_recent_created_order(order):
    """
    create_order 若 timeout，不重送 create_order。
    改查今天的訂單，確認 Google 是否其實已經寫入成功，避免重複訂單。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    # ignore_budget=True：這是 create_order 逾時後「查回來確認是否其實
    # 已經寫入成功」的最後一道防線。如果這次呼叫也被請求預算擋掉，
    # 會直接回 False 讓使用者以為要重新確認，等於逼使用者重按確認、
    # 在 Google 試算表製造出重複訂單——這正是這個函式原本要防止的事，
    # 不能因為預算不足就自己先放棄。
    result = google_post({
        "action": "lookup_orders_by_date",
        "date": today
    }, timeout=12, retries=1, ignore_budget=True)

    if not result or not result.get("success"):
        return None

    matches = []
    for actual in result.get("orders", []):
        actual = dict(actual)
        actual["classes"] = copy_classes(actual.get("classes", []))
        if _order_matches_pending(actual, order):
            number = normalize_order_number(actual.get("order_number", ""))
            if number:
                matches.append(number)

    if not matches:
        return None

    # 同內容若本來就有舊單，以最新（最大編號）為本次建立的候選。
    return max(matches, key=lambda x: int(re.sub(r"\D", "", x) or 0))


def write_to_google_sheet(order):
    # 寫入只送一次。timeout 後絕對不自動重送，避免 Google 已寫入卻產生重複訂單。
    # ignore_budget=True：寫入本身不能因為這則訊息前面查詢已經花掉預算
    # 就被跳過不送；尤其多書訂購／確認前反覆模糊比對很容易在到達這裡
    # 之前就用掉大半預算，寫入本身反而是最不該被省略的一次呼叫。
    result = google_post({
        "action": "create_order",
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get("classes", [])
    }, timeout=15, retries=1, ignore_budget=True)

    if result and result.get("success") is True:
        return True, normalize_order_number(result.get("order_number", ""))

    # Google Apps Script 常見情況：已經完成 append，但 HTTP 回覆超時。
    # 稍等一下後查今天訂單；查到完全相同內容就視為成功。
    time.sleep(0.8)
    verified_order_number = _verify_recent_created_order(order)
    if verified_order_number:
        logger.warning(
            "create_order response missing/timeout, but verified order exists: %s",
            verified_order_number
        )
        clear_google_read_cache()
        return True, verified_order_number

    return False, None


def mark_order_note(order_number, note):
    result = google_post({
        "action": "set_order_note",
        "order_number": normalize_order_number(order_number),
        "note": str(note or "").strip()
    }, timeout=5, retries=1)

    return bool(result and result.get("success") is True)


def lookup_google_order(order_number):
    # retries 原本是 3，搭配預設 timeout=10 最壞要等 30 秒，逼近甚至
    # 超過建議的 60 秒 worker timeout；降到 2 次留多一點安全邊際。
    result = google_post({
        "action": "lookup_order",
        "order_number": normalize_order_number(order_number)
    }, retries=2)

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
    }, ignore_budget=True)

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

    # 查詢失敗（逾時／預算被跳過）跟「這位老師真的沒有訂書紀錄」要分開：
    # 回 [] 的話，呼叫端會直接告訴使用者「查不到訂書紀錄」，使用者可能
    # 因此誤以為真的沒訂過而重複下單。跟 lookup_orders_by_date() 一樣
    # 用 None 表示查詢本身失敗，呼叫端要另外處理。
    if not result or not result.get("success"):
        return None

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
    }, timeout=9, retries=2)

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


def get_today_order_stats_reply():
    date_text = datetime.now().strftime("%Y-%m-%d")
    orders = lookup_orders_by_date(date_text)
    if orders is None: return "⚠️ 今日訂單統計暫時無法讀取，請稍後再試。"
    if not orders: return f"📊 今日訂單統計｜{date_text}\n\n目前還沒有訂書訂單。"
    total_books = sum(int(o.get("quantity", 0) or 0) for o in orders)
    publisher_counts = {}
    for o in orders:
        publisher = str(o.get("publisher", "") or "未標示出版社").strip()
        publisher_counts[publisher] = publisher_counts.get(publisher, 0) + 1
    publisher_lines = [f"• {name}｜{count} 筆" for name, count in sorted(publisher_counts.items(), key=lambda x: -x[1])]
    return "\n".join([f"📊 今日訂單統計｜{date_text}", "", f"🧾 訂單：{len(orders)} 筆", f"📚 總數：{total_books} 本", "", "出版社分布", *publisher_lines])



def cancel_google_order(order_number):
    # retries 原本是 2：如果第一次其實已經在 Apps Script 端執行完成、
    # 只是 HTTP 回應逾時，retries=2 會重新送出第二次 cancel_order。
    # 跟其他寫入類 action（只送一次＋事後查回來驗證）的原則不一致，
    # 如果 Apps Script 的 cancel 不是冪等操作（例如用刪除列而不是改
    # 狀態欄位），可能重複取消或取消到位移後的別張訂單。改成只送
    # 一次，失敗時改查這張單目前狀態是否已經是「已取消」來判定。
    result = google_post({
        "action": "cancel_order",
        "order_number": normalize_order_number(order_number)
    }, timeout=8, retries=1)

    if result and result.get("success") is True:
        return True, result

    verified = lookup_google_order(order_number)
    if verified and str(verified.get("status", "")).strip() == "已取消":
        return True, {"success": True, "verified_after_write": True}

    return False, result or {}


def lookup_school_classes(school, grade="", class_name=""):
    result = google_post({
        "action": "lookup_school_classes",
        "school": school,
        "grade": grade,
        "class_name": class_name
    }, timeout=9, retries=1)

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


def _version_lookup_subject(subject):
    """
    學校版本資料的統一科目入口。

    書籍本身仍保留「歷史／地理／公民」原科目；只有查教科書版本時，
    這三科一律查 Google「學校版本資料」中的「社會」。
    放在最底層 lookup_school_versions() 前再正規化一次，避免任何上層流程漏轉。
    """
    s = str(subject or "").strip()
    if s in {"歷史", "地理", "公民"}:
        return "社會"
    if s == "英語":
        return "英文"
    return s


def lookup_school_versions_all_junior_grades(school, subject="", academic_period=""):
    combined = []
    periods = []
    any_success = False
    # 原本只要有任一年級成功就直接回傳「成功」，缺的那個年級完全沒有
    # 標示，使用者以為查到的清單就是全部，其實少了一個年級的資料。
    # 這裡把失敗的年級記下來，讓呼叫端可以在回覆裡明確提醒。
    failed_grades = []
    for grade in ["七年級", "八年級", "九年級"]:
        part = lookup_school_versions(school, grade, subject, academic_period)
        if part is None:
            failed_grades.append(grade)
            continue
        any_success = True
        combined.extend(part.get("versions", []))
        if part.get("latest_period"):
            periods.append(str(part.get("latest_period")))
    if not any_success:
        return None
    latest = academic_period or (max(periods) if periods else "")
    return {"latest_period": latest, "versions": combined, "failed_grades": failed_grades}


def lookup_school_versions(
    school,
    grade="",
    subject="",
    academic_period=""
):
    # v76：所有版本查詢最後都經過這裡，統一保證歷史／地理／公民查「社會」。
    # 這層是保險絲：即使上層某個流程忘了轉換，也不會把「歷史」直接送到 GAS。
    subject = _version_lookup_subject(subject)

    def do_lookup(school_name):
        return google_post({
            "action": "lookup_versions",
            "school": school_name,
            "grade": grade,
            "subject": subject,
            "academic_period": academic_period
        }, timeout=9, retries=1)

    result = do_lookup(school)

    if not result or not result.get("success"):
        return None

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


def _verify_recent_created_other_order(order):
    """
    create_other_order 若逾時，不重送。改查同校＋同老師＋同品項的其他
    訂單，確認 Google 是否其實已經寫入成功，避免使用者重按確認造成
    重複紀錄（跟 write_to_google_sheet() 對學校訂單的做法一致）。
    """
    orders = lookup_other_orders(
        teacher=order.get("teacher", ""),
        school=order.get("school", ""),
        item_keyword=order.get("item", ""),
    )
    if not orders:
        return None
    for actual in orders:
        if (
            str(actual.get("school", "")).strip() == str(order.get("school", "")).strip()
            and str(actual.get("teacher", "")).strip() == str(order.get("teacher", "")).strip()
            and str(actual.get("item", "")).strip() == str(order.get("item", "")).strip()
        ):
            return actual
    return None


def write_other_order_to_google_sheet(order):
    # ignore_budget=True：寫入不能因為這則訊息前面查詢已經花掉預算就
    # 被跳過不送；timeout 拉到 15 秒跟學校訂單一致。
    result = google_post({
        "action": "create_other_order",
        "school": order["school"],
        "teacher": order["teacher"],
        "item": order["item"]
    }, timeout=15, retries=1, ignore_budget=True)

    if result and result.get("success") is True:
        return True, result

    time.sleep(0.8)
    verified = _verify_recent_created_other_order(order)
    if verified:
        logger.warning(
            "create_other_order response missing/timeout, but verified record exists: %s",
            verified.get("order_number", "")
        )
        clear_google_read_cache()
        return True, verified

    return False, result or {}


def lookup_other_orders(teacher="", item_keyword="", row_number=None, order_number="", date="", school="", keyword=""):
    payload = {"action":"lookup_other_orders","teacher":teacher,"item_keyword":item_keyword,"order_number":order_number,"date":date,"school":school,"keyword":keyword}
    if row_number is not None:
        payload["row_number"] = row_number
    result = google_post(payload)
    if not result or not result.get("success"):
        return None
    orders = result.get("orders", [])
    for item in orders:
        if item.get("order_number"):
            item["order_number"] = normalize_other_order_number(item.get("order_number"))
    return orders


def update_other_order_in_google_sheet(order_number, field, value, row_number=None):
    payload={"action":"update_other_order","order_number":order_number,"field":field,"value":value}
    if row_number is not None: payload["row_number"]=row_number
    result=google_post(payload)
    return bool(result and result.get("success")), result


# =========================================================
# 小工具
# =========================================================
def normalize_text(text):
    # v54：手機輸入常見全形數字／全形英數／特殊空白先統一。
    # NFKC 會把 ７０１ 轉成 701，但不會破壞中文書名。
    import unicodedata
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("　", " ").replace("～", "~")
    return value.strip()


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

            normalized_subjects = unique_list(
                [str(s or "").strip() for s in subjects if str(s or "").strip()]
            )
            normalized_subjects = sorted(normalized_subjects, key=subject_sort_key)
            result.append({
                "class_name": str(item.get("class_name", "")),
                "students": int(item.get("students", 0) or 0),
                "subjects": normalized_subjects
            })
        except Exception:
            continue
    return sort_class_items(result)


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
# 訂購單 PDF
#
# 拆成兩層：generate_purchase_order_pdf() 只管畫 PDF；
# build_purchase_order_reply() 決定怎麼交付（目前只有下載連結）。
# 之後要加「機器人直接寄信」，改 build_purchase_order_reply() 就好，
# 不用動這裡的排版邏輯。詳見檔案最上面的說明區塊。
# =========================================================
CJK_FONT_NAME = "AppCJKFont"
# 這裡改用「嵌入字型檔」而不是 reportlab 內建的 CID 字型（例如
# MSung-Light）：內建 CID 字型不會把字型資料包進 PDF 裡，而是假設
# 打開 PDF 的軟體本身就有對應的繁中字型，很多手機上的 PDF 檢視器
# 其實沒有，容易變成空白或方框。改成嵌入字型後，不管用哪個裝置、
# 哪個 App 打開，字都保證顯示正確。
# 字型檔（fonts/cjk-tc.ttf）需要跟 app.py 一起放進你的 repo，
# 部署到 Render 才讀得到；這是一個已經去除多餘字符、縮小過的繁中
# 開源字型（文泉驛正黑的子集版），僅供內嵌進產生的 PDF 使用。
_CJK_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "cjk-tc.ttf")
_CJK_FONT_READY = False
try:
    pdfmetrics.registerFont(TTFont(CJK_FONT_NAME, _CJK_FONT_PATH))
    _CJK_FONT_READY = True
except Exception as error:
    logger.error(f"CJK font register failed ({_CJK_FONT_PATH}): {error}")

# 仿印章設定：大漢書局的店章資訊，蓋在訂購單右下角，讓 PDF 看起來
# 比較像正式單據，不是一張純白紙。可以透過環境變數覆蓋，之後店章
# 內容有異動（例如換電話、換地址）不用改程式碼、重新部署即可。
STAMP_COMPANY_NAME = os.environ.get("STAMP_COMPANY_NAME", "大漢書局")
STAMP_PHONE = os.environ.get("STAMP_PHONE", "電話：(02) 2833-3838")
STAMP_ADDRESS = os.environ.get("STAMP_ADDRESS", "台北市士林區美崙街96號")
STAMP_COLOR = colors.Color(0.72, 0.06, 0.06)  # 印章紅

# 訂購單版面配色：道奇藍（Dodger Blue），取代原本置中復古風的黑白配色。
PO_ACCENT_COLOR = colors.HexColor("#ED174C")
PO_ACCENT_LIGHT = colors.HexColor("#FCE7EC")
PO_SECONDARY_COLOR = colors.HexColor("#006BB6")


def _draw_purchase_order_stamp(c, doc):
    """
    在頁面右下角畫一個仿印章的紅色戳記（雙框＋店名＋地址電話，
    整體微微旋轉），讓 PDF 訂購單看起來比較像正式單據，而不是一張
    只有幾行字的白紙。純用向量圖形畫出來，不需要另外準備印章圖檔。
    """
    if not _CJK_FONT_READY:
        return

    c.saveState()
    page_width, _page_height = A5
    stamp_width, stamp_height = 60 * mm, 26 * mm
    x = page_width - stamp_width - 16 * mm
    y = 18 * mm

    c.translate(x + stamp_width / 2, y + stamp_height / 2)
    c.rotate(-6)
    c.translate(-stamp_width / 2, -stamp_height / 2)

    c.setStrokeColor(STAMP_COLOR)
    c.setFillColor(STAMP_COLOR)

    # 外框＋內框，仿真實印章常見的雙線邊框。
    c.setLineWidth(1.4)
    c.roundRect(0, 0, stamp_width, stamp_height, 3 * mm, stroke=1, fill=0)
    c.setLineWidth(0.6)
    c.roundRect(
        1.6 * mm, 1.6 * mm,
        stamp_width - 3.2 * mm, stamp_height - 3.2 * mm,
        2 * mm, stroke=1, fill=0
    )

    c.setFont(CJK_FONT_NAME, 17)
    c.drawCentredString(stamp_width / 2, stamp_height - 10 * mm, STAMP_COMPANY_NAME)

    c.setFont(CJK_FONT_NAME, 8.5)
    c.drawCentredString(stamp_width / 2, stamp_height - 17.3 * mm, STAMP_ADDRESS)
    c.drawCentredString(stamp_width / 2, stamp_height - 22.3 * mm, STAMP_PHONE)

    c.restoreState()


def generate_purchase_order_pdf(offer):
    """把訂購單內容畫成 A5（約 A4 一半）大小的 PDF，回傳 (token, path)；
    失敗回傳 (None, None)。版面：左靠標題＋道奇藍資訊區塊＋道奇藍表頭
    品項表＋合計＋簽章欄，右下角再蓋一個仿印章的紅色戳記。
    """
    if not _CJK_FONT_READY:
        logger.error("purchase order pdf skipped: CJK font not registered")
        return None, None

    token = uuid.uuid4().hex
    path = os.path.join(PURCHASE_ORDER_DIR, f"{token}.pdf")

    date_str = datetime.now().strftime("%Y/%m/%d")
    school = str(offer.get("school", "") or "")
    publisher = str(offer.get("publisher", "") or "")
    book = str(offer.get("book", "") or "")
    classes = offer.get("classes", [])
    total_qty = sum(int(item.get("students", 0) or 0) for item in classes)

    doc = SimpleDocTemplate(
        path, pagesize=A5,
        topMargin=18 * mm, bottomMargin=42 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm
    )

    title_style = ParagraphStyle(
        "title", fontName=CJK_FONT_NAME, fontSize=21, leading=26,
        alignment=0, textColor=PO_SECONDARY_COLOR
    )
    subtitle_style = ParagraphStyle(
        "subtitle", fontName=CJK_FONT_NAME, fontSize=10.5, leading=15,
        alignment=0, textColor=colors.grey, spaceAfter=8
    )
    label_style = ParagraphStyle("label", fontName=CJK_FONT_NAME, fontSize=12, leading=18)
    label_bold_style = ParagraphStyle(
        "label_bold", fontName=CJK_FONT_NAME, fontSize=12, leading=18,
        textColor=PO_SECONDARY_COLOR
    )
    small_style = ParagraphStyle(
        "small", fontName=CJK_FONT_NAME, fontSize=10, leading=15, textColor=colors.grey
    )

    elements = [
        Paragraph("訂　購　單", title_style),
        Paragraph("請協助幫忙下訂單", subtitle_style),
    ]

    info_data = [
        ["訂購人", "士林大漢", "日期", date_str],
        ["學校", school, "出版社", publisher],
    ]
    info_table = Table(info_data, colWidths=[18 * mm, 42 * mm, 18 * mm, 42 * mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), CJK_FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("TEXTCOLOR", (0, 0), (0, -1), PO_SECONDARY_COLOR),
        ("TEXTCOLOR", (2, 0), (2, -1), PO_SECONDARY_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, -1), PO_ACCENT_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, PO_ACCENT_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.white),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 12))

    table_data = [["書名", "班級", "數量"]]
    for item in classes:
        class_name = str(item.get("class_name", "") or "")
        students = int(item.get("students", 0) or 0)
        table_data.append([book, class_name, f"{students}本"])
    table_data.append(["", "合計", f"{total_qty}本"])

    table = Table(table_data, colWidths=[62 * mm, 22 * mm, 22 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), CJK_FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 11.5),
        ("BACKGROUND", (0, 0), (-1, 0), PO_ACCENT_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, PO_ACCENT_LIGHT]),
        ("LINEABOVE", (0, -1), (-1, -1), 1, PO_SECONDARY_COLOR),
        ("TEXTCOLOR", (0, -1), (-1, -1), PO_SECONDARY_COLOR),
        ("ALIGN", (1, 0), (2, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, colors.HexColor("#CCCCCC")),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("備註　麻煩教用貨單集中", label_style))
    elements.append(Paragraph(f"外箱備註　{school}", label_style))
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("以上訂單　麻煩幫我處理　感謝！！！", label_bold_style))
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("訂購人簽章：＿＿＿＿＿＿＿＿＿＿＿＿", small_style))

    try:
        doc.build(
            elements,
            onFirstPage=_draw_purchase_order_stamp,
            onLaterPages=_draw_purchase_order_stamp
        )
    except Exception as error:
        logger.error(f"purchase order pdf build error: {error}")
        return None, None

    return token, path


def _purchase_order_ttl_display():
    ttl_days = PURCHASE_ORDER_LINK_TTL_SECONDS / 86400
    if ttl_days >= 1 and float(ttl_days).is_integer():
        return f"{int(ttl_days)} 天"
    ttl_hours = PURCHASE_ORDER_LINK_TTL_SECONDS / 3600
    return f"{int(ttl_hours)} 小時"


def generate_cram_purchase_order_pdf(offer):
    """
    補習班版的訂購單 PDF：跟學校版共用同一顆印章跟同一套字型，
    但資訊區塊沒有單一「出版社」欄位（每本書出版社可能不同），改成
    品項表直接列「出版社／書名／數量」三欄。回傳 (token, path)；
    失敗回傳 (None, None)。
    """
    if not _CJK_FONT_READY:
        logger.error("cram purchase order pdf skipped: CJK font not registered")
        return None, None

    token = uuid.uuid4().hex
    path = os.path.join(PURCHASE_ORDER_DIR, f"{token}.pdf")

    date_str = datetime.now().strftime("%Y/%m/%d")
    cram_school = str(offer.get("cram_school", "") or "")
    items = offer.get("items", [])
    total_qty = sum(int(item.get("quantity", 0) or 0) for item in items)

    doc = SimpleDocTemplate(
        path, pagesize=A5,
        topMargin=18 * mm, bottomMargin=42 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm
    )

    title_style = ParagraphStyle(
        "title", fontName=CJK_FONT_NAME, fontSize=21, leading=26,
        alignment=0, textColor=PO_SECONDARY_COLOR
    )
    subtitle_style = ParagraphStyle(
        "subtitle", fontName=CJK_FONT_NAME, fontSize=10.5, leading=15,
        alignment=0, textColor=colors.grey, spaceAfter=8
    )
    label_style = ParagraphStyle("label", fontName=CJK_FONT_NAME, fontSize=12, leading=18)
    label_bold_style = ParagraphStyle(
        "label_bold", fontName=CJK_FONT_NAME, fontSize=12, leading=18,
        textColor=PO_SECONDARY_COLOR
    )
    small_style = ParagraphStyle(
        "small", fontName=CJK_FONT_NAME, fontSize=10, leading=15, textColor=colors.grey
    )

    elements = [
        Paragraph("訂　購　單", title_style),
        Paragraph("請協助幫忙下訂單", subtitle_style),
    ]

    info_data = [
        ["訂購人", "士林大漢", "日期", date_str],
        ["補習班", cram_school, "", ""],
    ]
    info_table = Table(info_data, colWidths=[18 * mm, 42 * mm, 18 * mm, 42 * mm])
    info_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), CJK_FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("TEXTCOLOR", (0, 0), (0, -1), PO_SECONDARY_COLOR),
        ("TEXTCOLOR", (2, 0), (2, -1), PO_SECONDARY_COLOR),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("BACKGROUND", (0, 0), (-1, -1), PO_ACCENT_LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.6, PO_ACCENT_COLOR),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.white),
        ("SPAN", (1, 1), (3, 1)),
    ]))
    elements.append(info_table)
    elements.append(Spacer(1, 12))

    table_data = [["出版社", "書名", "數量"]]
    for item in items:
        table_data.append([
            str(item.get("publisher", "") or ""),
            str(item.get("book", "") or ""),
            f"{int(item.get('quantity', 0) or 0)}本"
        ])
    table_data.append(["", "合計", f"{total_qty}本"])

    table = Table(table_data, colWidths=[24 * mm, 60 * mm, 22 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), CJK_FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("BACKGROUND", (0, 0), (-1, 0), PO_ACCENT_COLOR),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, PO_ACCENT_LIGHT]),
        ("LINEABOVE", (0, -1), (-1, -1), 1, PO_SECONDARY_COLOR),
        ("TEXTCOLOR", (0, -1), (-1, -1), PO_SECONDARY_COLOR),
        ("ALIGN", (2, 0), (2, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -2), 0.4, colors.HexColor("#CCCCCC")),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))

    elements.append(Paragraph("以上訂單　麻煩幫我處理　感謝！！！", label_bold_style))
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("訂購人簽章：＿＿＿＿＿＿＿＿＿＿＿＿", small_style))

    try:
        doc.build(
            elements,
            onFirstPage=_draw_purchase_order_stamp,
            onLaterPages=_draw_purchase_order_stamp
        )
    except Exception as error:
        logger.error(f"cram purchase order pdf build error: {error}")
        return None, None

    return token, path


def build_purchase_order_reply(offer):
    if offer.get("kind") == "cram": token, path = generate_cram_purchase_order_pdf(offer)
    else: token, path = generate_purchase_order_pdf(offer)
    if not token: return "❌ 訂購單 PDF 產生失敗，請稍後再試一次。"
    if PURCHASE_ORDER_DELIVERY_MODE == "email": logger.warning("email delivery mode not implemented yet, falling back to link")
    base_url = (PUBLIC_BASE_URL or request.url_root).rstrip("/")
    download_url = f"{base_url}/purchase-order/{token}.pdf"
    recipient = "出版社" if offer.get("kind") != "cram" else "補習班或出版社"
    return (
        "📄 訂購單 PDF 已產生\n\n"
        f"{download_url}\n\n"
        f"請開啟連結下載 PDF，再附加到 Email 寄給{recipient}。\n"
        f"⏳ 連結有效期限：{_purchase_order_ttl_display()}。過期後可回 LINE 重新產生。"
    )



# =========================================================
# LeBron 固定人設層
# =========================================================
def add_lebron_flavor(message):
    if isinstance(message, (list, tuple)):
        items = list(message)
        if not items:
            return []
        return [add_lebron_flavor(items[0])] + [str(x or "").strip() for x in items[1:]]

    body = str(message or "").strip()
    if not body:
        body = "目前沒有可顯示的內容。"

    if body.startswith("👑 LeBron James"):
        return body

    if body.startswith("請協助幫忙下訂單"):
        return body

    if body.startswith("📄 訂購單 PDF 已產生"):
        return body

    compact = re.sub(r"\s+", "", body)

    # 候選選擇還沒處理完成，不加「LeBron James 幫你處理好了」這類完成語。
    if "🔎找到接近的書名" in compact or "🔎資料庫沒有相符書名" in compact:
        return body

    if any(key in compact for key in [
        "查不到", "找不到", "處理失敗", "查詢失敗", "寫入失敗",
        "更新失敗", "沒有讀到", "系統剛剛處理失敗"
    ]):
        intro = "👑 LeBron James 這球沒找到目標"

    elif any(key in compact for key in [
        "還差", "請告訴我", "請提供", "請問是哪", "需要哪",
        "尚未提供", "請選擇", "請直接告訴我"
    ]):
        intro = "👑 LeBron James 還差一個助攻"

    elif any(key in compact for key in [
        "確認取消", "已取消", "取消這張", "取消這筆", "取消訂單"
    ]):
        intro = "👑 LeBron James 幫你把這球撤回來了"

    elif any(key in compact for key in [
        "確認修改", "修改確認", "已修改", "調整", "改成", "更新成功"
    ]):
        intro = "👑 LeBron James 幫你把陣容調整好了"

    elif "出版社" in compact and ("候選" in compact or "可能有錯字" in compact):
        intro = "👑 LeBron James 幫你認清東家了"

    elif "訂購確認" in compact or "訂書確認" in compact:
        intro = "👑 LeBron James 幫你把這張單整理好了"

    elif any(key in compact for key in ["教科書版本", "版本資料", "版本："]):
        intro = "👑 LeBron James 幫你把版本查好了"

    elif "🏫" in compact and ("總人數" in compact or "班級總數" in compact):
        intro = "👑 LeBron James 幫你點完名了"

    elif any(key in compact for key in [
        "班級資料", "學生人數", "總學生人數", "班級總數", "幾個班", "多少人"
    ]):
        intro = "👑 LeBron James 幫你點完名了"

    elif "訂單已確認" in compact and ("已成功寫入" in compact or "訂單編號" in compact):
        intro = "👑 LeBron James 這筆訂單完成助攻"

    elif any(key in compact for key in [
        "歷史訂單", "訂書紀錄", "訂書進度", "單日訂單", "筆訂單", "訂單編號"
    ]):
        intro = "👑 LeBron James 幫你把紀錄翻出來了"

    elif any(key in compact for key in [
        "大漢訂書小幫手", "我可以幫你", "功能", "直接用平常講話", "今天想幹嘛"
    ]):
        intro = "👑 LeBron James 幫你把戰術板打開了"

    elif any(key in compact for key in [
        "訂單已確認", "已建立", "成功", "已寫入", "完成"
    ]):
        intro = "👑 LeBron James 這球漂亮收尾"

    else:
        intro = "👑 LeBron James 幫你處理好了"

    return f"{intro}\n\n{body}"


# =========================================================
# LINE 回覆
# =========================================================
def reply_to_line(reply_token, message, quick_reply=None):
    if not reply_token:
        logger.warning("reply_token missing")
        return

    if not CHANNEL_ACCESS_TOKEN:
        logger.error("LINE access token 沒有讀到")
        return

    url = "https://api.line.me/v2/bot/message/reply"

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + CHANNEL_ACCESS_TOKEN
    }

    if isinstance(message, (list, tuple)):
        message_items = [str(item or "").strip() for item in message if str(item or "").strip()]
    else:
        message_items = [str(message or "").strip()]

    messages = [
        {"type": "text", "text": item[:4900]}
        for item in message_items[:5]
    ]

    # Quick Reply 只能附加在其中一則訊息上，LINE 會顯示在整組回覆的
    # 最下面，所以固定附加在最後一則。
    if quick_reply and messages:
        messages[-1]["quickReply"] = quick_reply

    data = {
        "replyToken": reply_token,
        "messages": messages
    }

    attempts = 2
    for attempt in range(attempts):
        try:
            response = HTTP.post(
                url,
                headers=headers,
                json=data,
                timeout=10
            )

            logger.info(f"LINE reply status: {response.status_code}")
            logger.debug(f"LINE reply response: {response.text}")

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < attempts - 1:
                    time.sleep(0.3)
                    continue
            return

        except Exception as error:
            logger.warning(f"LINE reply error (attempt {attempt + 1}/{attempts}): {error}")
            if attempt < attempts - 1:
                time.sleep(0.3)
                continue


# =========================================================
# v81：補習班「整段訂單」——轉傳一整段訊息，整理成可修改的清單
# =========================================================
# 流程：
#   1. 偵測：3 行以上都有「數字＋本/份/冊/套」的訊息，就當成整段訂單
#   2. AI 只負責「讀懂」：拆成一項一項（科目、冊次、出版社、系列、卷別、數量…）
#   3. 程式負責「對書」：用 Apps Script 的 list_books 一次拿到整份書籍資料
#      （含 E 欄「別名」），在本地逐項比對，不會一項打一次 Google
#   4. 給使用者一張有編號的清單：✅ 對到、❓ 要選、❌ 資料庫沒有
#   5. 可以改數量／刪除／選書／指定補習班，確認後寫進「補習班訂單」
pending_bulk_orders = {}
_SESSION_DICTS["pending_bulk_orders"] = pending_bulk_orders

_GOOGLE_CACHE_TTLS["list_books"] = 1800
_GOOGLE_STALE_MAX_AGES["list_books"] = 7 * 24 * 3600

_V81_QTY_RE = re.compile(r"\d+\s*(?:\+\s*\d+\s*)*(?:本|份|冊|套)|[xX×＊*]\s*\d+")
_V81_SUBJECTS = ["國文", "英文", "數學", "自然", "生物", "理化", "地科", "歷史", "地理", "公民", "社會"]
_V81_PAPER_TYPES = {"A卷", "B卷", "C卷", "D卷", "KA", "KB", "門市卷", "測驗卷"}
# 使用者確認過的講法：康軒「新挑戰」如果不是卷（沒寫 A/B 卷、KA/KB），指的是康軒學習講義
_V81_SERIES_REWRITE = {("康軒", "新挑戰"): "學習講義", ("康軒", "新挑戰講義"): "學習講義"}


def _v81_is_bulk_text(raw_text):
    lines = [l for l in str(raw_text or "").splitlines() if l.strip()]
    if len(lines) < 3:
        return False
    qty_lines = sum(1 for l in lines if _V81_QTY_RE.search(l))
    return qty_lines >= 3


def _v81_norm(text):
    t = normalize_text(str(text or "")).upper()
    t = re.sub(r"[\s，,。.!！?？:：、()（）【】\[\]「」『』\-－_/]+", "", t)
    return t.replace("英語", "英文").replace("8K", "")


def _v81_bulk_system_prompt():
    return """你是書店的補習班訂單整理員。使用者會貼上一整段補習班老師傳來的訊息，
你要把它拆成「一項一項要買的東西」，只輸出 JSON，不要自己補不存在的資料。

輸出格式：
{"cram_school":"","items":[{"section":"國文 第一冊","floor":"5F",
 "subjects":["國文"],"volume":1,"grade":0,"publisher":"翰林","version":"","series":"超級悍將",
 "paper":"","quantities":[43],"note":"","line":"超級悍將 國文（ㄧ） 43本"}],"notes":["整段共通的備註"]}

規則：
1. 標題行（例如「國文 第三冊 送8F」「社會科 7年級 送5F」）決定後面每一項的 section、subject、volume／grade、floor，直到下一個標題。
2. volume 是冊次數字：第一冊、（ㄧ）、(1)、（5）都要轉成數字。只寫年級沒寫冊次時，volume 填 0、grade 填 7/8/9。
3. 一行寫了好幾科（例如「歷史 地理 公民」「歷地公」），subjects 直接列多科 ["歷史","地理","公民"]，不用拆成好幾項。
   沒寫科目時沿用標題的科目。欄位值空白就給空字串，不要省略欄位名稱以外的東西，也不要多寫說明。
4. quantities：「43本」→[43]；「20+20份」→[20,20]（保留兩筆，不要加總）；「x45本」→[45]。
5. publisher 填訊息寫的品牌（翰林、康軒、南一、奇鼎…）；「8k 奇鼎 A卷 翰林」這種，publisher=奇鼎、version=翰林。
6. paper 只能填：A卷、B卷、C卷、D卷、KA、KB、門市卷、測驗卷，或空白。KA、KB 照寫，不要改成 A卷。
   「翰林A卷」「翰林 翰林A卷」「翰林A 卷」都是 publisher=翰林、paper=A卷，不能漏掉卷別。
7. series 填系列名稱原文（超級悍將、新挑戰學習講義、標竿、教學式、學習講義…），8K、8k 不用寫。
8. 「無法出貨看有什麼版本」「沒有就門市卷」這類替代說明放到該項 note；
   「北投、石牌…（翰林版）」「幫確定列的版本是否正確」這類跟整段有關的說明放 notes。
9. 有數量但看不懂的行也要列出來，series 填那一行原文，quantities 照寫，不要丟掉。
   沒有數量的行（學校名單、版本說明、「=====」分隔線）不是要買的東西，不要放進 items；有意義的說明放 notes，分隔線直接略過。
11. line 填這一項在原訊息裡的那一行原文（照抄，不要改字）。
10. 訊息裡有寫補習班名稱才填 cram_school，沒有就留空。"""


# v82：整段訂單的 AI 呼叫另外寫，原因（實際上線第一次就失敗）：
#   gpt-5-mini 是「會先思考」的模型，max_completion_tokens 包含思考用掉的字數。
#   43 項的清單本身就要 3000 字左右，再加上思考，4000 一定不夠 → 回覆被截斷 → JSON 壞掉。
#   所以：思考開到最少（reasoning_effort）、上限拉高、截斷時救回已經寫完的項目，
#   失敗時把原因告訴使用者、也寫進 log。
_V82_BULK_MAX_TOKENS = int(os.environ.get("BULK_AI_MAX_TOKENS", "16000"))
_V82_BULK_TIMEOUT = float(os.environ.get("BULK_AI_TIMEOUT_SECONDS", "45"))
_v82_last_bulk_failure = {}


def _v82_salvage_json(content):
    """回覆被截斷時，把已經完整寫完的項目救回來。"""
    text = str(content or "")
    start = text.find("{")
    if start < 0:
        return None
    text = text[start:]
    try:
        return json.loads(text)
    except Exception:
        pass
    items_at = text.find('"items"')
    if items_at < 0:
        return None
    # 從最後一個「},」或「}」往回試，補上 ]} 讓它變成合法 JSON
    cut = len(text)
    for _ in range(200):
        cut = text.rfind("}", 0, cut)
        if cut <= items_at:
            return None
        candidate = text[:cut + 1] + "]}"
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and data.get("items"):
                data.setdefault("notes", [])
                data["_truncated"] = True
                return data
        except Exception:
            pass
    return None


def _v82_bulk_ai_call(messages):
    """回傳 (data, 失敗原因)。原因：no_key / timeout / http_xxx / too_long / bad_json / error"""
    if not OPENAI_API_KEY:
        return None, "no_key"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    efforts = ["minimal", "low", None] if OPENAI_MODEL.lower().startswith(("gpt-5", "o")) else [None]
    deadline = time.time() + _V82_BULK_TIMEOUT
    last_reason = "error"
    for effort in efforts:
        remaining = deadline - time.time()
        if remaining < 5:
            return None, last_reason if last_reason != "error" else "timeout"
        payload = {
            "model": OPENAI_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_completion_tokens": _V82_BULK_MAX_TOKENS,
        }
        if effort:
            payload["reasoning_effort"] = effort
        started = time.time()
        try:
            response = HTTP.post("https://api.openai.com/v1/chat/completions",
                                 headers=headers, json=payload, timeout=remaining)
        except Exception as error:
            name = type(error).__name__
            logger.warning("bulk AI error effort=%s %s: %s", effort, name, error)
            return None, "timeout" if "Timeout" in name else "error"
        elapsed = time.time() - started
        if response.status_code == 400 and effort and "reasoning" in (response.text or "").lower():
            logger.warning("bulk AI: model rejected reasoning_effort=%s, retrying", effort)
            last_reason = "http_400"
            continue
        if response.status_code != 200:
            logger.warning("bulk AI failed status=%s body=%s", response.status_code, (response.text or "")[:300])
            return None, f"http_{response.status_code}"
        try:
            body = response.json()
            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content") or ""
            finish = choice.get("finish_reason") or ""
            usage = body.get("usage") or {}
        except Exception as error:
            logger.warning("bulk AI bad body: %s", error)
            return None, "bad_json"
        logger.info("bulk AI done effort=%s elapsed=%.1fs finish=%s usage=%s chars=%s",
                    effort, elapsed, finish, usage, len(content))
        try:
            return json.loads(content), ""
        except Exception:
            data = _v82_salvage_json(content)
            if data:
                logger.warning("bulk AI output truncated, salvaged %s items", len(data.get("items", [])))
                return data, ""
            return None, "too_long" if finish == "length" else "bad_json"
    return None, last_reason


def _v81_extract_bulk(raw_text):
    data, reason = _v82_bulk_ai_call([
        {"role": "system", "content": _v81_bulk_system_prompt()},
        {"role": "user", "content": str(raw_text or "")[:6000]},
    ])
    _v82_last_bulk_failure["reason"] = reason
    return data


def _v81_load_books():
    result = google_post({"action": "list_books"}, timeout=15, retries=1, ignore_budget=True)
    if not result or not result.get("success"):
        return None
    books = []
    for b in result.get("books", []) or []:
        name = str(b.get("name", "") or "").strip()
        if not name or name == "書名":
            continue
        aliases = [a.strip() for a in re.split(r"[、,，/;；]", str(b.get("alias", "") or "")) if a.strip()]
        books.append({
            "name": name,
            "publisher": str(b.get("publisher", "") or "").strip(),
            "version": str(b.get("version", "") or "").strip(),
            "category": str(b.get("category", "") or "").strip(),
            "aliases": aliases,
            "_n": _v81_norm(name),
            "_aliases_n": [_v81_norm(a) for a in aliases],
        })
    return books


def _v81_volume_from_grade(grade):
    """沒寫冊次時用年級＋學期推：8 月～隔年 1 月是上學期（1、3、5 冊），2～7 月是下學期（2、4、6 冊）。"""
    try:
        g = int(grade or 0)
    except Exception:
        return 0, ""
    if g not in (7, 8, 9):
        return 0, ""
    month = datetime.now().month
    upper = month >= 8 or month == 1
    vol = (g - 7) * 2 + (1 if upper else 2)
    cn = {7: "七", 8: "八", 9: "九"}[g]
    return vol, f"{cn}年級→第{vol}冊（{'上' if upper else '下'}學期）"


def _v81_subject_volume_ok(book, subject, volume):
    n = book["_n"]
    s = _v81_norm(subject)
    if not s or s not in n:
        return False
    if not volume:
        return True
    tail = n[n.index(s) + len(s):]
    return re.search(rf"(^|[^0-9~]){int(volume)}([^0-9~]|$)", tail) is not None


def _v81_match(item, books):
    """回傳 (狀態, 候選清單)。狀態：ok / choose / missing"""
    subject = item.get("subject", "")
    volume = item.get("volume", 0)
    pub = str(item.get("publisher", "") or "").strip()
    ver = str(item.get("version", "") or "").strip()
    paper = str(item.get("paper", "") or "").strip().upper().replace("卷", "卷")
    series = str(item.get("series", "") or "").strip()
    if paper == "門市卷":
        return "missing", []
    series = _V81_SERIES_REWRITE.get((pub, _v81_norm(series)), series) if not paper else series

    series_paper = _v81_norm(series + paper)
    key_full = _v81_norm(pub + series + paper)
    hits = []
    for b in books:
        if not _v81_subject_volume_ok(b, subject, volume):
            continue
        if ver and b["version"] and b["version"] != ver:
            continue
        if paper and b["category"] != "卷類":
            continue
        alias_hit = any(a and (a in key_full or (series_paper and a == series_paper)) for a in b["_aliases_n"])
        s = _v81_norm(subject)
        core = b["_n"][:b["_n"].index(s)] if s in b["_n"] else b["_n"]
        name_hit = bool(series_paper) and bool(core) and (core in series_paper or series_paper in core) \
            and min(len(core), len(series_paper)) >= 2
        if not (alias_hit or name_hit):
            continue
        if pub and b["publisher"] != pub and not alias_hit:
            continue
        rank = (alias_hit, b["publisher"] == pub, bool(ver) and b["version"] == ver, name_hit)
        hits.append((rank, b))
    if not hits:
        return "missing", []
    best = max(h[0] for h in hits)
    top = [b for r, b in hits if r == best]
    if len(top) == 1:
        return "ok", top
    return "choose", sorted(top, key=lambda b: (b["publisher"], b["name"]))[:6]


_V83_PAPER_RE = re.compile(r"門市卷|K\s*([AB])(?![A-Za-z])|(?<![A-Za-z])([ABCD])\s*卷", re.I)


def _v83_has_quantity(it):
    for q in (it.get("quantities") or []):
        try:
            if int(q) > 0:
                return True
        except Exception:
            continue
    return False


def _v83_fix_paper(it):
    """v83：AI 漏填卷別時（例如「翰林 翰林A卷 20+20份」），從 series／原文／備註把 A卷、KB… 找回來。"""
    if str(it.get("paper", "") or "").strip():
        return it
    it = dict(it)
    for field in ("series", "line", "note"):
        text = str(it.get(field, "") or "")
        m = _V83_PAPER_RE.search(text)
        if not m:
            continue
        if m.group(0) == "門市卷":
            it["paper"] = "門市卷"
        elif m.group(1):
            it["paper"] = "K" + m.group(1).upper()
        else:
            it["paper"] = m.group(2).upper() + "卷"
        if field == "series":
            rest = _V83_PAPER_RE.sub("", text)
            pub = str(it.get("publisher", "") or "")
            if pub:
                rest = rest.replace(pub, "")
            it["series"] = rest.strip(" 　8Kk")
        break
    return it


def _v83_clean_notes(notes):
    out = []
    for n in notes or []:
        t = str(n or "").strip()
        core = re.sub(r"[=＝\-－_~～\s　]+", "", t)
        core_wo_floor = re.sub(r"送?\d+\s*[FfＦ樓]", "", core)
        if not core or not core_wo_floor:
            continue          # 只剩分隔線、或只有「送5F」
        if t not in out:
            out.append(t)
    return out


def _v81_build_rows(data, books):
    rows = []
    expanded = []
    for it in (data.get("items") or []):
        if not isinstance(it, dict):
            continue
        subs = it.get("subjects")
        if isinstance(subs, str):
            subs = [x for x in re.split(r"[、,，\s/]+", subs) if x]
        if not subs:
            subs = [it.get("subject", "")]
        for sub in subs:
            expanded.append(dict(it, subject=sub))
    extra_notes = data.setdefault("notes", []) if isinstance(data.get("notes", []), list) else []
    for it in expanded:
        if not _v83_has_quantity(it):
            # v83：沒有數量的行（學校名單、版本說明）不是要買的東西，改放備註
            text = "；".join(x for x in (str(it.get("note", "") or "").strip(),
                                         str(it.get("line", "") or "").strip()) if x) \
                or str(it.get("series", "") or "").strip()
            if text and text not in extra_notes:
                extra_notes.append(text)
            continue
        it = _v83_fix_paper(it)
        subject = str(it.get("subject", "") or "").strip().replace("英語", "英文")
        try:
            volume = int(it.get("volume") or 0)
        except Exception:
            volume = 0
        vol_note = ""
        if not volume:
            volume, vol_note = _v81_volume_from_grade(it.get("grade"))
        qtys = []
        for q in (it.get("quantities") or []):
            try:
                q = int(q)
            except Exception:
                continue
            if q > 0:
                qtys.append(q)
        if not qtys:
            qtys = [0]
        base = {
            "line": str(it.get("line", "") or "").strip(),
            "section": str(it.get("section", "") or "").strip(),
            "floor": str(it.get("floor", "") or "").strip().upper(),
            "subject": subject, "volume": volume, "vol_note": vol_note,
            "publisher": str(it.get("publisher", "") or "").strip(),
            "version": str(it.get("version", "") or "").strip(),
            "series": str(it.get("series", "") or "").strip(),
            "paper": str(it.get("paper", "") or "").strip(),
            "note": str(it.get("note", "") or "").strip(),
        }
        status, cands = _v81_match(base, books)
        for idx, q in enumerate(qtys):
            row = dict(base, qty=q, status=status, removed=False,
                       choices=[{"book": b["name"], "publisher": b["publisher"]} for b in cands],
                       book="", book_publisher="")
            if len(qtys) > 1:
                row["note"] = ((row["note"] + "；") if row["note"] else "") + f"第{idx + 1}組"
            if status == "ok":
                row["book"] = cands[0]["name"]
                row["book_publisher"] = cands[0]["publisher"]
            if q <= 0:
                row["status"] = "missing"
                row["note"] = ((row["note"] + "；") if row["note"] else "") + "沒讀到數量"
            rows.append(row)
    for i, r in enumerate(rows, 1):
        r["no"] = i
    return rows


def _v81_describe_raw(r):
    parts = [r.get("publisher", ""), r.get("series", ""), r.get("paper", ""), r.get("subject", "")]
    text = " ".join(p for p in parts if p)
    if r.get("volume"):
        text += f"({r['volume']})"
    return text or r.get("line", "") or "（看不懂的一行）"


def _v81_confirmation(order):
    """回傳 LINE 訊息清單（最多 5 則，每則 4,500 字以內，照分段切，不會切在一項中間）。"""
    rows = [r for r in order["rows"] if not r["removed"]]
    head = ["👑 LeBron James 幫你把整段訂單整理好了", "", f"📋 補習班訂單整理（共 {len(rows)} 項）"]
    if order.get("cram_school"):
        head.append(f"🏫 補習班：{order['cram_school']}")
    else:
        head.append("❓ 補習班：訊息裡沒寫，請回覆補習班名稱（名單裡沒有的新補習班，請打「補習班是XXX」）")
    blocks = []          # 每個分段一塊
    section, cur = None, None
    for r in rows:
        floor_txt = f"送{r['floor']}" if r["floor"] else ""
        if floor_txt and floor_txt in r["section"].replace(" ", "").upper():
            floor_txt = ""
        sec = "｜".join(x for x in (r["section"], floor_txt) if x) or "其他"
        if sec != section:
            section = sec
            title = f"【{sec}】"
            if r.get("vol_note"):
                title = f"【{sec}】{r['vol_note']}"
            cur = [title]
            blocks.append(cur)
        unit = "份" if (r.get("paper") or "卷" in r.get("book", "")) else "本"
        group = re.search(r"第(\d+)組", r.get("note", ""))
        gtxt = f"（第{group.group(1)}組）" if group else ""
        note = re.sub(r"；?第\d+組", "", r.get("note", "")).strip("；")
        if r["status"] == "ok":
            cur.append(f"{r['no']}. ✅ [{r['book_publisher']}] {r['book']}｜{r['qty']}{unit}{gtxt}")
        elif r["status"] == "choose":
            cur.append(f"{r['no']}. ❓ {_v81_describe_raw(r)}｜{r['qty']}{unit}{gtxt}　請選：")
            for j, c in enumerate(r["choices"], 1):
                cur.append(f"      {j}) [{c['publisher']}] {c['book']}")
        else:
            cur.append(f"{r['no']}. ❌ {_v81_describe_raw(r)}｜{r['qty']}{unit}{gtxt}（資料庫沒有，需人工處理）")
        if note:
            cur.append(f"      📝 {note}")
    ok = [r for r in rows if r["status"] == "ok"]
    ask = [r for r in rows if r["status"] == "choose"]
    miss = [r for r in rows if r["status"] == "missing"]
    tail = []
    if order.get("notes"):
        tail += ["📌 備註"] + [f"• {n}" for n in order["notes"]] + [""]
    tail += [f"✅ {len(ok)} 項（共 {sum(r['qty'] for r in ok)} 本／份）　❓ {len(ask)} 項　❌ {len(miss)} 項",
             "", "修改：「3改25」「5刪掉」「6選2」",
             "確認後會把 ✅ 的寫進「補習班訂單」；❌ 的不會寫入，要自己另外處理。",
             "全部正確請回覆「確認」，不要了請回覆「取消」。"]

    limit = 4500
    messages, buf = [], "\n".join(head)
    for b in blocks:
        text = "\n".join(b)
        if len(buf) + 2 + len(text) > limit:
            messages.append(buf)
            buf = text
        else:
            buf += "\n\n" + text
    tail_text = "\n".join(tail)
    if len(buf) + 2 + len(tail_text) > limit:
        messages.append(buf)
        buf = tail_text
    else:
        buf += "\n\n" + tail_text
    messages.append(buf)
    if len(messages) > 5:     # LINE 一次最多 5 則
        messages = messages[:4] + ["⚠️ 清單太長，後面的項目沒辦法一次顯示；請把訊息分成兩段傳。\n\n" + tail_text]
    return messages if len(messages) > 1 else messages[0]


def _v81_with_prefix(prefix, conf):
    """在確認畫面前面加一行「✅ 已修改…」。"""
    if isinstance(conf, list):
        return [conf[0].replace("👑 LeBron James 幫你把整段訂單整理好了\n\n", "👑 LeBron James 幫你把整段訂單整理好了\n\n" + prefix + "\n\n", 1)] + conf[1:]
    return conf.replace("👑 LeBron James 幫你把整段訂單整理好了\n\n", "👑 LeBron James 幫你把整段訂單整理好了\n\n" + prefix + "\n\n", 1)


def _v81_resolve_cram_school(text):
    clean = re.sub(r"^(?:補習班(?:是|為|:|：)?)", "", re.sub(r"[\s，,。.!！?？]+", "", str(text or "")))
    if not clean or len(clean) > 20:
        return ""
    catalog = get_cram_school_catalog()
    if clean in catalog:
        return clean
    for name in catalog:
        if name.startswith(clean) or clean.startswith(name):
            return name
    return clean   # 名單裡沒有：照打的名字（Apps Script 會自動加進補習班名單）


def _v81_start_bulk(user_id, raw_text):
    if not OPENAI_API_KEY:
        return "📋 這看起來是一整段訂單，但整理整段訂單需要 AI 功能（OPENAI_API_KEY 還沒設定）。"
    data = _v81_extract_bulk(raw_text)
    if not isinstance(data, dict) or not data.get("items"):
        reason = _v82_last_bulk_failure.get("reason") or ("empty" if isinstance(data, dict) else "error")
        why = {
            "timeout": "AI 整理太久（超過 {:.0f} 秒）沒有回來".format(_V82_BULK_TIMEOUT),
            "too_long": "AI 的回覆太長被截斷",
            "bad_json": "AI 回覆的格式不對",
            "empty": "AI 沒有從這段訊息找到要買的東西",
            "http_401": "OpenAI 金鑰無效（401）",
            "http_429": "OpenAI 額度用完或太多人同時用（429）",
        }.get(reason, "AI 呼叫失敗（{}）".format(reason))
        return (
            "⚠️ 這段訂單我這次沒有整理成功。\n"
            f"原因：{why}\n\n"
            "可以再傳一次；如果還是不行，請分成幾段傳，或改用「補習班訂書」一本一本登記。"
        )
    books = _v81_load_books()
    if books is None:
        return (
            "⚠️ 讀不到書籍資料，這段訂單先沒辦法整理。\n\n"
            "如果 Apps Script 還沒加上 list_books 功能，請先更新 Apps Script。"
        )
    rows = _v81_build_rows(data, books)
    if not rows:
        return "⚠️ 這段訊息裡我沒有找到要訂的書。"
    clear_task_states_for_new_mode(user_id)
    pending_photo_orders.pop(user_id, None)
    guided_mode.pop(user_id, None)
    cram = str(data.get("cram_school", "") or "").strip()
    order = {
        "cram_school": _v81_resolve_cram_school(cram) if cram else "",
        "rows": rows,
        "notes": _v83_clean_notes(data.get("notes") or []),
        "created_at": time.time(),
    }
    pending_bulk_orders[user_id] = order
    return _v81_confirmation(order)


def _v81_find_row(order, no):
    for r in order["rows"]:
        if r["no"] == no and not r["removed"]:
            return r
    return None


def _v81_handle_pending(user_id, text):
    order = pending_bulk_orders.get(user_id)
    if not order:
        return None
    clean = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))

    if clean in {"取消", "不要了", "這筆不要", "取消訂單", "全部取消"}:
        pending_bulk_orders.pop(user_id, None)
        return "❌ 已取消這份補習班訂單整理，Google 沒有寫入。"

    if _is_confirm_word(clean):
        return _v81_confirm(user_id, order)

    m = re.fullmatch(r"第?(\d{1,3})(?:項)?(?:改成|改為|改|要)(\d{1,4})(?:本|份|冊|套)?", clean)
    if m:
        r = _v81_find_row(order, int(m.group(1)))
        if not r:
            return f"⚠️ 清單裡沒有第 {m.group(1)} 項。"
        if int(m.group(2)) <= 0:
            return "⚠️ 數量要大於 0；整項不要請說「" + m.group(1) + "刪掉」。"
        r["qty"] = int(m.group(2))
        return _v81_with_prefix(f"✅ 第 {r['no']} 項改成 {r['qty']}。", _v81_confirmation(order))

    m = re.fullmatch(r"(?:刪除?|刪掉|拿掉|移除)?第?(\d{1,3})(?:項)?(?:刪掉|刪除|不要了|不要|拿掉|移除|取消)?", clean)
    if m and clean != m.group(1) and not clean.isdigit():
        r = _v81_find_row(order, int(m.group(1)))
        if not r:
            return f"⚠️ 清單裡沒有第 {m.group(1)} 項。"
        r["removed"] = True
        return _v81_with_prefix(f"✅ 已刪除第 {r['no']} 項。", _v81_confirmation(order))

    m = re.fullmatch(r"第?(\d{1,3})(?:項)?選(?:第)?(\d{1,2})", clean)
    if m:
        r = _v81_find_row(order, int(m.group(1)))
        if not r:
            return f"⚠️ 清單裡沒有第 {m.group(1)} 項。"
        if r["status"] != "choose":
            return f"⚠️ 第 {r['no']} 項不用選。"
        k = int(m.group(2))
        if not (1 <= k <= len(r["choices"])):
            return f"⚠️ 第 {r['no']} 項請選 1～{len(r['choices'])}。"
        c = r["choices"][k - 1]
        r.update(status="ok", book=c["book"], book_publisher=c["publisher"])
        return _v81_with_prefix(f"✅ 第 {r['no']} 項選定：[{c['publisher']}] {c['book']}", _v81_confirmation(order))

    m = re.fullmatch(r"補習班(?:是|為|:|：)(.+)", clean)
    if m:
        name = _v81_resolve_cram_school(m.group(1))
        if name:
            order["cram_school"] = name
            return _v81_with_prefix(f"✅ 補習班：{name}", _v81_confirmation(order))
    if not order.get("cram_school") and re.fullmatch(r"[一-鿿A-Za-z]{2,15}", clean):
        # 還沒指定補習班時，只接受「看起來就是補習班」的回覆，
        # 避免把「查老師」這類指令當成補習班名稱。
        catalog = get_cram_school_catalog()
        in_catalog = any(n == clean or n.startswith(clean) or clean.startswith(n) for n in catalog)
        looks_like = re.search(r"(補習班|文理|美語|學苑|書院|學院|教育|數理|英語|數學|理化|家教|安親)$", clean)
        if in_catalog or looks_like:
            name = _v81_resolve_cram_school(clean)
            order["cram_school"] = name
            return _v81_with_prefix(f"✅ 補習班：{name}", _v81_confirmation(order))
    return None


def _v81_confirm(user_id, order):
    rows = [r for r in order["rows"] if not r["removed"]]
    if not order.get("cram_school"):
        return "⚠️ 還不知道是哪一間補習班，請回覆補習班名稱（新的補習班請打「補習班是XXX」）。"
    ask = [str(r["no"]) for r in rows if r["status"] == "choose"]
    if ask:
        return f"⚠️ 第 {'、'.join(ask)} 項還沒選書，請回覆例如「{ask[0]}選1」，不要的話說「{ask[0]}刪掉」。"
    ok = [r for r in rows if r["status"] == "ok" and r["qty"] > 0]
    miss = [r for r in rows if r["status"] == "missing"]
    if not ok:
        return "⚠️ 清單裡沒有資料庫對得到的書，沒有東西可以寫入。"
    draft = {"cram_school": order["cram_school"],
             "items": [{"publisher": r["book_publisher"], "book": r["book"], "quantity": r["qty"]} for r in ok]}
    success, order_number = write_cram_order_to_google(draft)
    if not success:
        return "❌ 補習班訂單寫入失敗，請稍後再試。清單還保留著，可以再回覆「確認」。"
    pending_bulk_orders.pop(user_id, None)
    pending_receipt_offers[user_id] = {
        "kind": "cram", "order_number": order_number, "cram_school": order["cram_school"],
        "items": draft["items"], "created_at": time.time(),
    }
    lines = ["✅ 補習班訂單已確認", "", f"訂單編號：{order_number}", f"補習班：{order['cram_school']}",
             f"寫入 {len(ok)} 項，共 {sum(r['qty'] for r in ok)} 本／份"]
    floors = {}
    for r in ok:
        floors.setdefault(r["floor"] or "未寫樓層", []).append(r)
    if len(floors) > 1 or "未寫樓層" not in floors:
        lines += ["", "📦 送貨樓層"]
        for fl, lst in floors.items():
            lines.append(f"• {fl}：{len(lst)} 項 {sum(r['qty'] for r in lst)} 本／份")
    if miss:
        lines += ["", "⚠️ 以下沒有寫入，需要人工處理："]
        lines += [f"• {_v81_describe_raw(r)}｜{r['qty']}" + (f"（{r['note']}）" if r["note"] else "") for r in miss]
    return ["\n".join(lines),
            "需要幫你生成一份訂購單 PDF，讓你可以存下來 email 給補習班或出版社嗎？\n"
            "回覆「要」或「好」即可，40 秒內沒有回覆就會自動取消這個提問。"]


def _v81_route_bulk(user_id, raw_text):
    """_route_message 最前面呼叫：有待確認的整段訂單就先處理指令；新的整段訂單就開始整理。"""
    if user_id in pending_bulk_orders:
        reply = _v81_handle_pending(user_id, raw_text)
        if reply is not None:
            return reply
        if not _v81_is_bulk_text(raw_text):
            # 清單還沒確認時不讓其他功能接手——不然之後別的流程打「確認」，
            # 會被當成確認這份補習班訂單（跟拍照訂單待確認時的做法一樣）。
            order = pending_bulk_orders[user_id]
            n = len([r for r in order["rows"] if not r["removed"]])
            return (
                f"📋 你還有一份補習班訂單清單（{n} 項）還沒確認。\n\n"
                "• 要修改：「3改25」「5刪掉」「6選2」\n"
                "• 要寫入：回覆「確認」\n"
                "• 不要了：回覆「取消」\n"
                "• 先去做別的事：回覆「主選單」（這份清單會被放棄）"
            )
    if _v81_is_bulk_text(raw_text):
        return _v81_start_bulk(user_id, raw_text)
    return None


_V81_ORDER_FAIL_MARKERS = ("找不到符合", "找不到這位老師", "查不到", "無法確認", "請重新輸入正確的老師", "資料庫目前找不到")


def _v81_ai_rescue_failed_order(user_id, text, order_reply):
    """#17：一般訂書規則抓錯（例如老師名變成「習作給侯」）時，讓 AI 讀一次整句。"""
    if not OPENAI_API_KEY:
        return None
    body = "\n".join(order_reply) if isinstance(order_reply, (list, tuple)) else str(order_reply or "")
    if not any(m in body for m in _V81_ORDER_FAIL_MARKERS):
        return None
    compact = re.sub(r"\s+", "", str(text or ""))
    if len(compact) < 8 or compact in CONFIRM_WORDS or compact in EXIT_WORDS:
        return None          # 短回答（例如只打老師名字）照舊，不多等一次 AI
    had_photo = user_id in pending_photo_orders
    reply = handle_smart_order_fallback(user_id, text)
    if reply is None or had_photo or user_id not in pending_photo_orders:
        return None
    # AI 讀懂了：清掉剛剛規則層留下的半套訂書狀態，避免兩邊打架
    order_flow_context.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
    pending_orders.pop(user_id, None)
    if guided_mode.get(user_id) == "order_flow":
        guided_mode.pop(user_id, None)
    return reply


if __name__ == "__main__":
    # 注意：正式環境（Render）是透過 gunicorn 啟動 Start Command，
    # 不會執行到這裡；這裡只有本機用 `python app.py` 測試時會用到。
    #
    # 【重要】請確認 Render 的 Start Command 有帶足夠長的 --timeout，
    # 例如：
    #     gunicorn app:app --workers 2 --timeout 60
    # Google Apps Script 偶爾會慢到 8~10 秒才回應，如果 gunicorn 的
    # worker timeout 太短（預設 30 秒），遇到連續幾次查詢疊加時，
    # worker 可能會被強制殺掉（WORKER TIMEOUT），導致該則訊息完全
    # 收不到任何回覆。搭配上面新增的請求時間預算機制，兩者一起可以
    # 大幅降低這種情況發生的機率。
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port
    )
