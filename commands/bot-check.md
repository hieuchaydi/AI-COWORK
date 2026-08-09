---
name: bot-check
description: Health check 1 bot Telegram (mặc định @tagent_telegram_bot)
args: [bot]
---

Phân tích health bot `{bot}` theo skill `bot-history-analysis`:

1. `mcp__telegram-mtproto__list_bots()` để confirm bot tồn tại
2. `mcp__telegram-mtproto__count_bot_messages(bot="{bot}")` — dataset size
3. `mcp__telegram-mtproto__get_bot_history(bot="{bot}", limit=200)` — sample tin gần
4. Phân tích:
   - Classify OK vs FAIL keyword
   - Peak hour
   - Trend 7 ngày qua
5. `mcp__artifacts__publish_artifact(name="bot-check-{bot}", content=<html_report>, type="html")` để có URL share
6. Reply: URL + 3 dòng insight
