---
name: shopee-reviews
description: Cào toàn bộ đánh giá một sản phẩm Shopee ra CSV, qua browser đã đăng nhập
triggers: [cào đánh giá shopee, review shopee, đánh giá sản phẩm shopee, shopee reviews, lấy comment shopee]
---

# Skill: Cào đánh giá Shopee

**Không dùng `browser_open`.** Đo 2026-08-08: Shopee chặn browser điều khiển bằng CDP ngay
request đầu tiên, cả Chromium bundled lẫn Chrome thật, cả khi chưa cần đăng nhập. Dùng job queue
— extension trong Chrome thường của user chạy bằng cookie thật:

1. `web_fetch("http://127.0.0.1:8766/ingest/job?url=<link>")` → lấy `job.id`
2. `web_fetch("http://127.0.0.1:8766/ingest/result?id=<job id>")`, `ok:false` thì chờ vài giây
   hỏi lại (tối đa ~10 lần)
3. `ok:true` → báo `result.count` + `result.csv`. `result.ok:false` → báo nguyên văn
   `result.error`
4. Muốn phân tích thêm: `web_fetch` file JSON ở `result.url`, rồi tóm tắt khen/chê, phân bố sao

Quá 1 phút chưa xong = extension chưa cài hoặc Chrome đang đóng → bảo user mở
<http://127.0.0.1:8766/ingest> làm theo "Cách 1". **Đừng** quay lại `browser_open`.

---

Phần dưới chỉ dùng cho site **không** chặn CDP: gọi API của chính site từ trong tab đã đăng nhập.

1. `todo_write` 4 bước.

2. `browser_open(url=<link sản phẩm>, wait_until="networkidle")`.

3. **Kiểm tra tường login trước khi làm gì khác** — `browser_current_url()`:
   - Chứa `/verify/traffic` hoặc `is_logged_in=false` → **DỪNG LẠI**. Nhắn user:
     *"Shopee đang bắt đăng nhập. Bấm Log In ngay trong cửa sổ Chromium đang mở, đăng nhập xong
     nhắn tôi một tiếng."* Cookie lưu ở `~/.coworker-browser` nên chỉ phải làm một lần.
   - Tuyệt đối **không** tự viết script Python thay thế — xem mục Không ở dưới.

4. Lấy `shopid` + `itemid`, rồi phân trang API trong **một** lần `browser_evaluate`:

   ```js
   async () => {
     let m = location.href.match(/i\.(\d+)\.(\d+)/);
     let shopid, itemid;
     if (m) { shopid = m[1]; itemid = m[2]; }
     else {
       const h = document.documentElement.innerHTML;
       shopid = (h.match(/"shopid":\s*(\d+)/) || [])[1];
       itemid = (h.match(/"itemid":\s*(\d+)/) || [])[1];
     }
     if (!shopid || !itemid) return { error: "no shopid/itemid — xin link dạng ...-i.<shopid>.<itemid>" };

     const out = [];
     for (let offset = 0; offset < 2000; offset += 50) {
       const r = await fetch(
         `/api/v2/item/get_ratings?itemid=${itemid}&shopid=${shopid}&type=0&filter=0&limit=50&offset=${offset}`,
         { headers: { "x-requested-with": "XMLHttpRequest" } }
       );
       if (!r.ok) return { error: `HTTP ${r.status} tại offset ${offset}`, got: out.length, rows: out };
       const j = await r.json();
       const batch = (j.data && j.data.ratings) || [];
       for (const x of batch) {
         out.push({
           user: x.author_username || "",
           sao: x.rating_star,
           noi_dung: (x.comment || "").replace(/\s+/g, " ").trim(),
           thoi_gian: new Date((x.ctime || 0) * 1000).toISOString().slice(0, 19).replace("T", " "),
           phan_loai: (x.product_items || []).map(p => p.model_name).join("|"),
         });
       }
       if (batch.length < 50) break;
       await new Promise(s => setTimeout(s, 700));   // đi chậm, đừng để bị flag
     }
     return { total: out.length, rows: out };
   }
   ```

   Trả về `{error: ...}` → báo user kèm nguyên văn, đừng đoán mò rồi thử cách khác.

5. `save_csv(filename="shopee_reviews_<itemid>.csv", rows=<rows>)` — BOM UTF-8 mặc định đã bật,
   Excel đọc tiếng Việt không lỗi font.

6. Trả lời: tổng số đánh giá, phân bố sao, 2-3 ý lặp lại nhiều nhất, kèm link
   `[Tải CSV](artifact:outputs/csv/<tên file>)`.

## Nếu Shopee chặn cả browser tool

Dấu hiệu: dính `verify/traffic` **dù đã đăng nhập**, hoặc API trả 200 mà `data: null`. Lúc đó
Playwright/CDP bị nhận diện — đổi luồng, đừng cố retry:

1. Nói user mở <http://127.0.0.1:8766/ingest>, kéo bookmarklet lên thanh bookmark (làm 1 lần).
2. User mở trang sản phẩm trong **Chrome thường** đã đăng nhập, bấm bookmarklet.
3. **Helper tự ghi CSV** — `outputs/csv/shopee_<itemid>.csv` + bản thô
   `outputs/inbox/shopee_<itemid>.json`. Bạn KHÔNG cần `save_csv` nữa, việc đã xong.
4. Muốn phân tích thêm thì đọc file JSON bằng `web_fetch`
   (`http://127.0.0.1:8766/outputs/inbox/shopee_<itemid>.json`) — **không** dùng `read_file`,
   `outputs/` nằm ngoài workspace của session nên không đọc trực tiếp được.

Luồng này không có automation nào nên không thể bị fingerprint — Chrome thường chạy JS của
chính trang, dùng cookie của chính user.

## Không

- **Không viết script Python `requests`/`httpx`.** Ngoài browser không có cookie → Shopee trả
  tường login hoặc HTML rỗng. Đã thử và fail (2026-08-08).
- **Không cào DOM bằng cuộn trang.** Đánh giá load lazy, cuộn kiểu gì cũng sót và chậm gấp bội.
- **Không hạ `limit` xuống 20 rồi gọi hàng trăm lần.** Càng nhiều request càng dễ ăn
  `verify/traffic`. 50/lần + nghỉ 700ms là đủ.
- Bị `verify/traffic` **dù đã đăng nhập** = bị flag. Nghỉ vài phút, đừng retry liên tục.
