"""One-off recon: dump the My Alectra login page structure.

Run: .venv/bin/python scripts/recon_login.py [--headed]
No credentials are entered; this only inspects the public login form.
"""

import asyncio
import json
import sys

from playwright.async_api import async_playwright

LOGIN_URL = "https://myalectra.alectrautilities.com/portal/#/login"


async def main(headed: bool) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        page.set_default_timeout(45000)

        requests: list[dict] = []
        page.on("request", lambda r: requests.append({"url": r.url, "method": r.method}))

        print(f"goto {LOGIN_URL} ...", flush=True)
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)  # let the SPA settle

        info = await page.evaluate(
            """() => ({
                url: location.href,
                title: document.title,
                inputs: [...document.querySelectorAll('input')].map(el => ({
                    type: el.type, id: el.id, name: el.name,
                    placeholder: el.placeholder, ariaLabel: el.getAttribute('aria-label'),
                    classes: el.className, visible: !!(el.offsetWidth || el.offsetHeight),
                })),
                buttons: [...document.querySelectorAll('button, [role=button], input[type=submit]')].map(el => ({
                    tag: el.tagName, id: el.id, text: (el.textContent || el.value || '').trim().slice(0, 60),
                    classes: el.className, visible: !!(el.offsetWidth || el.offsetHeight),
                })),
                hasGrecaptcha: typeof window.grecaptcha !== 'undefined',
                localStorageKeys: Object.keys(localStorage),
                sessionStorageKeys: Object.keys(sessionStorage),
                forms: document.forms.length,
                bodySnippet: document.body ? document.body.innerText.slice(0, 1500) : null,
            })"""
        )
        print(json.dumps(info, indent=2, ensure_ascii=False))

        print("\n--- requests during load ---")
        for r in requests:
            print(r["method"], r["url"][:160])

        await browser.screenshot(path="captures/login_page.png") if False else None
        await page.screenshot(path="captures/login_page.png")
        print("\nscreenshot -> captures/login_page.png")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main("--headed" in sys.argv))
