"""Diag #3: validate token on authed endpoints; dump UsageAPI 401 details;
diff our XHR headers against the SPA's own authenticated request headers."""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alectra_plans.client import AlectraSession  # noqa: E402

API = "https://alectra-svc.smartcmobile.link"

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
            for (const [k, v] of Object.entries(extraHeaders || {})) xhr.setRequestHeader(k, v);
            xhr.timeout = timeoutMs;
            xhr.onload = () => finish({
                status: xhr.status, body: xhr.responseText,
                respHeaders: xhr.getAllResponseHeaders(),
            });
            xhr.onerror = () => finish({status: 0, body: 'XHR_ERROR'});
            xhr.ontimeout = () => finish({status: 0, body: 'XHR_TIMEOUT'});
            xhr.send(body === undefined || body === null ? null : JSON.stringify(body));
        } catch (e) { finish({status: 0, body: 'XHR_EXC:' + e}); }
    });
}"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def mask(text, repl="<JWT>"):
    return re.sub(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}?", repl, text)


async def probe(session, label, path, token, extra=None, params="", timeout_s=25):
    r = await asyncio.wait_for(
        session.page.evaluate(
            XHR_EVAL,
            {"method": "GET", "url": f"{API}{path}{params}", "body": None,
             "token": token, "timeoutMs": timeout_s * 1000, "extraHeaders": extra or {}},
        ),
        timeout=timeout_s + 10,
    )
    log(f"{label}: status={r['status']} in {r.get('s', 0):.1f}s")
    if r.get("respHeaders"):
        interesting = [ln for ln in r["respHeaders"].splitlines()
                       if any(k in ln.lower() for k in ("www-auth", "content-type", "cf-", "server"))]
        for ln in interesting:
            log(f"    resp-hdr: {ln}")
    log(f"    body: {mask(r['body'][:400])!r}")
    return r


async def main():
    async with AlectraSession(headless=False) as session:
        page = session.page
        await page.goto(
            "https://myalectra.alectrautilities.com/portal/#/login",
            wait_until="domcontentloaded",
        )
        await session.attach_recorder()
        creds = json.loads((Path.home() / ".alectra-credentials").read_text())
        log("auto-filling login...")
        await asyncio.wait_for(session.login(creds["username"], creds["password"]), timeout=90)
        token = session.access_token
        log("login ok; settling 8s...")
        await page.wait_for_timeout(8000)

        # What did the SPA call, and with which headers?
        log(f"SPA made {len(session.captured_urls())} calls:")
        for u in session.captured_urls():
            log(f"    {u[:140]}")

        # dump headers of the SPA's own authenticated requests
        seen_hdr = False
        for cap in session._captured:
            if "ConfigurationAPI" in cap["url"]:
                continue
            try:
                headers = await cap.get("response").all_headers() if isinstance(cap.get("response"), object) else None
            except Exception:
                headers = None
        # simpler: re-request headers from the recorder entries via request objects is
        # awkward; instead probe the known authed endpoint directly:
        uuid_claim = session.page and None
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        uuid_claim = claims["Uuid"]
        log(f"JWT uuid={uuid_claim} exp={claims['exp']}")

        log("--- probe 1: Addresses/User/{uuid} (authed, SPA-used path) ---")
        await probe(session, "addresses", f"/AccountManagementAPI/api/1/Addresses/User/{uuid_claim}", token)

        log("--- probe 2: UsageAPI Electric, plain (expect 401) ---")
        params = ("?AccountNumber=0000000000&MeterNumber=DEMOMETER01"
                  "&From=2025-11-01&To=2025-11-30&Uom=kWh&Periodicity=HH")
        await probe(session, "usage-plain", "/UsageAPI/api/V1/Electric", token, params=params)

        log("--- probe 3: UsageAPI + st: PL header ---")
        await probe(session, "usage-st", "/UsageAPI/api/V1/Electric", token,
                    extra={"st": "PL"}, params=params)

        log("--- probe 4: UsageAPI + origin header (explicit) ---")
        await probe(session, "usage-origin", "/UsageAPI/api/V1/Electric", token,
                    extra={"origin": "https://myalectra.alectrautilities.com"}, params=params)

        log("--- probe 5: UsageAPI + Accept-Language/UA-ish extras ---")
        await probe(session, "usage-full", "/UsageAPI/api/V1/Electric", token,
                    extra={
                        "accept-language": "en-US,en;q=0.9",
                        "sec-fetch-dest": "empty",
                        "sec-fetch-mode": "cors",
                        "sec-fetch-site": "cross-site",
                    }, params=params)

        log("done; page still open:", page.url)


raise SystemExit(asyncio.run(main()))
