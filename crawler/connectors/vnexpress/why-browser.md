# VnExpress Fetch Strategy

VnExpress articles are server-side rendered (SSR), meaning the complete content of the article is present in the initial HTML response. 
There is no need for a headless browser (like Playwright/Puppeteer) to execute JavaScript to fetch the article content.

Using Rung 1 (plain HTTP requests) is sufficient, faster, and consumes significantly less resources compared to using a browser.

**Fetch Rung**: HTTP
**Reasoning**: Server-rendered HTML, no JavaScript execution required for core content (title, lead, body, published date, author).
