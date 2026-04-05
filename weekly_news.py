import anthropic
import re
import requests
import xml.etree.ElementTree as ET
import os
import json
from datetime import datetime, timezone, timedelta


# 讀取執行程式時需要的環境變數。
# ANTHROPIC_API_KEY：用來呼叫 Anthropic API。
# DISCORD_WEBHOOK_URL：用來把完成的週報發送到 Discord。
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# 定義台灣時區，之後所有日期與週數都用台灣時間計算。
TAIWAN_TZ = timezone(timedelta(hours=8))

# 定義記憶檔案名稱，用來存放每週已報導的標題。
MEMORY_FILE = "newsletter_memory.json"


# 定義可抓取的 RSS 來源。
# key 是給 LLM 使用的來源名稱，value 是實際的 RSS URL。
RSS_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Wired": "https://www.wired.com/feed/rss",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
    "iThome": "https://www.ithome.com.tw/rss",
}


# ════════════════════════════════════════════════════════════
# Tool 實作
# 這一區是真正會做事的函式。
# LLM 不會直接抓網頁或發 Discord，而是透過這些工具函式完成。
# ════════════════════════════════════════════════════════════


def tool_fetch_rss(source: str) -> str:
    """
    根據來源名稱抓取 RSS。

    參數：
    - source：來源名稱，例如 "TechCrunch"、"Wired"

    回傳：
    - 成功時：多行文字，每行格式為「標題 | 網址」
    - 失敗時：錯誤訊息字串
    """
    # 根據來源名稱查出對應的 RSS URL。
    url = RSS_FEEDS.get(source)

    # 如果輸入的來源名稱不存在，直接回傳可用來源清單。
    if not url:
        return f"找不到來源：{source}。可用來源：{', '.join(RSS_FEEDS)}"

    try:
        # 發送 HTTP GET 請求抓 RSS 內容。
        # timeout=8 代表最久等 8 秒。
        # headers 中放 User-Agent，可降低部分網站拒絕請求的機率。
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})

        # 將 RSS / Atom XML 內容解析成 ElementTree。
        root = ET.fromstring(resp.content)

        # 兼容兩種常見格式：
        # 1. RSS：item
        # 2. Atom：entry
        items = (
            root.findall(".//item")
            or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        )

        # 用來收集最後要回傳的文章列表。
        results = []

        # 只取前 5 篇，避免一次餵給模型太多內容。
        for item in items[:5]:
            # 嘗試抓文章標題。
            # 先抓一般 RSS 的 title，再抓 Atom 的 title。
            title = (
                item.findtext("title")
                or item.findtext("{http://www.w3.org/2005/Atom}title")
                or ""
            ).strip()

            # 嘗試抓文章連結。
            # 先抓一般 RSS 的 link，再抓 Atom 的 link。
            link = (
                item.findtext("link")
                or item.findtext("{http://www.w3.org/2005/Atom}link")
                or ""
            ).strip()

            # 有些 Atom feed 的 link 不在文字內容，而是在 href 屬性裡。
            # 如果前面沒抓到，就改從 link element 的 href 取值。
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                if link_el is not None:
                    link = link_el.get("href", "").strip()

            # 只保留：
            # 1. 有標題
            # 2. 連結是 https:// 開頭
            if title and link.startswith("https://"):
                results.append(f"{title} | {link}")

        # 如果有抓到文章，就用換行串起來。
        # 如果沒有抓到，就回傳該來源無法取得文章。
        return "\n".join(results) if results else f"{source}：無法取得文章"

    except Exception as e:
        # 任一錯誤都包成字串回傳，避免工具直接中斷整個流程。
        return f"{source} 失敗：{e}"


def _split_chunks(content: str, limit: int = 3800) -> list[str]:
    """
    將長文字切成多段，避免單則 Discord embed description 過長。

    參數：
    - content：完整週報內容
    - limit：每段最大長度，預設 3800，保留一些安全空間

    回傳：
    - 字串列表，每個元素是一段可送出的內容
    """
    # 用來存放切割後的段落。
    chunks = []

    # 當內容長度超過限制時，持續切割。
    while len(content) > limit:
        # 優先在限制範圍內找最後一個換行，讓切點自然一點。
        cut = content.rfind("\n", 0, limit)

        # 如果這段內容完全沒有換行，就直接硬切在 limit。
        if cut == -1:
            cut = limit

        # 把前半段加入結果。
        chunks.append(content[:cut])

        # 剩下的內容繼續處理。
        # strip() 用來去掉切段後開頭可能殘留的空白或換行。
        content = content[cut:].strip()

    # 迴圈結束後，剩餘內容如果不為空，也加入結果。
    if content:
        chunks.append(content)

    return chunks


def tool_post_discord(content: str) -> str:
    """
    將完成的週報發送到 Discord Webhook。

    參數：
    - content：完整週報內容

    回傳：
    - 成功時：成功訊息
    - 失敗時：HTTP 錯誤資訊
    """
    # 產生今天日期，顯示在 Discord embed title。
    today = datetime.now(TAIWAN_TZ).strftime("%Y/%m/%d")

    # 取得當前 ISO week number，用於週報標題。
    week_num = datetime.now(TAIWAN_TZ).isocalendar()[1]

    # 先把內容切成 Discord 可接受的多段。
    chunks = _split_chunks(content)

    # 逐段發送到 Discord。
    for i, chunk in enumerate(chunks):
        # 如果不只一段，就在標題後面標示目前是第幾段。
        tag = f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""

        # Discord Webhook payload。
        payload = {
            "username": "黛安娜的科技蟹蟹水果報",
            "embeds": [{
                "title": f"黛安娜的科技蟹蟹水果報  Week {week_num} | {today}{tag}",
                "description": chunk,
                "color": 0xC0392B,
                "footer": {"text": "Powered by Claude AI | 每週五 18:00 發布"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
        }

        # 將 payload 送到 Discord。
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)

        # Discord Webhook 成功通常回 200 或 204。
        # 只要不是這兩個，就視為失敗並停止後續發送。
        if resp.status_code not in (200, 204):
            return f"Discord 發送失敗：HTTP {resp.status_code} — {resp.text}"

    # 全部段落都送出後，回傳成功訊息。
    return f"成功發送（共 {len(chunks)} 則訊息）"


# ── Memory ────────────────────────────────────────────────
# 這一區處理長期記憶。
# 目的不是讓模型記住所有細節，而是避免未來幾週一直重複報導同一主題。
# ──────────────────────────────────────────────────────────


def tool_load_memory() -> str:
    """
    讀取最近幾週已報導的新聞標題，提供給 Collector 做去重參考。

    回傳：
    - 若沒有記憶檔：回傳首次執行訊息
    - 若檔案損壞：回傳重置訊息
    - 若成功：回傳整理好的多行文字
    """
    # 如果記憶檔不存在，代表尚未建立歷史資料。
    if not os.path.exists(MEMORY_FILE):
        return "尚無歷史記錄，這是第一期。"

    # 嘗試讀取 JSON 記憶檔。
    # 如果檔案壞掉或讀不到，就視為重新開始。
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return "記憶檔案損壞，視為第一期重新開始。"

    # 只取最近 4 週，避免 prompt 太長。
    recent = data[-4:]

    # 把記憶整理成容易讓模型理解的文字格式。
    lines = []
    for week in recent:
        lines.append(f"Week {week['week']} ({week['date']})：")
        for title in week["titles"]:
            lines.append(f"  - {title}")

    # 若有資料就回傳整理後文字，否則回傳空記憶訊息。
    return "\n".join(lines) if lines else "尚無歷史記錄。"


def tool_save_memory(titles: list[str]) -> str:
    """
    將本週實際使用的新聞標題寫入記憶檔。

    參數：
    - titles：本週週報中最終採用的標題列表

    回傳：
    - 成功寫入後的訊息
    """
    # 預設先建立空清單。
    data = []

    # 如果記憶檔存在，先讀進來。
    # 如果讀不到或檔案損壞，就用空清單重新開始。
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = []

    # 取得現在的週數與日期。
    now = datetime.now(TAIWAN_TZ)

    # 把本週資料加進記憶中。
    data.append({
        "week": now.isocalendar()[1],
        "date": now.strftime("%Y/%m/%d"),
        "titles": titles,
    })

    # 最多只保留最近 12 週，避免檔案無限長大。
    data = data[-12:]

    # 將最新記憶寫回 JSON 檔。
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return f"已儲存 {len(titles)} 筆標題到記憶"


# ════════════════════════════════════════════════════════════
# Tool Schema
# 這一區不是工具本身，而是把工具的名稱、用途、輸入格式告訴 LLM。
# LLM 會根據這些 schema 決定何時呼叫工具，以及傳入什麼參數。
# ════════════════════════════════════════════════════════════


FETCH_RSS_TOOL = {
    "name": "fetch_rss",
    "description": (
        "抓取指定科技媒體的最新文章清單，回傳標題和完整網址。"
        "可用來源：TechCrunch, The Verge, Wired, Ars Technica, iThome。"
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
# 這一區負責把 LLM 想呼叫的工具名稱，對應到實際 Python 函式。
# 也就是說，LLM 說「我要用 fetch_rss」，程式就在這裡真正執行。
# ════════════════════════════════════════════════════════════


def execute_tool(name: str, inputs: dict) -> str:
    """
    根據工具名稱執行對應工具函式。

    參數：
    - name：工具名稱
    - inputs：工具輸入參數

    回傳：
    - 該工具執行後的字串結果
    """
    # 如果要執行的是抓 RSS 工具。
    if name == "fetch_rss":
        result = tool_fetch_rss(inputs["source"])
        print(f"    [tool] fetch_rss({inputs['source']}): {result.count(chr(10)) + 1} 篇")
        return result

    # 如果要執行的是發送 Discord 工具。
    if name == "post_discord":
        result = tool_post_discord(inputs["content"])
        print(f"    [tool] post_discord: {result}")
        return result

    # 如果工具名稱不認得，回傳未知工具訊息。
    return f"未知工具：{name}"


# ════════════════════════════════════════════════════════════
# Reflection
# 這一區負責「第二次檢查」。
# Writer 先寫草稿，然後 Critic 再審一次，確認格式、禁用詞與規則是否符合。
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
    讓另一個模型扮演主編，審查週報草稿並在必要時修正。

    參數：
    - client：Anthropic client
    - draft：Writer 產生的草稿
    - max_retries：最多允許修正幾輪

    回傳：
    - 通過審查後的最終稿件
    - 如果審查流程異常，回傳目前版本
    """
    # current 代表目前正在被審查的版本。
    # 一開始先等於原始草稿，後續若有修正，就會被替換。
    current = draft

    # 最多進行 max_retries + 1 次嘗試。
    # 例如 max_retries=2，代表最多跑 3 輪審查。
    for attempt in range(max_retries + 1):
        # 呼叫模型進行審稿。
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=CRITIC_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"請審查以下草稿：\n\n{current}"
            }],
        )

        # 取出模型回傳的文字內容。
        text = resp.content[0].text.strip()

        # 如果模型直接回覆 APPROVED，表示目前稿件已通過。
        if text.startswith("APPROVED"):
            print(f"  [reflection] 第 {attempt + 1} 次審查通過")
            return current

        # 如果模型回傳中含有 REVISED，代表它提供了修正版。
        if "REVISED" in text:
            # 找到 REVISED 出現的位置，後面的內容視為新稿件。
            idx = text.index("REVISED") + len("REVISED")
            current = text[idx:].strip()
            print(f"  [reflection] 第 {attempt + 1} 次：發現問題，已修改")
        else:
            # 如果既沒有 APPROVED，也沒有 REVISED，
            # 代表模型輸出格式不符合預期，直接停止審稿流程。
            print(f"  [reflection] 第 {attempt + 1} 次：格式異常，保留現版本")
            break

    # 如果超過最大次數仍未正式通過，就回傳最後一版。
    print("  [reflection] 已達最大審查次數，使用最後版本")
    return current


# ════════════════════════════════════════════════════════════
# Multi-Agent
# 這一區把任務分成多個角色：
# 1. CollectorAgent：蒐集候選文章
# 2. WriterAgent：撰寫週報
# 3. OrchestratorAgent：負責協調整個流程
# ════════════════════════════════════════════════════════════


class CollectorAgent:
    """
    蒐集型 Agent。

    職責：
    - 呼叫 fetch_rss 逐一抓取各媒體 RSS
    - 參考記憶，避開過去幾週已經報過的重複主題
    - 從候選文章中整理出 7 篇文章給 Writer 使用
    """

    # 系統提示詞：定義 Collector 的角色、任務與輸出格式。
    SYSTEM = """你是資料蒐集員，負責抓取 RSS 並精選文章。

任務：
1. 逐一呼叫 fetch_rss 抓取全部 5 個來源
2. 對照已報導標題，排除高度重疊的主題
3. 挑出 7 篇候選（多給 2 篇緩衝供後續撰稿員選擇）

輸出格式（純文字，每篇一行，不要分析）：
來源名稱 | 文章標題 | 網址

只輸出這 7 行，不加任何其他文字。"""

    # 最多允許模型迭代 15 次，避免無限迴圈。
    MAX_ITER = 15

    def run(self, client: anthropic.Anthropic, today: str, memory_context: str) -> str:
        """
        執行 Collector Agent。

        參數：
        - client：Anthropic client
        - today：今天日期字串
        - memory_context：最近幾週已報導標題的文字內容

        回傳：
        - 候選文章清單字串
        - 若失敗則回傳空字串
        """
        print("\n[Collector Agent] 開始蒐集文章...")

        # 初始化對話。
        # user message 會把日期、歷史記憶與任務要求一起交給 Collector。
        messages = [{
            "role": "user",
            "content": (
                f"今天是 {today}。\n\n"
                f"以下是過去已報導的標題，請避開相似主題：\n{memory_context}\n\n"
                "請逐一呼叫 fetch_rss 抓取全部 5 個來源，挑出 7 篇候選文章。"
            ),
        }]

        # 在最大迭代次數內，持續讓模型思考、呼叫工具、接收結果。
        for iteration in range(self.MAX_ITER):
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2048,
                system=self.SYSTEM,
                tools=[FETCH_RSS_TOOL],
                messages=messages,
            )

            # 把模型這一輪的輸出加入對話歷史。
            messages.append({"role": "assistant", "content": resp.content})

            # 如果模型表示這一輪已完成，不再需要工具，就嘗試取出文字結果。
            if resp.stop_reason == "end_turn":
                for block in resp.content:
                    if hasattr(block, "text") and block.text.strip():
                        print(f"  [Collector] 完成（{block.text.count(chr(10)) + 1} 篇候選）")
                        return block.text
                return ""

            # 如果模型要求呼叫工具，就逐一執行工具。
            if resp.stop_reason == "tool_use":
                tool_results = []

                for block in resp.content:
                    if block.type == "tool_use":
                        # 根據模型指定的工具名稱與參數去執行。
                        result = execute_tool(block.name, block.input)

                        # 把工具執行結果包成 tool_result，
                        # 下一輪再餵回模型，讓模型繼續決策。
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # 把所有工具結果加入對話歷史，交還給模型。
                messages.append({"role": "user", "content": tool_results})

        # 如果跑到最大輪數還沒結束，代表流程異常，直接終止。
        print("  ! [Collector] 已達最大迭代次數，強制終止")
        return ""


class WriterAgent:
    """
    撰稿型 Agent。

    職責：
    - 接收 Collector 整理好的候選文章
    - 從中選出 5 篇
    - 依固定格式撰寫完整週報
    """

    # 系統提示詞：定義 Writer 的寫作風格與格式規則。
    SYSTEM = """你是「黛安娜」，每週為商學院學生與工程師撰寫科技週報的編輯。

語氣要求：
- 繁體中文，輕鬆自然但不輕浮，像懂產業的朋友在解說
- 幽默感來自觀察和措辭，不靠賣萌，不加感嘆號
- 不使用任何 emoji 或表情符號
- 禁用詞：值得關注、深刻影響、不容忽視、劃時代、引領未來

網址格式規則（嚴格遵守）：
- 網址直接貼出，不加任何括號或 Markdown 格式
- 正確：來源：TechCrunch [https://techcrunch.com/2026/04/04/example](https://techcrunch.com/2026/04/04/example)
- 錯誤：來源：TechCrunch [https://techcrunch.com/...](https://techcrunch.com/...)"""

    # 這是 Writer 要遵守的固定週報格式模板。
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
        執行 Writer Agent。

        參數：
        - client：Anthropic client
        - today：今天日期字串
        - articles：Collector 回傳的候選文章清單

        回傳：
        - Writer 產生的完整週報草稿
        """
        print("\n[Writer Agent] 開始撰寫週報...")

        # 將日期、候選文章與固定格式一併交給 Writer。
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

        # 取出模型生成的草稿。
        draft = resp.content[0].text

        # 印出字數，方便觀察輸出長度。
        print(f"  [Writer] 草稿完成（{len(draft)} 字）")

        return draft


# ════════════════════════════════════════════════════════════
# Orchestrator
# 這是整個 pipeline 的總控。
# 它不負責寫內容，而是負責決定整個順序：
# 1. 載入記憶
# 2. 蒐集候選文章
# 3. 撰寫草稿
# 4. 審稿
# 5. 發送 Discord
# 6. 儲存記憶
# ════════════════════════════════════════════════════════════


class OrchestratorAgent:
    """
    協調整個多代理流程的主控 Agent。
    """

    def run(self):
        """
        執行完整 pipeline。
        """
        # 建立 Anthropic client，用來呼叫所有模型。
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        # 取得今天日期，作為 prompt 內容的一部分。
        today = datetime.now(TAIWAN_TZ).strftime("%Y年%m月%d日")

        # 印出啟動標題。
        print("=" * 55)
        print("  黛安娜的科技蟹蟹水果報  ·  Agent Pipeline 啟動")
        print("=" * 55)

        # ── Step 1：載入記憶 ──────────────────────────────────
        # 先讀出最近幾週的已報導標題，讓後面的 Collector 可以避開重複主題。
        print("\n[Step 1] 載入歷史記憶")
        memory_context = tool_load_memory()

        # 只印前 120 個字做預覽，避免終端輸出太長。
        preview = memory_context[:120] + "..." if len(memory_context) > 120 else memory_context
        print(f"  {preview}")

        # ── Step 2：Collector ─────────────────────────────────
        # 讓 Collector Agent 去抓 RSS 並精選候選文章。
        print("\n[Step 2] Collector Agent 蒐集候選文章")
        try:
            articles = CollectorAgent().run(client, today, memory_context)
        except Exception as e:
            # 如果 Collector 整段流程出錯，就停止整個 pipeline。
            print(f"  ! [Step 2] Collector 失敗：{e}")
            return

        # 基本驗證：如果文章太少，代表候選結果不足，不繼續往下。
        if not articles or articles.count("\n") < 3:
            print("  ! [Step 2] Collector 回傳文章不足（少於 4 篇），終止 Pipeline。")
            return

        # ── Step 3：Writer ────────────────────────────────────
        # 讓 Writer Agent 根據候選文章撰寫完整週報。
        print("\n[Step 3] Writer Agent 撰寫週報")
        try:
            draft = WriterAgent().run(client, today, articles)
        except Exception as e:
            # Writer 出錯就直接終止，因為沒有草稿可往下走。
            print(f"  ! [Step 3] Writer 失敗：{e}")
            return

        # ── Step 4：Reflection ────────────────────────────────
        # 將 Writer 草稿交給 Critic 審稿，檢查格式、禁用詞與品質。
        print("\n[Step 4] Reflection 審稿")
        try:
            final_draft = reflect_on_draft(client, draft)
        except Exception as e:
            # 如果審稿失敗，不中斷整個流程，而是降級使用原始草稿。
            print(f"  ! [Step 4] Reflection 失敗，使用未審稿的草稿：{e}")
            final_draft = draft  # 降級：直接用原稿

        # ── Step 5：發送 Discord ──────────────────────────────
        # 將最終稿送到 Discord。
        print("\n[Step 5] 發送到 Discord")
        try:
            result = tool_post_discord(final_draft)
            print(f"  {result}")
        except Exception as e:
            # 發送失敗時直接終止，因為核心任務沒有完成。
            print(f"  ! [Step 5] Discord 發送失敗：{e}")
            return

        # ── Step 6：儲存記憶 ──────────────────────────────────
        # 從最終週報中抽出標題，存回記憶檔，供未來幾週去重使用。
        print("\n[Step 6] 儲存本週標題到記憶")
        try:
            titles = _extract_titles(final_draft)
            save_result = tool_save_memory(titles)
            print(f"  {save_result}")
        except Exception as e:
            # 記憶儲存失敗不影響本週已經發出的週報，因此只記錄警告。
            print(f"  ! [Step 6] 記憶儲存失敗（不影響已發送的週報）：{e}")

        # 全部流程完成後，印出結束訊息。
        print("\n" + "=" * 55)
        print("  Pipeline 完成")
        print("=" * 55)


def _extract_titles(newsletter: str) -> list[str]:
    """
    從週報內文中抽出 5 則新聞標題。

    作法：
    - 掃描每一行
    - 尋找像「1. 標題」或「2．標題」這種編號格式
    - 把標題文字取出來
    - 最多取前 5 筆
    """
    titles = []

    for line in newsletter.splitlines():
        # 正則解釋：
        # ^\d+        -> 開頭必須是數字，例如 1、2、10
        # [.．]       -> 接半形句點或全形句點
        # \s+         -> 句點後至少一個空白
        # (.+)        -> 把後面的標題文字抓出來
        m = re.match(r"^\d+[.．]\s+(.+)", line.strip())

        # 如果符合標題格式，就把標題加入列表。
        if m:
            titles.append(m.group(1).strip())

        # 最多只取 5 筆。
        if len(titles) >= 5:
            break

    return titles


# ════════════════════════════════════════════════════════════
# Entry Point
# 這裡是程式入口。
# 只有當這個檔案是直接執行時，才會啟動整個 pipeline。
# 如果這個檔案被其他 Python 檔 import，則不會自動執行。
# ════════════════════════════════════════════════════════════


if __name__ == "__main__":
    OrchestratorAgent().run()
