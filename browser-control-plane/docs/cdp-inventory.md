# CDP Inventory

Map the existing Playwright calls in browser.py to BCP methods.

| browser.py call | Playwright API used | BCP method |
|---|---|---|
| browser_open | page.goto() | page.navigate |
| browser_read | page.content(), page.evaluate() | page.content, runtime.evaluate |
| browser_read_selector | page.locator().inner_text() | dom.query + dom.text |
| browser_click | page.locator().click(), page.get_by_text().click(), page.get_by_role().click() | input.click |
| browser_fill | page.locator().fill() | dom.query + input.type |
| browser_press | page.keyboard.press() | input.press |
| browser_screenshot | page.screenshot() | page.screenshot |
| browser_wait_for | page.locator().wait_for() | dom.waitForSelector |
| browser_evaluate | page.evaluate() | runtime.evaluate |
| browser_get_links | page.evaluate() | runtime.evaluate |
| browser_get_forms | page.evaluate() | runtime.evaluate |
| browser_current_url | page.url, page.title() | runtime.evaluate |
| browser_close | context.close(), playwright.stop() | agent.shutdown |

Also note:
- launch_persistent_context → target.create (with profile support)
- Viewport setting in launch → page.setViewport
- --disable-blink-features=AutomationControlled → out of scope (anti-detection, BCP §1.3)
