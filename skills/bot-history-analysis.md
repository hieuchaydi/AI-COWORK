---
name: bot-history-analysis
description: Phân tích thống kê lịch sử tin nhắn của 1 bot Telegram
triggers: [phân tích bot, thống kê bot, stats bot, analyze bot, health bot]
---

# Skill: Bot history analysis

Khi user muốn hiểu health/pattern của 1 bot (VD cookie bot, notification bot):

1. `list_bots()` để confirm tên bot user nói đến (fuzzy match)
2. `count_bot_messages(bot=@X)` để biết dataset size
3. Nếu > 500 → dùng `get_bot_history(bot, limit=500)` + note "còn N msg cũ hơn"
4. Phân tích:
   - Classify theo keyword: OK/FAIL, cycle vs alert, hourly/daily count
   - Tần suất failure — nếu tăng gấp 2 trong tuần cuối → cảnh báo user
   - Peak hour → suggest optimize cron
5. `write_file` report `bot-{name}-analysis.md` với:
   - Summary counts
   - Chart bằng ASCII bar
   - Top 3 pattern nổi bật
   - Recommendation
6. Reply user với link file + 3-line summary

**Pattern nhận biết**:
- "Chu kỳ vừa xong" / "OK" / "success" → healthy signal
- "BAD" / "expired" / "invalid" / "error" → fail signal
- Messages có số (VD "196/500") → progress metric
