"""
黛安娜的科技蟹蟹水果報 — 自動化週報 Pipeline
============================================
架構：Multi-Agent（Collector → Writer → Critic）
觸發：每週五 18:00（台灣時間），由 GitHub Actions 執行
輸出：Discord Webhook

Agent 分工：
  CollectorAgent  — 抓取 RSS，篩選 7 篇候選文章（claude-haiku-4-5）
  WriterAgent     — 從候選中選 5 篇，撰寫完整週報（claude-opus-4-5）
  reflect_on_draft — 審稿，檢查格式與禁用詞（claude-sonnet-4-5）
  OrchestratorAgent — 總控流程，不寫內容

必要環境變數：
  ANTHROPIC_API_KEY   — Anthropic API 金鑰
  DISCORD_WEBHOOK_URL — Discord Webhook URL
"""

import anthropic
import re
import requests
import xml.etree.ElementTree as ET
import os
import json
from datetime import datetime, timezone, timedelta


# ════════════════════════════════════════════════════════════
# 環境變數與全域設定
# ════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# 台灣時區（UTC+8），所有日期與週數計算均以此為準
TAIWAN_TZ = timezone(timedelta(hours=8))

# 跨週記憶的 JSON 檔案路徑，用來避免重複報導同一主題
MEMORY_FILE = "newsletter_memory.json"

# ── 可調整的參數（改這裡就能全域生效，不需要搜尋整份程式碼）──
MAX_RSS_ITEMS      = 5      # 每個 RSS 來源最多抓幾篇文章
MEMORY_KEEP_WEEKS  = 12     # 記憶檔案最多保留幾週（約 3 個月）
MEMORY_LOAD_WEEKS  = 4      # 每次載入最近幾週給 Collector 做去重參考
DISCORD_CHUNK_LIMIT = 3800  # Discord embed description 每段字數上限（官方上限 4096）
COLLECTOR_MAX_ITER  = 15    # Collector agentic loop 最大迭代次數，防止無限迴圈
RSS_TIMEOUT        = 8      # RSS HTTP 請求 timeout（秒）

# RSS 來源字典
# key   = 給 LLM 使用的媒體名稱（需與 Tool Schema 的 description 一致）
# value = 實際的 RSS/Atom Feed URL
RSS_FEEDS = {
    "TechCrunch":  "https://techcrunch.com/feed/",
    "The Verge":   "https://www.theverge.com/rss/index.xml",
    "Wired":       "https://www.wired.com/feed/rss",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "iThome":      "https://www.ithome.com.tw/rss",
}


# ════════════════════════════════════════════════════════════
# Tool 實作
# LLM 不直接操作外部系統，而是透過這些工具函式完成。
# 所有工具函式：
#   - 輸入輸出皆為字串，介面統一
#   - 失敗時回傳錯誤字串而非 raise，確保 pipeline 不中斷
# ════════════════════════════════════════════════════════════


def tool_fetch_rss(source: str) -> str:
    """
    根據來源名稱抓取 RSS，回傳「標題 | 網址」格式的多行文字。

    支援兩種 XML 格式：
      - RSS 2.0：<item> 節點，<link> 為文字內容
      - Atom：<entry> 節點，<link href="..."> 為屬性

    參數：
      source — 媒體名稱，必須是 RSS_FEEDS 的 key 之一

    回傳：
      成功 → 多行文字，每行「標題 | https://...」
      失敗 → 錯誤訊息字串
    """
    url = RSS_FEEDS.get(source)

    # 來源名稱不在字典中，直接告知可用選項
    if not url:
        return f"找不到來源：{source}。可用來源：{', '.join(RSS_FEEDS)}"

    try:
        resp = requests.get(
            url,
            timeout=RSS_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},  # 避免被部分網站拒絕
        )
        root = ET.fromstring(resp.content)

        # 同時支援 RSS（item）和 Atom（entry）格式
        items = (
            root.findall(".//item")
            or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        )

        results = []
        for item in items[:MAX_RSS_ITEMS]:
            # 抓標題：優先 RSS，fallback 到 Atom namespace
            title = (
                item.findtext("title")
                or item.findtext("{http://www.w3.org/2005/Atom}title")
                or ""
            ).strip()

            # 抓連結：優先 RSS <link> 文字內容，再嘗試 Atom <link> 文字
            link = (
                item.findtext("link")
                or item.findtext("{http://www.w3.org/2005/Atom}link")
                or ""
            ).strip()

            # Atom 的 <link> 連結有時存在 href 屬性而非文字內容
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "").strip()

            # 只保留有標題且連結為 HTTPS 的文章
            if title and link.startswith("https://"):
                results.append(f"{title} | {link}")

        return "\n".join(results) if results else f"{source}：無法取得文章"

    except Exception as e:
        return f"{source} 失敗：{e}"


def _split_chunks(content: str, limit: int = DISCORD_CHUNK_LIMIT) -> list[str]:
    """
    將長文字切成多段，確保每段不超過 Discord embed description 的字數上限。

    切割策略：優先在換行處切，避免切斷句子中間。
    若整段沒有換行，則在 limit 處硬切。

    參數：
      content — 完整週報內容
      limit   — 每段最大字數，預設使用全域常數

    回傳：
      list[str]，每個元素為一段可發送的內容
    """
    chunks = []
    while len(content) > limit:
        # rfind 從右往左找，取得 limit 範圍內最後一個換行位置
        cut = content.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit  # 沒有換行就硬切
        chunks.append(content[:cut])
        content = content[cut:].strip()

    if content:
        chunks.append(content)

    return chunks


def tool_post_discord(content: str) -> str:
    """
    將完成的週報發送到 Discord Webhook。

    若內容超過單則 embed 上限，會自動切段並標示「(1/3)」等分頁標記。
    任一段發送失敗即停止，回傳 HTTP 錯誤資訊。

    參數：
      content — 完整週報文字

    回傳：
      成功 → 「成功發送（共 N 則訊息）」
      失敗 → 「Discord 發送失敗：HTTP XXX — ...」
    """
    today    = datetime.now(TAIWAN_TZ).strftime("%Y/%m/%d")
    week_num = datetime.now(TAIWAN_TZ).isocalendar()[1]
    chunks   = _split_chunks(content)

    for i, chunk in enumerate(chunks):
        # 多段時在標題加上分頁標記
        tag = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""

        payload = {
            "username": "黛安娜的科技蟹蟹水果報",
            "embeds": [{
                "title":       f"黛安娜的科技蟹蟹水果報  Week {week_num} | {today}{tag}",
                "description": chunk,
                "color":       0xC0392B,
                "footer":      {"text": "Powered by Claude AI | 每週五 18:00 發布"},
                "timestamp":   datetime.now(timezone.utc).isoformat(),
            }],
        }

        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)

        # Discord Webhook 成功回 200 或 204，其他視為失敗
        if resp.status_code not in (200, 204):
            return f"Discord 發送失敗：HTTP {resp.status_code} — {resp.text}"

    return f"成功發送（共 {len(chunks)} 則訊息）"


# ════════════════════════════════════════════════════════════
# MemoryStore — 跨週記憶管理
#
# 目的：避免未來幾週重複報導相同主題。
# 做法：把每週採用的新聞標題存成 JSON，Collector 下週參考去重。
#
# 設計：將原本兩個函式（load/save）共用的讀檔邏輯
#       統一收進 _load_raw()，消除重複程式碼。
# ════════════════════════════════════════════════════════════


class MemoryStore:
    """
    管理跨週新聞標題記憶的類別。

    JSON 結構範例：
    [
      {"week": 15, "date": "2026/04/10", "titles": ["標題A", "標題B", ...]},
      {"week": 16, "date": "2026/04/17", "titles": [...]}
    ]
    """

    def __init__(self, path: str = MEMORY_FILE):
        self.path = path

    def _load_raw(self) -> list:
        """
        讀取 JSON 原始資料。
        檔案不存在或損壞時，回傳空清單（視為第一期）。
        """
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def load(self) -> str:
        """
        回傳最近幾週標題的純文字格式，供 Collector 做去重參考。

        只載入最近 MEMORY_LOAD_WEEKS 週，控制 prompt 長度。

        回傳：
          有資料 → 多行文字「Week N (日期)：\n  - 標題」
          無資料 → 首次執行提示訊息
        """
        data = self._load_raw()
        if not data:
            return "尚無歷史記錄，這是第一期。"

        recent = data[-MEMORY_LOAD_WEEKS:]
        lines = []
        for week in recent:
            lines.append(f"Week {week['week']} ({week['date']})：")
            for title in week["titles"]:
                lines.append(f"  - {title}")

        return "\n".join(lines) if lines else "尚無歷史記錄。"

    def save(self, titles: list[str]) -> str:
        """
        將本週採用的標題存回 JSON，並維持最多 MEMORY_KEEP_WEEKS 週。

        參數：
          titles — 本週週報最終採用的新聞標題列表

        回傳：
          「已儲存 N 筆標題到記憶」
        """
        data = self._load_raw()
        now  = datetime.now(TAIWAN_TZ)

        data.append({
            "week":   now.isocalendar()[1],
            "date":   now.strftime("%Y/%m/%d"),
            "titles": titles,
        })

        # 滾動保留，避免檔案無限增長
        data = data[-MEMORY_KEEP_WEEKS:]

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return f"已儲存 {len(titles)} 筆標題到記憶"


# ════════════════════════════════════════════════════════════
# Tool Schema
# 這不是工具本身，而是工具的「說明書」——告訴 LLM：
#   - 工具叫什麼名字
#   - 什麼情況下呼叫它
#   - 需要傳入什麼參數
# LLM 根據 description 決定呼叫時機，根據 input_schema 決定參數格式。
# ════════════════════════════════════════════════════════════

FETCH_RSS_TOOL = {
    "name": "fetch_rss",
    "description": (
        "抓取指定科技媒體的最新文章清單，回傳標題和完整網址。"
        # 動態產生來源清單：新增/移除 RSS_FEEDS 的 key 時，這裡自動同步
        f"可用來源：{', '.join(RSS_FEEDS)}。"
        "每個來源需要分別呼叫一次。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "媒體名稱，必須完全符合可用來源之一",
            }
        },
        "required": ["source"],
    },
}

POST_DISCORD_TOOL = {
    "name": "post_discord",
    "description": (
        "把完成的週報內容發送到 Discord。"
        "只在週報完整寫好後才呼叫，蒐集資料期間不要呼叫。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "完整的週報文字，包含開場、5 則新聞分析、本週觀察",
            }
        },
        "required": ["content"],
    },
}


# ════════════════════════════════════════════════════════════
# Tool 執行器
# 負責把 LLM 指定的工具名稱對應到實際 Python 函式。
#
# 使用 dict dispatch 取代 if-else：
#   - 新增工具只需在 TOOL_DISPATCH 加一行
#   - 不需要修改 execute_tool 本體
# ════════════════════════════════════════════════════════════

TOOL_DISPATCH: dict = {
    "fetch_rss":    lambda i: tool_fetch_rss(i["source"]),
    "post_discord": lambda i: tool_post_discord(i["content"]),
}


def execute_tool(name: str, inputs: dict) -> str:
    """
    根據工具名稱執行對應工具函式。

    參數：
      name   — LLM 指定的工具名稱
      inputs — LLM 傳入的參數字典

    回傳：
      工具執行結果字串；工具不存在時回傳錯誤訊息
    """
    fn = TOOL_DISPATCH.get(name)
    if fn is None:
        return f"未知工具：{name}"

    result = fn(inputs)
    # 統一 log 格式，截斷過長輸出
    print(f"    [tool] {name}: {result[:80]}{'...' if len(result) > 80 else ''}")
    return result


# ════════════════════════════════════════════════════════════
# Reflection（Self-Critique Pattern）
# Writer 產出草稿後，由另一個獨立 Prompt 的模型扮演「主編」審稿。
# 目的：用不同視角發現 Writer 可能忽略的格式問題或禁用詞。
#
# 審稿流程（最多 max_retries + 1 輪）：
#   APPROVED → 立即回傳，不再繼續
#   REVISED  → 取出修正稿，進入下一輪
#   其他格式  → 輸出異常，保留現版本 break
#   超過輪數  → 回傳最後一版（不讓審稿失敗阻斷整個 pipeline）
# ════════════════════════════════════════════════════════════

CRITIC_SYSTEM = """你是資深科技媒體主編，負責審查週報草稿。
審查標準（全部符合才算通過）：
1. 禁用詞不得出現：值得關注、深刻影響、不容忽視、劃時代、引領未來
2. 不得有任何 emoji 或表情符號
3. 網址必須裸露貼出，格式為「來源：媒體名稱 https://...」
   不可使用 Markdown [文字](連結) 格式，也不可加任何括號包住網址
4. 每則新聞必須同時包含「商業視角」和「科技視角」兩個段落
5. 開場和本週觀察需有具體觀點，不能只是陳述事實的摘要

回覆規則：
- 若草稿完全符合，僅回覆一行：APPROVED
- 若有問題，依以下格式回覆：
ISSUES
（條列具體問題，每條一行）
REVISED
（修改後的完整稿件）"""


def reflect_on_draft(client: anthropic.Anthropic, draft: str, max_retries: int = 2) -> str:
    """
    讓模型扮演主編，審查草稿並在必要時修正（最多 max_retries + 1 輪）。

    參數：
      client      — Anthropic client
      draft       — Writer 產生的原始草稿
      max_retries — 最多允許修正幾輪（預設 2，共 3 輪）

    回傳：
      通過審查或最後一版的稿件字串
    """
    current = draft

    for attempt in range(max_retries + 1):
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=CRITIC_SYSTEM,
            messages=[{"role": "user", "content": f"請審查以下草稿：\n\n{current}"}],
        )
        text = resp.content[0].text.strip()

        if text.startswith("APPROVED"):
            print(f"  [reflection] 第 {attempt + 1} 次審查通過")
            return current

        if "REVISED" in text:
            idx     = text.index("REVISED") + len("REVISED")
            current = text[idx:].strip()
            print(f"  [reflection] 第 {attempt + 1} 次：發現問題，已修改")
        else:
            # 模型輸出格式不符預期，停止審稿
            print(f"  [reflection] 第 {attempt + 1} 次：格式異常，保留現版本")
            break

    print("  [reflection] 已達最大審查次數，使用最後版本")
    return current


# ════════════════════════════════════════════════════════════
# Agent 定義
# ════════════════════════════════════════════════════════════


class CollectorAgent:
    """
    蒐集型 Agent（使用 claude-haiku-4-5）。

    採用 ReAct 模式（Reason + Act）：
      每輪讓模型決定呼叫哪個工具 → 執行 → 把結果餵回模型 → 重複
    直到模型宣告完成（stop_reason == "end_turn"）或達到 COLLECTOR_MAX_ITER。

    職責：
      1. 逐一呼叫 fetch_rss 抓取全部 5 個來源
      2. 對照記憶，排除近期已報導的重複主題
      3. 輸出 7 篇候選（多 2 篇緩衝供 Writer 挑選）
    """

    # System prompt 定義 Collector 的角色、任務與輸出格式
    SYSTEM = """你是資料蒐集員，負責抓取 RSS 並精選文章。

任務：
1. 逐一呼叫 fetch_rss 抓取全部 5 個來源
2. 對照已報導標題，排除高度重疊的主題
3. 挑出 7 篇候選（多給 2 篇緩衝供後續撰稿員選擇）

輸出格式（純文字，每篇一行，不要分析）：
來源名稱 | 文章標題 | 網址

只輸出這 7 行，不加任何其他文字。"""

    def run(self, client: anthropic.Anthropic, today: str, memory_context: str) -> str:
        """
        執行 ReAct 迴圈直到取得候選文章清單。

        參數：
          client         — Anthropic client
          today          — 今天日期字串（用於 prompt）
          memory_context — 近幾週已報導標題（用於去重）

        回傳：
          候選文章清單字串；異常時回傳空字串
        """
        print("\n[Collector Agent] 開始蒐集文章...")

        # 初始 user message：把日期、歷史記憶與任務要求一起交給模型
        messages = [{
            "role": "user",
            "content": (
                f"今天是 {today}。\n\n"
                f"以下是過去已報導的標題，請避開相似主題：\n{memory_context}\n\n"
                "請逐一呼叫 fetch_rss 抓取全部 5 個來源，挑出 7 篇候選文章。"
            ),
        }]

        for _ in range(COLLECTOR_MAX_ITER):
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2048,
                system=self.SYSTEM,
                tools=[FETCH_RSS_TOOL],
                messages=messages,
            )

            # 把模型這輪的輸出加入對話歷史
            messages.append({"role": "assistant", "content": resp.content})

            # 模型完成，不再需要工具 → 取出文字結果
            if resp.stop_reason == "end_turn":
                for block in resp.content:
                    if hasattr(block, "text") and block.text.strip():
                        print(f"  [Collector] 完成（{block.text.count(chr(10)) + 1} 篇候選）")
                        return block.text
                return ""

            # 模型要求呼叫工具 → 執行並把結果餵回
            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })
                messages.append({"role": "user", "content": tool_results})

        print("  ! [Collector] 已達最大迭代次數，強制終止")
        return ""


class WriterAgent:
    """
    撰稿型 Agent（使用 claude-opus-4-5）。

    不需要工具，單次 API 呼叫即完成。
    選用 Opus 原因：輸出是給真實讀者看的內容，品質直接影響使用者體驗。

    職責：
      從 7 篇候選中選 5 篇，依固定格式撰寫完整週報草稿。
    """

    # 語氣與格式規則
    # 注意：正確範例為裸露網址，不可使用 Markdown 超連結語法
    SYSTEM = """你是「黛安娜」，每週為商學院學生與工程師撰寫科技週報的編輯。

語氣要求：
- 繁體中文，輕鬆自然但不輕浮，像懂產業的朋友在解說
- 幽默感來自觀察和措辭，不靠賣萌，不加感嘆號
- 不使用任何 emoji 或表情符號
- 禁用詞：值得關注、深刻影響、不容忽視、劃時代、引領未來

網址格式規則（嚴格遵守）：
- 網址直接裸露貼出，不加任何括號或 Markdown 格式
- 正確：來源：TechCrunch https://techcrunch.com/2026/04/04/example
- 錯誤：來源：TechCrunch [https://techcrunch.com/...](https://techcrunch.com/...)"""

    # 固定週報格式模板
    NEWSLETTER_FORMAT = """黛安娜的科技蟹蟹水果報｜本週科技趨勢整理

開場：
（2-3 句，點出本週主旋律，有觀察感）

本週 5 大趨勢：

1. 新聞標題
發生什麼：（一句話說清楚核心）
商業視角：（對市場、商業模式、投資邏輯的意義，1-2 句）
科技視角：（對開發者、技術選型、工作流程的意義，1-2 句）
來源：媒體名稱 https://完整網址

（以此類推到第 5 則）

本週觀察：
（2-3 句，有自己的觀點）"""

    def run(self, client: anthropic.Anthropic, today: str, articles: str) -> str:
        """
        根據候選文章撰寫完整週報草稿（單次 API 呼叫）。

        參數：
          client   — Anthropic client
          today    — 今天日期字串
          articles — Collector 回傳的候選文章清單

        回傳：
          完整週報草稿字串
        """
        print("\n[Writer Agent] 開始撰寫週報...")

        resp = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=4096,
            system=self.SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"今天是 {today}。\n\n"
                    f"以下是精選候選文章：\n{articles}\n\n"
                    f"請從中挑 5 篇，依以下格式撰寫週報。只輸出週報內文，不要任何說明。\n\n"
                    f"{self.NEWSLETTER_FORMAT}"
                ),
            }],
        )

        draft = resp.content[0].text
        print(f"  [Writer] 草稿完成（{len(draft)} 字）")
        return draft


# ════════════════════════════════════════════════════════════
# Orchestrator — Pipeline 總控
#
# 職責：決定步驟順序與錯誤處理策略，不寫任何內容。
#
# 錯誤處理分三級：
#   核心失敗（Collector / Writer / Discord）→ return 終止，無意義繼續
#   品質保障（Reflection）→ 降級用原稿，週報仍需發出
#   附加功能（Memory 儲存）→ 僅警告，本週任務已完成
# ════════════════════════════════════════════════════════════


class OrchestratorAgent:
    """協調整個多代理流程的主控，不產生任何內容。"""

    def run(self):
        """執行完整 6 步 pipeline。"""
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        today  = datetime.now(TAIWAN_TZ).strftime("%Y年%m月%d日")
        memory = MemoryStore()

        print("=" * 55)
        print("  黛安娜的科技蟹蟹水果報  ·  Agent Pipeline 啟動")
        print("=" * 55)

        # ── Step 1：載入記憶 ──────────────────────────────────
        print("\n[Step 1] 載入歷史記憶")
        memory_context = memory.load()
        preview = memory_context[:120] + "..." if len(memory_context) > 120 else memory_context
        print(f"  {preview}")

        # ── Step 2：Collector ─────────────────────────────────
        print("\n[Step 2] Collector Agent 蒐集候選文章")
        try:
            articles = CollectorAgent().run(client, today, memory_context)
        except Exception as e:
            print(f"  ! [Step 2] Collector 失敗：{e}")
            return  # 沒有文章無法繼續

        # 最低品質門檻：至少要有 4 篇候選
        if not articles or articles.count("\n") < 3:
            print("  ! [Step 2] Collector 回傳文章不足（少於 4 篇），終止 Pipeline。")
            return

        # ── Step 3：Writer ────────────────────────────────────
        print("\n[Step 3] Writer Agent 撰寫週報")
        try:
            draft = WriterAgent().run(client, today, articles)
        except Exception as e:
            print(f"  ! [Step 3] Writer 失敗：{e}")
            return  # 沒有草稿無法繼續

        # ── Step 4：Reflection ────────────────────────────────
        print("\n[Step 4] Reflection 審稿")
        try:
            final_draft = reflect_on_draft(client, draft)
        except Exception as e:
            # 審稿失敗降級處理：用原稿繼續，週報仍需發出
            print(f"  ! [Step 4] Reflection 失敗，降級使用原稿：{e}")
            final_draft = draft

        # ── Step 5：發送 Discord ──────────────────────────────
        print("\n[Step 5] 發送到 Discord")
        try:
            result = tool_post_discord(final_draft)
            print(f"  {result}")
        except Exception as e:
            print(f"  ! [Step 5] Discord 發送失敗：{e}")
            return  # 核心任務未完成

        # ── Step 6：儲存記憶 ──────────────────────────────────
        # 此步驟失敗不影響已發出的週報，只記錄警告
        print("\n[Step 6] 儲存本週標題到記憶")
        try:
            titles      = _extract_titles(final_draft)
            save_result = memory.save(titles)
            print(f"  {save_result}")
        except Exception as e:
            print(f"  ! [Step 6] 記憶儲存失敗（不影響已發送的週報）：{e}")

        print("\n" + "=" * 55)
        print("  Pipeline 完成")
        print("=" * 55)


# ════════════════════════════════════════════════════════════
# 輔助函式
# ════════════════════════════════════════════════════════════


def _extract_titles(newsletter: str) -> list[str]:
    """
    從週報內文中用正則抽出 5 則新聞標題。

    匹配格式：行首數字 + 半形或全形句點 + 空白 + 標題文字
    例如：「1. OpenAI 宣布新模型」或「2．Meta 發布 AR 眼鏡」

    正則說明：
      ^\d+    — 行首數字（1, 2, 10...）
      [.．]  — 半形句點或全形句點
      \s*     — 句點後零個或多個空白（\s* 比 \s+ 更寬鬆）
      (.+)    — capture group，取出標題文字

    參數：
      newsletter — 完整週報文字

    回傳：
      最多 5 個標題的字串列表
    """
    titles = []
    for line in newsletter.splitlines():
        m = re.match(r"^\d+[.．]\s*(.+)", line.strip())
        if m:
            titles.append(m.group(1).strip())
        if len(titles) >= 5:
            break
    return titles


# ════════════════════════════════════════════════════════════
# Entry Point
# 直接執行此檔案時啟動 pipeline；被其他模組 import 時不執行。
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    OrchestratorAgent().run()
