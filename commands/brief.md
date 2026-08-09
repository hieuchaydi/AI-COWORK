---
name: brief
description: Sinh brief hôm nay (email + telegram bot status + calendar)
args: []
---

Sinh brief hôm nay theo skill `daily-summary`:

1. `todo_write` các bước
2. `mcp__skills__load_skill(name="daily-summary")` để lấy instruction đầy đủ
3. Thực hiện theo skill body
4. `write_file` `brief-{YYYY-MM-DD}.md`
5. Tùy chọn: publish qua artifacts_mcp để có URL share
6. Reply user link + 3-line summary
