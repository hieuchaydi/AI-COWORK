---
name: daily-summary
description: Sinh brief hôm nay tổng hợp mail + telegram + calendar
triggers: [brief hôm nay, tóm tắt ngày, morning report, đầu ngày, today summary]
---

# Skill: Daily summary brief

Sinh 1 brief ngắn (< 20 dòng) tổng hợp trạng thái đầu ngày của user:

1. `todo_write` bước
2. Gmail: đọc top 10 unread hôm nay → lọc theo importance (label, sender), summarize theo 3 dòng
3. Telegram (tuỳ user opt):
   - `stats_recent_senders()` — bao nhiêu user mới DM bot
   - `get_bot_history(bot=@X, limit=20)` cho bot quan trọng — tin fail?
4. Google Calendar: events hôm nay
5. `write_file` `brief-YYYY-MM-DD.md`:
   ```
   # Brief HH/MM/YYYY
   ## Email quan trọng (N unread)
   - ...
   ## Telegram
   - Bot cookie: X fail / Y OK trong 24h
   - N user mới DM
   ## Lịch hôm nay
   - HH:MM — event
   ```
6. Optional: `send_message(target="telegram:6973629128", text=summary)` để user nhận qua Telegram

**Không**: dán full inbox. Chỉ TÓM TẮT — user muốn thấy gì cần action ngay.
