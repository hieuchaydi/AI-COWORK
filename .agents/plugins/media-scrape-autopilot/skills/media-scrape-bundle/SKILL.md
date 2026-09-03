---
name: media-scrape-bundle
description: Scrapes or crawls data that may include images/videos, saves an Excel-safe CSV, downloads media into a folder, zips it, and reports direct links without waiting for plan approval.
---

# Media Scrape Bundle

Use this skill whenever the user asks to crawl, scrape, collect, export, or analyze data that may include images, videos, galleries, product review media, catalog media, article media, attachments, or other downloadable files.

## Execution Mode

- Execute automatically from A to Z in the same turn.
- If the user invokes `/plan`, create or update `implementation_plan.md` only as an informational checklist with `RequestFeedback: false`, then continue immediately.
- Do not stop at "Submit plan", "Review plan", "Approve", or "Proceed".
- Ask the user only for irreversible data-loss operations or missing credentials/login sessions.

## Workflow

1. Crawl or extract the structured rows needed for the user request.
2. Keep source media URLs in the rows when useful. Look for fields such as `image`, `images`, `photo`, `photos`, `thumbnail`, `video`, `videos`, `media`, `media_urls`, `attachments`, `gallery`, `src`, and `url`.
3. Normalize the job name to a short ASCII slug, for example `shopee_123`, `catalog_20260903`, or `reviews_<itemid>`.
4. Prefer this one-shot tool when available:

   ```text
   crawl_and_export_bundle(
     rows=<list_of_dicts>,
     media_urls=<optional_list_of_urls>,
     job_name="<job_name>",
     csv_filename="<job_name>.csv",
     zip_filename="<job_name>_media.zip"
   )
   ```

5. If `crawl_and_export_bundle` is unavailable, use this fallback sequence:

   ```text
   save_csv(rows=<rows>, filename="<job_name>.csv")
   download_media_and_zip(
     urls=<deduped_media_urls>,
     folder_name="<job_name>",
     zip_filename="<job_name>_media.zip"
   )
   ```

6. If media files are already saved on disk, use:

   ```text
   zip_folder(folder_path="outputs/media/<job_name>", zip_filename="<job_name>_media.zip")
   ```

## Media URL Handling

- Deduplicate URLs while preserving order.
- Include images and videos by default when the task asks for "media", "anh", "hinh anh", "video", "gallery", "review co anh", or similar wording.
- Do not download buyer avatars or profile pictures unless the user specifically asks for them.
- Respect max file limits if a tool enforces them; report partial failures clearly.

## Blocked Sites

- Shopee must use the local ingest queue flow if available. Do not use controlled browser retries against Shopee traffic verification.
- For other login-gated sites, open the site once, let the user log in if necessary, then call same-origin APIs from the logged-in page where possible.

## Final Answer

When media exists, always include both links:

```markdown
[Tai file CSV](http://localhost:8766/outputs/csv/<job_name>.csv)
[Tai tron bo anh/video .ZIP](http://localhost:8766/outputs/zips/<job_name>_media.zip)
```

Also include row count, downloaded media count, failed media count if available, and the local media folder path.
