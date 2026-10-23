"""Diag #4: see the 401 bodies (unfiltered), test a fresh in-page reCAPTCHA
token, and diff our request headers against the SPA's own authed requests."""

import asyncio
import base64
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alectra_plans.client import AlectraSession  # noqa: E402

API = "https://alectra-svc.smartcmobile.link"
SITE_KEY = "6Lf4Hg0kAAAAAK7Aw4DL1V043_aDymt-hfupBcU4"

XHR_EVAL = """async ({method, url, body, token, timeoutMs, extraHeaders}) => {
    return await new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        let done = false;
        const finish = (r) => { if (!done) { done = true; clearTimeout(timer); resolve(r); } };
        const timer = setTimeout(() => { xhr.abort(); finish({status: 0, body: 'CLIENT_ABORT'}); }, timeoutMs);
        try {
            xhr.open(method, url, true);
            xhr.setRequestHeader('accept', 'application/json, text/plain, */*');
            if (token) xhr.setRequestHeader('authorization', 'Bearer ' + token);
            xhr.setRequestHeader('pt', '1');
            xhr.setRequestHeader('uid', '1');
            for (const [k, v] of Object.entries(extraHeaders || {})) {
                try { xhr.setRequestHeader(k, v); } catch (e) {}
            }
            xhr.timeout = timeoutMs;
            xhr.onload = () => finish({status: xhr.status, body: xhr.responseText,
                                       respHeaders: xhr.getAllResponseHeaders()});
            xhr.onerror = () => finish({status: 0, body: 'XHR_ERROR',
                                        respHeaders: xhr.getAllResponseHeaders()});
            xhr.ontimeout = () => finish({status: 0, body: 'XHR_TIMEOUT'});
            xhr.send(body === undefined || body === null ? null : JSON.stringify(body));
        } catch (e) { finish({status: 0, body: 'XHR_EXC:' + e}); }
    });
}"""

CAPTCHA_EVAL = """async (siteKey) => {
    if (typeof grecaptcha === 'undefined') return null;
    return await new Promise((resolve) => {
        grecaptcha.ready(async () => {
            try { resolve(await grecaptcha.execute(siteKey, {action: 'submit'})); }
            catch (e) { resolve('CAPTCHA_ERR:' + e); }
        });
    });
}"""

PARAMS = ("?AccountNumber=0000000000&MeterNumber=DEMOMETER01"
          "&From=2025-11-01&To=2025-11-30&Uom=kWh&Periodicity=HH")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mask(text):
    return re.sub(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}?", "<JWT>", str(text))


async def probe(session, label, path, token, extra=None, params="", timeout_s=25):
    r = await asyncio.wait_for(
        session.page.evaluate(
            XHR_EVAL,
            {"method": "GET", "url": f"{API}{path}{params}", "body": None,
             "token": token, "timeoutMs": timeout_s * 1000, "extraHeaders": extra or {}},
        ),
        timeout=timeout_s + 10,
    )
    log(f"{label}: HTTP {r['status']}")
    for ln in (r.get("respHeaders") or "").splitlines():
        if any(k in ln.lower() for k in ("www-auth", "content-type", "cf-", "server:", "allow")):
            log(f"    resp-hdr {ln}")
    log(f"    body: {mask(r['body'][:500])!r}")
    return r


async def main():
    # collect the SPA's own request headers for authed calls
    spa_requests = []

    async with AlectraSession(headless=False) as session:
        page = session.page

        async def grab_headers(response):
            try:
                if "smartcmobile.link" in response.url and "/Configuration" not in response.url:
                    h = await response.request.all_headers()
                    spa_requests.append({"url": response.url, "status": response.status, "headers": h})
            except Exception:
                pass

        page.on("response", grab_headers)
        await page.goto("https://myalectra.alectrautilities.com/portal/#/login",
                        wait_until="domcontentloaded")
        await session.attach_recorder()
        creds = json.loads((Path.home() / ".alectra-credentials").read_text())
        await asyncio.wait_for(session.login(creds["username"], creds["password"]), timeout=90)
        token = session.access_token
        log("login ok; settling 8s...")
        await page.wait_for_timeout(8000)

        log("--- SPA's own non-configuration API calls (status + headers) ---")
        for req in spa_requests:
            h = req["headers"]
            keys = {k: (v[:24] + "..." if k in ("authorization", "recaptchatoken") else v)
                    for k, v in h.items()
                    if k not in ("user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
                                 "accept-language", "referer")}
            log(f"  [{req['status']}] {req['url'][:120]}")
            for k, v in keys.items():
                log(f"      {k}: {mask(v)}")

        log("--- probe 1: Addresses/User (no-credential XHR) ---")
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        uuid_claim = json.loads(base64.urlsafe_b64decode(payload))["Uuid"]
        await probe(session, "addresses", f"/AccountManagementAPI/api/1/Addresses/User/{uuid_claim}", token)

        log("--- probe 2: UsageAPI plain ---")
        await probe(session, "usage-plain", "/UsageAPI/api/V1/Electric", token, params=PARAMS)

        log("--- probe 3: fresh reCAPTCHA v3 token from this page ---")
        captcha = await page.evaluate(CAPTCHA_EVAL, SITE_KEY)
        log(f"    captcha token: {('got ' + str(len(captcha)) + ' chars') if captcha else 'NONE'}")
        if captcha and not str(captcha).startswith("CAPTCHA_ERR"):
            await probe(session, "usage-captcha", "/UsageAPI/api/V1/Electric", token,
                        extra={"RecaptchaToken": captcha}, params=PARAMS)

    log("done")


raise SystemExit(asyncio.run(main()))
