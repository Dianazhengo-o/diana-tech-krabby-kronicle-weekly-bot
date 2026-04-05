import anthropic
import requests
import xml.etree.ElementTree as ET
import os
from datetime import datetime, timezone, timedelta

ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
TAIWAN_TZ = timezone(timedelta(hours=8))

# ── RSS 來源────────────────────────────────────────────
RSS_FEEDS = [
    ("TechCrunch",  "https://techcrunch.com/feed/"),
    ("The Verge",   "https://www.theverge.com/rss/index.xml"),
    ("Wired",       "https://www.wired.com/feed/rss"),
    ("Ars Technica","https://feeds.arstechnica.com/arstechnica/index"),
    ("iThome",      "https://www.ithome.com.tw/rss"),
]

def fetch_rss_articles(max_per_feed=3):
    """從各 RSS 抓最新文章，回傳標題 + 網址清單"""
    articles = []
    headers = {"User-Agent": "Mozilla/5.0"}

    for source, url in RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=8, headers=headers)
            root = ET.fromstring(resp.content)

            # 同時支援 RSS 2.0 和 Atom 格式
            items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

            count = 0
            for item in items:
                title = (item.findtext("title") or
                         item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
                link  = (item.findtext("link") or
                         item.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()

                # Atom 的 link 有時是屬性而非文字
                if not link:
                    link_el = item.find("{http://www.w3.org/2005/Atom}link")
                    if link_el is not None:
                        link = link_el.get("href", "").strip()

                if title and link.startswith("https://"):
                    articles.append(f"{title} | {link} | {source}")
                    count += 1
                    if count >= max_per_feed:
                        break

            print(f"✓ {source}：抓到 {count} 篇")
        except Exception as e:
            print(f"✗ {source} 失敗：{e}")

    return articles


def pick_top5_with_claude(articles):
    """讓 Claude 從 RSS 清單中挑出最重要的 5 篇"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today  = datetime.now(TAIWAN_TZ).strftime("%Y年%m月%d日")

    article_list = "\n".join([f"{i+1}. {a}" for i, a in enumerate(articles)])

    prompt = f"""
今天是 {today}。以下是從各科技媒體 RSS 抓到的最新文章清單：

{article_list}

請從中挑出「本週最重要、最值得科技人和商學院學生關注」的 5 篇。

只輸出以下格式，不要寫任何其他文字：

1. 新聞標題 | 完整網址
2. 新聞標題 | 完整網址
3. 新聞標題 | 完整網址
4. 新聞標題 | 完整網址
5. 新聞標題 | 完整網址

標題和網址直接從上面清單複製，不可修改。
"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )

    for block in response.content:
        if block.type == "text":
            return block.text.strip()
    return ""


def write_report_with_urls(url_list_text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    today  = datetime.now(TAIWAN_TZ).strftime("%Y年%m月%d日")

    prompt = f"""
你是「黛安娜」，固定在每週五晚上為一群商學院學生與工程師撰寫科技週報的編輯。
今天是 {today}。

以下是本週 5 則科技新聞的標題和真實網址：

{url_list_text}

請根據這些新聞，撰寫本週的《黛安娜的科技蟹蟹水果報》。

【語氣要求】
- 繁體中文。語氣輕鬆自然但不輕浮，像懂產業的朋友在解說。
- 幽默感來自觀察和措辭，不靠賣萌，不加感嘆號。
- 不使用任何 emoji 或表情符號。
- 禁用詞：值得關注、深刻影響、不容忽視、劃時代、引領未來。

【讀者】
- 一半商學院：關心產業趨勢、商業模式、競爭格局、投資方向。
- 一半工程師：關心工具變化、技術方向、開發工作流、基礎設施。

【輸出格式，嚴格照做】

黛安娜的科技蟹蟹水果報｜本週科技趨勢整理

開場：
2-3 句，點出這週科技圈的主旋律。要有觀察感，不要是流水帳。

本週 5 大趨勢：

1. 新聞標題
發生什麼：一句話說清楚核心。
商業視角：對市場、商業模式、投資邏輯的意義，1-2 句。
科技視角：對開發者、技術選型、工作流程的意義，1-2 句。
來源：媒體名稱 網址直接貼，不加任何括號或標點符號，例如：來源：TechCrunch https://techcrunch.com/...

2. 新聞標題
發生什麼：
商業視角：
科技視角：
來源：

3. 新聞標題
發生什麼：
商業視角：
科技視角：
來源：

4. 新聞標題
發生什麼：
商業視角：
科技視角：
來源：

5. 新聞標題
發生什麼：
商業視角：
科技視角：
來源：

本週觀察：
2-3 句總結，這週主旋律是什麼、接下來最值得追什麼。要有自己的觀點。
"""

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}]
    )

    all_text = []
    for block in response.content:
        if block.type == "text":
            all_text.append(block.text)
    return "\n".join(all_text) if all_text else "無法取得內容"


def post_to_discord(news_text):
    today    = datetime.now(TAIWAN_TZ).strftime("%Y/%m/%d")
    week_num = datetime.now(TAIWAN_TZ).isocalendar()[1]
    LIMIT    = 3800

    chunks = [news_text] if len(news_text) <= LIMIT else [
        news_text[:news_text.rfind("\n", 0, LIMIT)],
        news_text[news_text.rfind("\n", 0, LIMIT):].strip()
    ]

    for i, chunk in enumerate(chunks):
        tag = f" ({i+1}/{len(chunks)})" if len(chunks) > 1 else ""
        payload = {
            "username": "黛安娜的科技蟹蟹水果報",
            "embeds": [{
                "title":       f"黛安娜的科技蟹蟹水果報  Week {week_num} | {today}{tag}",
                "description": chunk,
                "color":       0xC0392B,
                "footer":      {"text": "Powered by Claude AI | 每週五 18:00 發布"},
                "timestamp":   datetime.now(timezone.utc).isoformat()
            }]
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload)
        print(f"發送成功（第 {i+1} 則）" if resp.status_code in (200, 204)
              else f"失敗：{resp.status_code} — {resp.text}")


if __name__ == "__main__":
    print("Step 1：從 RSS 抓最新文章...")
    articles = fetch_rss_articles()
    print(f"共抓到 {len(articles)} 篇")

    print("\nStep 2：Claude 挑選本週 Top 5...")
    url_list = pick_top5_with_claude(articles)
    print(url_list)

    print("\nStep 3：撰寫週報...")
    news = write_report_with_urls(url_list)
    print(news)

    print("\nStep 4：發送到 Discord...")
    post_to_discord(news)
