---
name: broadcast
description: Gửi message text cho mọi user đã DM bot gần đây
args: [text]
---

Broadcast qua Telegram bot:

1. `mcp__telegram-bot__stats_recent_senders()` — xem N users
2. Nếu N > 10 → hỏi user xác nhận "Sẽ gửi cho N users, OK?" (chỉ 1 lần này)
3. Nếu OK hoặc N ≤ 10 → `mcp__telegram-bot__broadcast_to_recent_senders(text="{text}")`
4. Report: sent/failed count + list failed nếu ≤ 20
