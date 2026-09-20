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
import concurrent.futures
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
APP_VERSION = "2026-09-20-v53-image-ai-timeout-fix"

# 單一使用者單則訊息的長度上限。純粹是防呆／防濫用，
# 避免異常長的輸入把後面一大串正規表示式處理效能拖垮。
MAX_USER_TEXT_LENGTH = 1000

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
}

def _cache_key(payload):
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return str(payload)

def clear_google_read_cache():
    _google_read_cache.clear()


def _parallel_google_calls(calls):
    results = {}
    if not calls:
        return results

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = {
            executor.submit(func, *args, **kwargs): name
            for name, (func, args, kwargs) in calls.items()
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as error:
                logger.error(f"parallel google call error ({name}): {error}")
                results[name] = None

    return results


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
# 查詢資料」分類更清楚。
QUICK_REPLY_QUERY_ITEMS = [
    ("👨‍🏫 查老師", "查老師"),
    ("📅 查訂單", "查訂單"),
    ("📖 查版本", "查版本"),
    ("📊 查人數", "查人數"),
    ("📦 查其他訂單", "查其他訂單"),
]

# 「更多功能」按鈕點下去要出現的第二組按鈕——刻意跟 QUICK_REPLY_MAIN_ITEMS
# 完全不重複，展示常用清單裡沒放的次要功能，不然使用者點「更多功能」
# 卻看到同一組按鈕，會覺得沒有作用。
QUICK_REPLY_MORE_ITEMS = [
    ("📷 拍照訂書", "拍照訂書"),
    ("🧠 AI 助手", "AI助手"),
    ("📚 多書訂購", "多書訂購"),
    ("🏫 補習班訂書", "補習班訂書"),
    ("📦 其他訂單", "其他訂單"),
    ("📊 今日統計", "統計"),
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

CONFIRM_WORDS = {"確認", "是", "對", "對的", "沒錯", "正確", "可以", "好", "就是"}

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

# =========================================================
# 跨 worker 對話狀態持久化：SQLite
# =========================================================
_STATE_DB_PATH = os.environ.get("ORDER_STATE_DB_PATH", "/tmp/line_order_bot_state.sqlite3")
_SESSION_STALE_SECONDS = 3 * 24 * 3600
_PROCESSED_MESSAGE_TTL_SECONDS = 24 * 3600


def _state_db():
    conn = sqlite3.connect(_STATE_DB_PATH, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_session ("
        "user_id TEXT PRIMARY KEY, session_json TEXT, updated_at REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS processed_message ("
        "message_id TEXT PRIMARY KEY, processed_at REAL)"
    )
    return conn


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
        user_id = source.get("userId", "unknown")
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
        _hydrate_session(user_id)
        try:
            reply = _route_message(user_id, user_text)
            _remember_ai_turn(user_id, user_text, reply)
            return reply
        finally:
            _persist_session(user_id)


def _route_message(user_id, user_text):
    text = normalize_text(user_text)

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
        google_post({"action": "clear_cache"}, timeout=5, retries=1)
        get_school_catalog(force_refresh=True)
        return "✅ 已清除查詢快取，下一次查詢會直接讀取 Google 最新資料。"

    if text in {"統計", "今日統計", "今天統計", "訂單統計", "今日訂單統計"}:
        return get_today_order_stats_reply()

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
                or is_stats_mode_start(text)
                or is_multi_book_order_mode_start(text)
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

        escape_reply = _guided_mode_escape_reply(user_id, text)
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
        escape_reply = _guided_mode_escape_reply(user_id, text)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_teacher_lookup(user_id, text)

    if current_mode == "version_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        escape_reply = _guided_mode_escape_reply(user_id, text)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_version_lookup(user_id, text)

    if current_mode == "stats_lookup":
        if _is_exit_word(text):
            guided_mode.pop(user_id, None)
            return get_main_menu_reply()
        escape_reply = _guided_mode_escape_reply(user_id, text)
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

        escape_reply = _guided_mode_escape_reply(user_id, text)
        if escape_reply is not None:
            return escape_reply
        return handle_guided_history_lookup(user_id, text)

    if current_mode == "other_order":
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

        if user_id not in pending_other_orders:
            escape_reply = _guided_mode_escape_reply(user_id, text)
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
def _guided_mode_escape_reply(user_id, text):
    """
    在引導模式（guided_mode）中，如果使用者輸入的內容明顯符合
    『查老師 / 查各科老師 / 查版本 / 查人數 / 查訂單編號』這類其他功能
    既有的格式，直接跳出目前的引導模式並依該功能處理，
    而不是死板地卡在原模式一直要求正確格式。

    只有真的比對得上既有格式時才會跳出；比對不上的話回傳
    None，維持原本引導模式的提示與行為不變。
    """
    class_query = parse_class_teacher_query(text)
    if class_query:
        guided_mode.pop(user_id, None)
        return handle_class_teacher_query(class_query)

    subject_query = parse_subject_teacher_query(text)
    if subject_query:
        guided_mode.pop(user_id, None)
        return handle_subject_teacher_query(subject_query)

    stats_query = parse_school_stats_query(user_id, text)
    if stats_query:
        guided_mode.pop(user_id, None)
        return handle_school_stats_query(stats_query)

    if looks_like_teacher_lookup(text):
        guided_mode.pop(user_id, None)
        return handle_teacher_lookup(user_id, text)

    version_query = parse_school_version_query(user_id, text)
    if version_query:
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
    school = extract_school_name(clean)
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
    cram_order_context.pop(user_id, None)
    multi_book_order_context.pop(user_id, None)

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
    book = extract_book_candidate(text_for_book, teacher, classes) if has_order_intent else ""

    return {
        "has_order_intent": has_order_intent,
        "teacher": teacher,
        "school": school,
        "grade": grade,
        "classes": classes,
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
    if len(exact_matches) == 1:
        exact = exact_matches[0]
        teacher = exact["teacher"]
        school = exact["school"]
        teacher_classes = copy_classes(exact.get("classes", []))
        draft["teacher"] = teacher
        draft["school"] = school
    elif school and not teacher_classes:
        teacher_classes = get_teacher_classes(school, teacher) or []

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
    "好了", "不用了", "這樣就好", "完成", "沒有了", "訂好了", "夠了", "可以了", "這樣就可以了"
}


def is_cram_order_mode_start(text):
    compact = re.sub(r"[\s，,。.!！?？]+", "", str(text or ""))
    return compact in {
        "補習班訂書", "我要幫補習班訂書", "幫補習班訂書", "補習班訂購",
        "我要補習班訂書", "補習班要訂書", "補習班訂單", "新增補習班訂單",
        "我要訂補習班的書"
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

    if (
        not force_refresh
        and cram_school_catalog_cache.get("schools")
        and now < float(cram_school_catalog_cache.get("expires_at", 0) or 0)
    ):
        return list(cram_school_catalog_cache["schools"])

    result = google_post({"action": "list_cram_schools"}, timeout=10, retries=1)

    schools = []
    if result and result.get("success"):
        schools = unique_list([
            str(item or "").strip()
            for item in result.get("schools", [])
            if str(item or "").strip()
        ])

    if schools:
        cram_school_catalog_cache["schools"] = schools
        cram_school_catalog_cache["expires_at"] = now + 600
        return list(schools)

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
    return validate_cram_item_publisher_input(user_id, clean, draft)


def write_cram_order_to_google(draft):
    result = google_post({
        "action": "create_cram_order",
        "cram_school": draft.get("cram_school", ""),
        "items": draft.get("items", [])
    }, timeout=15, retries=1)

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

    if not draft.get("current_publisher"):
        if clean in CRAM_FINISH_WORDS:
            return _enter_cram_confirm_stage(user_id, draft)
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
MULTI_BOOK_MATCH_SCORE_THRESHOLD = 0.5
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

    exact_matches = lookup_teacher_matches(clean)

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


def _collect_multi_book_matches(query, publisher):
    """
    查一次書籍模糊比對，回傳所有分數達門檻、且書名不重複的候選
    （依分數高到低排序）。底層 lookup_book_candidates_enhanced()
    本身最多只會回 10 筆，這裡不另外放寬，算是既有機制的限制。
    """
    raw = lookup_book_candidates_enhanced(query, publisher=publisher)
    seen = set()
    result = []
    for c in raw:
        value = str(c.get("value", "") or "").strip()
        score = float(c.get("score", 0) or 0)
        if not value or value in seen or score < MULTI_BOOK_MATCH_SCORE_THRESHOLD:
            continue
        seen.add(value)
        result.append({
            "value": value,
            "publisher": str(c.get("publisher", "") or publisher or ""),
            "score": score,
        })
    return sorted(result, key=lambda x: x["score"], reverse=True)


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
        for c in _collect_multi_book_matches(query, pub):
            if c["value"] not in excluded_values:
                return c
    return None


def validate_multi_book_count_input(user_id, raw_text, draft):
    clean = re.sub(r"[，,。.!！?？\s]+", "", str(raw_text or ""))
    m = re.fullmatch(r"([0-9]{1,3}|[一二三四五六七八九十]{1,3})種?", clean)
    if not m:
        return "請直接告訴我要挑幾種書，用數字回覆就好，例如「7」。"

    raw_count = m.group(1)
    count = int(raw_count) if raw_count.isdigit() else int(_CN_NUM_MAP.get(raw_count, 0) or 0)

    if not count or count <= 0 or count > MULTI_BOOK_MAX_CANDIDATES:
        return f"數量要介於 1～{MULTI_BOOK_MAX_CANDIDATES} 之間，請重新輸入。"

    draft["expected_count"] = count
    multi_book_order_context[user_id] = draft
    return _run_multi_book_search(user_id, draft)


def _run_multi_book_search(user_id, draft):
    query = draft.get("keyword", "")
    publisher = draft.get("publisher") or ""
    expected = draft.get("expected_count", 0)

    matched = _collect_multi_book_matches(query, publisher)

    # 指定出版社湊不滿時，自動放寬成不限出版社繼續湊，直到湊滿、或
    # 資料庫裡真的沒有更多符合的書為止。判斷出版社永遠是看資料庫的
    # 「出版社」欄位，書名裡就算寫著「OO版」也不影響——那只是這份
    # 考卷搭配哪個課本版本的說明文字，不是它真正的出版社。
    if publisher and len(matched) < expected:
        seen_values = {c["value"] for c in matched}
        for c in _collect_multi_book_matches(query, ""):
            if len(matched) >= expected:
                break
            if c["value"] in seen_values:
                continue
            matched.append(c)
            seen_values.add(c["value"])

    matched = matched[:MULTI_BOOK_MAX_CANDIDATES]
    draft["candidates"] = matched
    draft["excluded_values"] = sorted({c["value"] for c in matched})
    multi_book_order_context[user_id] = draft

    if not matched:
        draft["expected_count"] = 0
        multi_book_order_context[user_id] = draft
        return (
            "⚠️ 依照目前條件完全查不到符合的書（已經含跨出版社查詢）。\n\n"
            f"關鍵字：{query}\n\n"
            "請重新輸入書名關鍵字。"
        )

    if len(matched) == expected:
        draft["awaiting_decision"] = False
        draft["awaiting_review"] = True
        multi_book_order_context[user_id] = draft
        return make_multi_book_review_reply(draft)

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
    lines = ["🔎 找到符合的書（已含跨出版社湊數量）：", ""]
    lines.extend(_format_multi_book_candidate_lines(candidates))
    lines.append("")
    lines.append(f"共 {len(candidates)} 種，符合你要的數量。")
    lines.append("")
    lines.append("如果有哪一項不要，回覆「N不要」或「換N」（例如「4不要」）我會幫你換掉那一項。")
    lines.append("都沒問題的話，請回覆「確認」，我再請你分配班級。")
    return "\n".join(lines)


def make_multi_book_classes_prompt(draft):
    candidates = draft.get("candidates", [])
    class_list = "、".join(
        c["class_name"] for c in sort_class_items(draft.get("teacher_classes", []))
    )
    lines = ["📚 最終書單：", ""]
    lines.extend(_format_multi_book_candidate_lines(candidates))
    lines.append("")
    lines.append(
        f"請依照上面清單的順序，依序告訴我要給哪 {len(candidates)} 個班"
        "（用空格或逗號分隔）。"
    )
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
    if clean in MULTI_BOOK_ACCEPT_FOUND_WORDS:
        draft["expected_count"] = len(draft.get("candidates", []))
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

    known_names = {c["class_name"] for c in draft.get("teacher_classes", [])}
    unknown = [p for p in parts if p not in known_names]

    if unknown:
        available = "、".join(
            c["class_name"] for c in sort_class_items(draft.get("teacher_classes", []))
        )
        return (
            f"⚠️ 這幾個班級對不上 {draft.get('teacher', '')} 的資料：{'、'.join(unknown)}\n\n"
            f"{draft.get('teacher', '')} 目前班級：{available}\n\n"
            "請重新輸入，用空格或逗號分隔。"
        )

    if len(unique_list(parts)) != len(parts):
        return "⚠️ 班級名稱重複了，請確認每個班只出現一次後重新輸入。"

    expected = len(draft.get("candidates", []))
    if len(parts) != expected:
        return (
            f"⚠️ 目前有 {expected} 種書，但你輸入了 {len(parts)} 個班級，兩者數量要一樣。\n\n"
            "請重新輸入班級名稱（用空格或逗號分隔）。"
        )

    class_lookup = {c["class_name"]: c for c in draft.get("teacher_classes", [])}
    assignments = []
    for class_name, book_item in zip(parts, draft["candidates"]):
        info = class_lookup[class_name]
        assignments.append({
            "class_name": class_name,
            "students": int(info.get("students", 0) or 0),
            "book": str(book_item.get("value", "") or ""),
            "publisher": str(book_item.get("publisher", "") or draft.get("publisher") or ""),
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
        lines.append(
            f"{i}. {item['class_name']}（{item['students']}本）→ "
            f"[{item['publisher']}] {item['book']}"
        )
        total += item["students"]
    lines.append("")
    lines.append(f"共 {len(draft.get('assignments', []))} 種書，總數量：{total}本")
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
    clean = str(text or "").strip()

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

    for prefix in sorted(prefixes, key=len, reverse=True):
        suffix_chars = "".join(sorted(re.escape(c) for c in prefixes[prefix]))
        if not suffix_chars:
            continue
        pattern = re.escape(prefix) + "[" + suffix_chars + "]+"
        for m in re.finditer(pattern, remaining):
            for ch in m.group(0)[len(prefix):]:
                found.append(prefix + ch)
        remaining = re.sub(pattern, " ", remaining)

    # 只講年級本身、沒有列出字母（例如「國一」），代表這位老師底下
    # 這個年級的班級「全部」都要，不用一個一個打。上面那段已經把
    # 「年級＋字母」的寫法都吃掉了，這裡剩下的「國一」就是單純講
    # 年級整體的情況。
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix in remaining:
            for suffix in sorted(prefixes[prefix]):
                found.append(prefix + suffix)
            remaining = remaining.replace(prefix, " ", 1)

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
        "講義", "評量", "教材", "複習", "測驗", "題本",
        "自修", "課本", "習作", "學習單", "套書",
    ]
    return any(w in clean for w in book_words)


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
        short = re.sub(r"(?:國中|高中|國小|中學)$", "", school)
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
_CLASS_LETTER_ORDER = {c: i for i, c in enumerate("甲乙丙丁戊己庚辛壬癸", start=1)}


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
    if found and not working.strip():
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
    m = re.search(r"([一二三四五六七八九十]{1,2})\s*個?班", text)
    if m and m.group(1) in _CN_NUM_MAP:
        return int(_CN_NUM_MAP[m.group(1)])
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
    remove_verbs = ("取消", "不要", "刪除", "刪掉", "拿掉", "移除")
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

    m = re.fullmatch(rf"(?:不要|刪除|刪掉|拿掉|移除)\s*({class_pattern})", text)
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
    add_verbs = ("新增", "增加", "加入", "加")
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

    # 備註：確認前直接加／改，最後確認時會寫入 Google J 欄。
    m = re.fullmatch(r"(?:加上?|新增|修改|改)?\s*備註[：:]?\s*(.+)", text)
    if m:
        order["note"] = m.group(1).strip()
        return make_order_confirmation(order)

    # 出版社修改：必須驗證「目前書名＋新出版社」確實存在。
    m = re.fullmatch(r"(?:出版社)?(?:改成|改為|換成|改)\s*(.+)", text)
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

    quantity_matches = list(re.finditer(
        rf"(?<!\d)({pattern})(?!\d)\s*(?:人數)?\s*"
        r"(改成|改為|改|多|少)\s*"
        r"(\d+)\s*(?:人|本)?",
        text
    ))

    remove_matches = list(re.finditer(
        rf"(?<!\d)({pattern})(?!\d)\s*"
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
            return datetime(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

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
        "多少人", "幾人", "幾個人", "學生人數", "總人數", "人數",
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

        return header + "\n\n" + "\n\n".join(blocks)

    lines = [f"• {item['subject']}：{item['version']}" for item in versions]
    return header + "\n\n" + "\n".join(lines)


def extract_school_name(text):
    clean = re.sub(r"[，,。.!！?？：:\s]+", "", str(text or ""))
    if not clean:
        return ""

    schools = get_school_catalog()

    aliases = []
    for school in schools:
        canonical = str(school or "").strip()
        if not canonical:
            continue

        short = re.sub(r"(?:國中|高中|國小|中學)$", "", canonical)
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
        and school_catalog_cache.get("schools")
        and now < float(school_catalog_cache.get("expires_at", 0) or 0)
    ):
        return list(school_catalog_cache["schools"])

    result = google_post(
        {"action": "list_schools"},
        timeout=10,
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

    if schools:
        school_catalog_cache["schools"] = schools
        school_catalog_cache["expires_at"] = now + 600
        return list(schools)

    return list(school_catalog_cache.get("schools", []))


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
def _other_order_school_aliases():
    aliases = {}
    for school in get_school_catalog():
        school = str(school or "").strip()
        if not school:
            continue
        aliases[school] = school
        short = re.sub(r"(?:國民中學|國民小學|高級中學|國中|國小|高中|中學|女中)$", "", school)
        if short:
            aliases[short] = school
    for key, value in {
        "天母":"天母國中","天母國中":"天母國中",
        "華興":"華興中學","華興中學":"華興中學",
        "衛理":"衛理女中","衛理女中":"衛理女中"
    }.items():
        aliases.setdefault(key,value)
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

    if school_hint:
        classes=get_teacher_classes(school_hint,teacher_name)
        if classes:
            return {"school":school_hint,"teacher":teacher_name}
    return None


def parse_other_order(user_id, text):
    clean=re.sub(r"[，,。.!！?？]+$","",str(text or "").strip())
    clean=re.sub(r"\s+","",clean)
    if not clean or clean.startswith(("查","查詢")):
        return None

    aliases=_other_order_school_aliases()
    school_hint=""
    for alias in sorted(aliases,key=len,reverse=True):
        if clean.startswith(alias):
            school_hint=aliases[alias]
            clean=clean[len(alias):]
            break

    clean=re.sub(r"^幫","",clean)

    # 姓名使用 non-greedy，避免「藍明月要買」被切成「藍明月要＋買」。
    patterns=[
        r"^(?P<teacher>[\u4e00-\u9fff]{2,4}?)老師(?:那邊)?(?:要買|購買|買|要|訂購|訂)(?P<item>.+)$",
        r"^(?P<teacher>[\u4e00-\u9fff]{2,4}?)(?:那邊)?(?:要買|購買|買|要|訂購|訂)(?P<item>.+)$",
    ]
    match=None
    for pattern in patterns:
        match=re.fullmatch(pattern,clean)
        if match:
            break
    if not match:
        return None

    teacher_raw=match.group("teacher").strip()
    item=match.group("item").strip()
    if not teacher_raw or not item:
        return None

    resolved=_resolve_other_order_teacher(user_id,teacher_raw,school_hint)
    if not resolved:
        return None

    return {"school":resolved["school"],"teacher":resolved["teacher"],"item":item}


def make_other_order_confirmation(order):
    return (
        "📦 其他訂單｜請確認\n\n"
        f"🏫 學校：{order['school']}\n"
        f"👨‍🏫 老師：{order['teacher']}\n"
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
        return "📦 查不到符合條件的其他訂單。"
    if len(orders) == 1:
        other_order_context[user_id] = orders[0]
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


def _image_class_items(data):
    raw = data.get("class_items", []) if isinstance(data, dict) else []
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
    publisher = str(data.get("publisher", "") or "").strip()
    book = clean_book_name(str(data.get("book", "") or "").strip())
    classes = _image_class_items(data)
    confidence = str(data.get("confidence", "") or "").lower()

    missing = []
    if not school: missing.append("學校")
    if not book: missing.append("書名")
    if not classes: missing.append("班級與數量")
    if confidence == "low" or missing:
        missing_text = "、".join(missing) if missing else "部分文字"
        return (
            "📷 我有讀到這是一張訂購單，但有些內容還不夠清楚。\n\n"
            f"需要再確認：{missing_text}\n\n"
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
    pending_orders[user_id] = order
    # 正式訂購單本身已提供數量，不建立 teacher class context，避免把圖片數量覆蓋成資料庫人數。
    order_flow_context.pop(user_id, None)
    guided_mode.pop(user_id, None)
    pending_name_confirmations.pop(user_id, None)
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
        return {"status": "publisher_choice", "book": exact[0]["book"], "publishers": pubs}
    if len(exact) == 1 and exact[0]["publisher"]:
        return {"status": "ok", **exact[0]}

    # 非完全相同時只在高信心且沒有出版社歧義時自動採用。
    top = candidates[0]
    top_score = float(top.get("score", 0) or 0)
    top_value = str(top.get("value", "") or "").strip()
    same_top = [c for c in candidates if str(c.get("value", "") or "").strip() == top_value]
    top_pubs = unique_list([str(c.get("publisher", "") or "").strip() for c in same_top if str(c.get("publisher", "") or "").strip()])
    if len(top_pubs) > 1:
        return {"status": "publisher_choice", "book": top_value, "publishers": top_pubs}
    if top_score >= 0.78 and top_value and str(top.get("publisher", "") or "").strip():
        return {"status": "ok", "book": top_value, "publisher": str(top.get("publisher", "") or "").strip()}
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
        for c in item.get("classes", []):
            lines.append(f"   • {c['class_name']}：{c['students']}本")
        if item.get("note"):
            lines.append(f"   📝 {item['note']}")
    if batch.get("note"):
        lines.extend(["", f"📝 共同備註：{batch['note']}"])
    lines.extend(["", "以上資料正確請回覆「確認」。", "需要取消請回覆「取消」。"])
    return "\n".join(lines)


def _photo_cram_batch_confirmation(batch):
    source_label = batch.get("source_label", "照片")
    icon = "📷" if source_label == "照片" else "🧠"
    lines = [f"{icon} {source_label}辨識完成", "", f"補習班：{batch.get('cram_school','')}", "", "📚 訂購書籍"]
    for i, item in enumerate(batch.get("items", []), 1):
        lines.append(f"{i}. {item['book']}｜{item['publisher']}｜{item['quantity']}本")
        if item.get("note"):
            lines.append(f"   📝 {item['note']}")
    if batch.get("note"):
        lines.extend(["", f"📝 共同備註：{batch['note']}"])
    lines.extend(["", "以上資料正確請回覆「確認」。", "需要取消請回覆「取消」。"])
    return "\n".join(lines)


def _continue_photo_resolution(user_id):
    batch = pending_photo_orders.get(user_id)
    if not batch:
        return None

    for idx, item in enumerate(batch.get("items", [])):
        if item.get("publisher"):
            continue
        resolved = _resolve_photo_book(item.get("book", ""))
        if resolved["status"] == "publisher_choice":
            batch["awaiting_publisher_index"] = idx
            batch["publisher_options"] = resolved["publishers"]
            item["book"] = resolved["book"]
            pending_photo_orders[user_id] = batch
            lines = ["📚 找到相同書名", "", f"書名：{resolved['book']}", "", "這本書有不同出版社："]
            for i, pub in enumerate(resolved["publishers"], 1):
                lines.append(f"{i}. {pub}")
            lines.extend(["", f"請回覆 1～{len(resolved['publishers'])}"])
            return "\n".join(lines)
        if resolved["status"] != "ok":
            pending_photo_orders.pop(user_id, None)
            return f"⚠️ 我無法從書籍資料庫確認「{item.get('book','')}」。\n\n請改用文字輸入這本書的完整書名。"
        item["book"] = resolved["book"]
        item["publisher"] = resolved["publisher"]

    batch.pop("awaiting_publisher_index", None)
    batch.pop("publisher_options", None)
    pending_photo_orders[user_id] = batch
    if batch.get("kind") == "school":
        return _photo_school_batch_confirmation(batch)
    return _photo_cram_batch_confirmation(batch)


def handle_pending_photo_order(user_id, text):
    batch = pending_photo_orders.get(user_id)
    if not batch:
        return None
    clean = re.sub(r"[\\s，,。.!！?？]+", "", str(text or ""))

    if clean in {"取消", "不要了", "這筆不要", "取消訂單"}:
        pending_photo_orders.pop(user_id, None)
        return "❌ 已取消這筆拍照訂單，Google 沒有寫入。"

    idx = batch.get("awaiting_publisher_index")
    if idx is not None:
        options = batch.get("publisher_options", [])
        choice = None
        if clean.isdigit():
            n = int(clean)
            if 1 <= n <= len(options):
                choice = options[n - 1]
        elif clean in options:
            choice = clean
        if not choice:
            return f"請回覆 1～{len(options)} 選擇出版社。"
        batch["items"][idx]["publisher"] = choice
        batch.pop("awaiting_publisher_index", None)
        batch.pop("publisher_options", None)
        pending_photo_orders[user_id] = batch
        return _continue_photo_resolution(user_id)

    if _is_confirm_word(clean):
        return confirm_photo_order(user_id)

    return (_photo_school_batch_confirmation(batch) if batch.get("kind") == "school"
            else _photo_cram_batch_confirmation(batch))


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
    lines = ["✅ 學校拍照訂單已處理", "", f"成功 {len(ok)} 筆／共 {len(results)} 筆"]
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
            "note": entry.get("note", "")
        })

    pending_photo_orders[user_id] = {
        "kind": "school",
        "teacher": teacher_info["teacher"],
        "school": teacher_info["school"],
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
            "note": str(raw.get("note", "") or "").strip()
        })

    if not items:
        return (
            "📷 我有辨識到補習班名稱，但還沒有讀到「書名＋數量」。\n\n"
            "請重新拍清楚一點，或直接用文字補充。"
        )

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
    if keep_mode and guided_mode.get(user_id) == "teacher_lookup":
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
    compact = re.sub(r"\\s+", "", str(text or ""))
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
    if cram_school and valid_cram_items:
        data["intent"] = "cram_order"
        data["items"] = valid_cram_items
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


def lookup_book_candidates_enhanced(query, publisher=""):
    query = str(query or "").strip()
    if not query:
        return []
    candidates = lookup_fuzzy_candidates("book", query, publisher=publisher)
    if not candidates:
        core = book_core_text(query)
        if core and core != query:
            candidates = lookup_fuzzy_candidates("book", core, publisher=publisher)

    # v47: same title from different publishers must remain separate.
    merged = {}
    for item in candidates:
        value = str(item.get("value", "") or "").strip()
        pub = str(item.get("publisher", "") or "").strip()
        if not value:
            continue
        score = max(book_keyword_score(query, value),
                    float(item.get("score", 0) or 0) * 0.85)
        key = (value, pub)
        if key not in merged or score > merged[key]["score"]:
            merged[key] = {"value": value, "publisher": pub, "school": "", "score": score}
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:10]

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
    elif clean in yes_words:
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
def google_post(payload, timeout=10, retries=1):
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
    if _request_budget_exceeded():
        logger.warning(f"Google call skipped (request time/call budget exceeded): action={action}")
        return None

    _register_google_call()

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
            logger.info(
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
                should_cache = not (action == "lookup_fuzzy_candidates" and not data.get("candidates"))
                if should_cache:
                    _google_read_cache[key] = {
                        "data": copy.deepcopy(data),
                        "expires_at": time.time() + cache_ttl
                    }

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

            return None

    return None


def lookup_teacher_matches(teacher, school="", grade="", subject=""):
    result = google_post({
        "action": "lookup_teacher_matches",
        "teacher": str(teacher or "").strip(),
        "school": str(school or "").strip(),
        "grade": str(grade or "").strip(),
        "subject": str(subject or "").strip()
    }, timeout=5, retries=1)

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
    result = google_post({
        "action": "lookup_orders_by_date",
        "date": today
    }, timeout=12, retries=1)

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
    result = google_post({
        "action": "create_order",
        "teacher": order["teacher"],
        "school": order["school"],
        "book": order["book"],
        "publisher": order["publisher"],
        "classes": order.get("classes", [])
    }, timeout=15, retries=1)

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
    result = google_post({
        "action": "cancel_order",
        "order_number": normalize_order_number(order_number)
    }, timeout=8, retries=2)

    return bool(result and result.get("success") is True), result or {}


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


def lookup_school_versions_all_junior_grades(school, subject="", academic_period=""):
    combined = []
    periods = []
    any_success = False
    for grade in ["七年級", "八年級", "九年級"]:
        part = lookup_school_versions(school, grade, subject, academic_period)
        if part is None:
            continue
        any_success = True
        combined.extend(part.get("versions", []))
        if part.get("latest_period"):
            periods.append(str(part.get("latest_period")))
    if not any_success:
        return None
    latest = academic_period or (max(periods) if periods else "")
    return {"latest_period": latest, "versions": combined}


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


def write_other_order_to_google_sheet(order):
    result = google_post({"action":"create_other_order","school":order["school"],"teacher":order["teacher"],"item":order["item"]})
    return bool(result and result.get("success")), result or {}


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

    if body.startswith("📄 訂購單 PDF 已經產生好了"):
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
