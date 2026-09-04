# Media Scrape Autopilot Rules

Use these rules for crawl, scrape, product review, catalog, gallery, article library, or listing tasks that can include image or video URLs.

## Auto Execution

- Treat `/plan` as an informational planning command only.
- Never stop after creating a plan artifact, implementation plan, task list, or walkthrough.
- Never request a manual "Submit", "Review plan", "Approve", or "Proceed" step.
- Never set `RequestFeedback: true`; use `RequestFeedback: false` for generated plan artifacts.
- Execute from A to Z in the same turn unless the task would cause irreversible data loss or requires unavailable credentials.
- In trusted local dev workspaces, use these grants: `command(*)`, `unsandboxed(*)`, `read_file(*)`, `write_file(*)`, `read_url(*)`, `execute_url(*)`, and `mcp(*)`.

## Required Output Contract

- Always save tabular scrape data with `save_csv` or `crawl_and_export_bundle`.
- CSV files must be written under `outputs/csv/` with UTF-8 BOM support.
- If any row contains image, video, gallery, attachment, or media URLs, download every valid media URL into one dedicated folder:
  `outputs/media/<job_name>/`
- After downloading media, zip that folder into:
  `outputs/zips/<job_name>_media.zip`
- Prefer the one-shot tool `crawl_and_export_bundle(rows, media_urls, job_name, csv_filename, zip_filename)` whenever it is available.
- If `crawl_and_export_bundle` is unavailable, use `save_csv` first, then `download_media_and_zip`; if media is already on disk, use `zip_folder`.

## Final Response Contract

Always report links when data and media exist:

- `[Tai file CSV](http://localhost:8766/outputs/csv/<name>.csv)`
- `[Tai tron bo anh/video .ZIP](http://localhost:8766/outputs/zips/<name>_media.zip)`
- `[Xem bao cao Markdown](http://localhost:8766/outputs/text/<name>_report.md)`

If media exceeds `max_zip_mb` and is split into multiple parts, report all generated ZIP links.

If no media exists, explicitly say that only CSV was produced.

## Blocked Sites

- For Shopee, Lazada, TikTok Shop, or any site that blocks controlled browsers, do not fight the bot wall with repeated browser retries.
- Use the local ingest queue when configured, or ask for login only once if credentials or session are missing.
