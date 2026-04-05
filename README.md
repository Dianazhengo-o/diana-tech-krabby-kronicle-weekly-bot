# 黛安娜的科技蟹蟹水果報

每週五自動蒐集科技新聞、撰寫分析週報，並發送到 Discord 的 Multi-Agent 系統。

---

## 專案簡介

本系統透過 Anthropic Claude API 實作多 Agent 架構，自動完成從 RSS 蒐集、新聞篩選、週報撰寫、品質審查到 Discord 發送的完整流程。加入跨週記憶機制，避免重複報導相同主題。

目標讀者為商學院學生與軟體工程師，每則新聞同時提供商業視角與科技視角分析。

---

## 系統架構

```
OrchestratorAgent（主控）
    |
    |-- Step 1: 載入記憶 (newsletter_memory.json)
    |-- Step 2: CollectorAgent  -- 抓取 RSS，精選 7 篇候選
    |-- Step 3: WriterAgent     -- 從候選中選 5 篇，撰寫完整週報
    |-- Step 4: Reflection      -- 審稿，檢查格式與品質
    |-- Step 5: 發送 Discord
    |-- Step 6: 儲存記憶
```

### Agent 分工

| Agent | 使用模型 | 職責 |
|---|---|---|
| CollectorAgent | claude-haiku-4-5 | 逐一呼叫 fetch_rss，蒐集並去重後回傳 7 篇候選 |
| WriterAgent | claude-opus-4-5 | 從候選中選 5 篇，依固定格式撰寫完整週報 |
| Reflection (Critic) | claude-sonnet-4-5 | 審查草稿，檢查禁用詞、格式與網址規則，最多修改 2 輪 |
| OrchestratorAgent | — | 協調整個 Pipeline，各步驟獨立 try/except，確保容錯 |

---

## RSS 來源

- TechCrunch
- The Verge
- Wired
- Ars Technica
- iThome

---

## 週報格式

```
黛安娜的科技蟹蟹水果報｜本週科技趨勢整理

開場：
（2-3 句，點出本週主旋律）

本週 5 大趨勢：

1. 新聞標題
發生什麼：
商業視角：
科技視角：
來源：媒體名稱 https://完整網址

本週觀察：
（2-3 句，有觀點的總結）
```

---

## 記憶機制

每次發送完畢後，系統會把本週 5 篇文章的標題存入 `newsletter_memory.json`，最多保留 12 週。

下週執行時，CollectorAgent 會讀取最近 4 週的標題，主動避開高度重疊的主題，確保週報內容不重複。

---

## 安裝與設定

### 1. 安裝套件

```bash
pip install anthropic requests
```

### 2. 設定環境變數

```bash
export ANTHROPIC_API_KEY="your_api_key_here"
export DISCORD_WEBHOOK_URL="your_discord_webhook_url_here"
```

### 3. 執行

```bash
python diana_bot_agent.py
```

---

## 部署：GitHub Actions 自動排程

在 `.github/workflows/newsletter.yml` 中設定，每週五 18:00 台灣時間（UTC+8）自動執行：

```yaml
name: Diana Newsletter

on:
  schedule:
    - cron: "0 10 * * 5"   # UTC 10:00 = 台灣時間 18:00，每週五
  workflow_dispatch:         # 允許手動觸發

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install anthropic requests
      - run: python diana_bot_agent.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
```

在 GitHub repo 的 Settings > Secrets and variables > Actions 中加入 `ANTHROPIC_API_KEY` 和 `DISCORD_WEBHOOK_URL`。

---

## API 費用估算

每次執行使用三個模型，總費用約 $0.05 - $0.08 USD：

| 步驟 | 模型 | 費用 |
|---|---|---|
| CollectorAgent (x5 RSS) | claude-haiku-4-5 | 極低 |
| WriterAgent | claude-opus-4-5 | 主要費用 |
| Reflection (最多 3 輪) | claude-sonnet-4-5 | 中等 |

每月執行 4 次（每週一次），預估月費用 $0.20 - $0.32 USD。

---

## 技術架構

- **語言**: Python 3.10+
- **LLM**: Anthropic Claude API（Tool Use / ReAct Loop）
- **Agent 模式**: Multi-Agent Pipeline with Reflection and Memory
- **RSS 解析**: xml.etree.ElementTree（同時支援 RSS 2.0 和 Atom）
- **通知**: Discord Webhook（自動分段處理超長內容）
- **記憶**: JSON 本地檔案，跨週持久化
- **部署**: GitHub Actions

---

## 專案結構

```
.
├── diana_bot_agent.py        # 主程式（Multi-Agent Pipeline）
├── newsletter_memory.json    # 自動生成，儲存歷史報導標題
└── .github/
    └── workflows/
        └── newsletter.yml    # GitHub Actions 排程設定
```

