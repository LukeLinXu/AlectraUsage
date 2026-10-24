"""One-shot diagnostic: which in-page transport works for UsageAPI?

Login (auto-fill) -> settle -> compare fetch vs XHR on a known-good SPA path
and on UsageAPI. If XHR works, fetch all requested months right away.
"""

import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Probe targets: defaults are placeholders. Export ALECTRA_ACCOUNT /
# ALECTRA_METER (or edit here) to probe a real account.
import os as _os
ACCOUNT = _os.environ.get("ALECTRA_ACCOUNT", "0000000000")
METER = _os.environ.get("ALECTRA_METER", "DEMOMETER01")


from alectra_plans import analysis  # noqa: E402
from alectra_plans.cache import UsageCache  # noqa: E402
from alectra_plans.client import AlectraSession  # noqa: E402
from alectra_plans.models import UsageRecord  # noqa: E402

API = "https://alectra-svc.smartcmobile.link"

XHR_EVAL = """async ({method, url, body, token, timeoutMs}) => {
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
            xhr.timeout = timeoutMs;
            xhr.onload = () => finish({status: xhr.status, body: xhr.responseText});
            xhr.onerror = () => finish({status: 0, body: 'XHR_ERROR'});
            xhr.ontimeout = () => finish({status: 0, body: 'XHR_TIMEOUT'});
            xhr.send(body === undefined || body === null ? null : JSON.stringify(body));
        } catch (e) { finish({status: 0, body: 'XHR_EXC:' + e}); }
    });
}"""

FETCH_EVAL = """async ({method, url, body, token, timeoutMs}) => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        const resp = await fetch(url, {
            method,
            headers: {'accept':'application/json, text/plain, */*','authorization':'Bearer '+token,'pt':'1','uid':'1'},
            body: body === undefined || body === null ? undefined : JSON.stringify(body),
            credentials: 'include', signal: ctrl.signal,
        });
        return {status: resp.status, body: await resp.text()};
    } catch (e) {
        return {status: 0, body: 'FETCH_EXC:' + e};
    } finally { clearTimeout(timer); }
}"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def call(session, js, method, path, token, timeout_s=30, params=""):
    url = f"{API}{path}{params}"
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            session.page.evaluate(
                js,
                {"method": method, "url": url, "body": None, "token": token,
                 "timeoutMs": int(timeout_s * 1000)},
            ),
            timeout=timeout_s + 10,
        )
    except asyncio.TimeoutError:
        return {"status": 0, "body": "EVAL_NEVER_RESOLVED", "s": time.monotonic() - t0}
    result["s"] = time.monotonic() - t0
    return result


async def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
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
        log("login ok; settling 8s...")
        await page.wait_for_timeout(8000)

        token = session.access_token
        sanity = await page.evaluate("1 + 1")
        log(f"sanity: evaluate 1+1 = {sanity}")

        usage_calls = [u for u in session.captured_urls() if "UsageAPI" in u]
        log(f"SPA made {len(session.captured_urls())} API calls; UsageAPI among them: {len(usage_calls)}")
        for u in usage_calls[:5]:
            log(f"  SPA usage call: {u[:150]}")

        log("--- probe A: XHR on ConfigurationAPI (SPA-known path) ---")
        r = await call(session, XHR_EVAL, "GET", "/ConfigurationAPI/api/1/Configuration/SystemMaintainance", token, 20)
        log(f"    status={r['status']} in {r['s']:.1f}s body={r['body'][:80]!r}")

        params = (f"?AccountNumber={ACCOUNT}&MeterNumber={METER}"
                  "&From=2025-11-01&To=2025-11-30&Uom=kWh&Periodicity=HH")
        log("--- probe B: XHR on UsageAPI Electric (Nov 2025) ---")
        rb = await call(session, XHR_EVAL, "GET", "/UsageAPI/api/V1/Electric", token, 30, params)
        log(f"    status={rb['status']} in {rb['s']:.1f}s bytes={len(rb['body'])}")

        log("--- probe C: fetch on UsageAPI Electric (Nov 2025) ---")
        rc = await call(session, FETCH_EVAL, "GET", "/UsageAPI/api/V1/Electric", token, 30, params)
        log(f"    status={rc['status']} in {rc['s']:.1f}s bytes={len(rc['body'])}")

        winner = None
        if rb["status"] == 200:
            winner, js = "xhr", XHR_EVAL
        elif rc["status"] == 200:
            winner, js = "fetch", FETCH_EVAL
        if not winner:
            log("BOTH transports failed. Captured portal URLs this session:")
            for u in session.captured_urls()[:30]:
                log(f"    {u[:150]}")
            return 5
        log(f"--- winner: {winner}; fetching {months} months via {winner} ---")

        cache = UsageCache()
        all_entries = []
        today = date.today().replace(day=1)
        y, m = today.year, today.month
        month_list = []
        for _ in range(months):
            month_list.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        month_list.reverse()

        import calendar as _cal
        for (yy, mm) in month_list:
            key = f"{yy:04d}-{mm:02d}"
            if (cached := cache.load(ACCOUNT, METER, key)):
                all_entries.extend(cached)
                log(f"  {key}: cache ({len(cached)} records)")
                continue
            first = date(yy, mm, 1)
            last = date(yy, mm, _cal.monthrange(yy, mm)[1])
            q = (f"?AccountNumber={ACCOUNT}&MeterNumber={METER}"
                 f"&From={first.isoformat()}&To={last.isoformat()}&Uom=kWh&Periodicity=HH")
            r = await call(session, js, "GET", "/UsageAPI/api/V1/Electric", token, 60, q)
            if r["status"] != 200:
                log(f"  {key}: FAILED status={r['status']} body={r['body'][:120]!r}")
                continue
            try:
                entries = json.loads(r["body"])["Result"]["electricUsages"]
            except Exception as exc:
                log(f"  {key}: parse error {exc}")
                continue
            cache.store(ACCOUNT, METER, key, entries)
            all_entries.extend(entries)
            log(f"  {key}: {len(entries)} records in {r['s']:.1f}s")

        if not all_entries:
            log("no usage data fetched")
            return 6
        records = [UsageRecord.from_api(e) for e in all_entries]
        result = analysis.analyze(records)
        out = REPO / "data"
        out.mkdir(exist_ok=True)
        (out / f"analysis_{ACCOUNT}_{METER}.json").write_text(json.dumps(result, indent=1))
        t = result["totals"]
        log(f"DONE: {len(all_entries)} records; best plan: {t['best']}")
        for mrow in result["months"]:
            p = mrow["plans"]
            log(f"  {mrow['key']}: {mrow['kwh']:7.1f} kWh  tiered {p['tiered'] or 0:7.2f}  "
                f"tou {p['tou'] or 0:7.2f}  ulo {(p['ulo'] if p['ulo'] is not None else float('nan')):7.2f}  best={mrow['best']}")
        log(f"  TOTAL: tiered {t['tiered']:.2f}  tou {t['tou']:.2f}  ulo {t['ulo']:.2f}  best={t['best']}")
        return 0


raise SystemExit(asyncio.run(main()))
