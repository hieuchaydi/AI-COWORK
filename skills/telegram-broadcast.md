---
name: telegram-broadcast
description: Gửi cùng 1 message cho nhiều user Telegram đã từng DM bot
triggers: [broadcast, gửi hàng loạt, gửi tất cả user, mass message, blast]
---

# Skill: Telegram broadcast

Khi user muốn gửi cùng 1 tin cho nhiều người đã DM bot:

1. Nếu user không nói list cụ thể → `stats_recent_senders()` để cho user thấy sẽ gửi cho bao nhiêu user (transparency)
2. Nếu ≤ 5 users → gửi trực tiếp
3. Nếu > 5 users → xin xác nhận: "Sẽ gửi cho N users. OK?" trước khi bấm
4. Gọi `broadcast_to_recent_senders(text=...)` hoặc `broadcast_to_chat_ids([...], text=...)`
5. Report sent/failed count + list failed users (nếu ≤ 20 failed)

**Rate limit lưu ý**: Telegram giới hạn ~30 msg/sec cho bot. Bridge gửi tuần tự nên OK cho <100 users. Trên 100 → cảnh báo user + suggest gửi thành nhiều batch.

**Personalize**: nếu user muốn cá nhân hoá tin ("chào {name}"), lấy `list_recent_senders()` trước, format text với `first_name`, rồi loop `send_message` từng user.
