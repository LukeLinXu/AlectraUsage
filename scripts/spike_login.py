"""Smoke test: log in to My Alectra with YOUR OWN credentials and probe the API.

This verifies the automation end to end:
  1. portal login (incl. optional TFA),
  2. account/meter discovery,
  3. one month of hourly usage.

Usage:
  .venv/bin/python scripts/spike_login.py <username> <password> [--headed] [--month 2025-06]

Credentials are only sent to Alectra. A redacted request log is written to
captures/spike_requests.json (tokens masked).
"""

import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alectra_plans.client import (  # noqa: E402
    AlectraSession,
    LoginFailed,
    TfaRequired,
    _mask,
)
from alectra_plans.models import UsageRecord  # noqa: E402
from alectra_plans.rates import compute_months  # noqa: E402

CAPTURES = Path(__file__).resolve().parent.parent / "captures"


async def prompt(text: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, input, text)


async def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    month_arg = "2025-06"
    if "--month" in sys.argv:
        month_arg = sys.argv[sys.argv.index("--month") + 1]
    if len(args) < 2:
        print(__doc__)
        return 2
    username, password = args[0], args[1]
    headed = "--headed" in flags

    CAPTURES.mkdir(exist_ok=True)
    api_log: list[dict] = []

    async with AlectraSession(headless=not headed) as session:
        page = session.page
        page.on(
            "response",
            lambda r: api_log.append(
                {"method": r.request.method, "url": r.url, "status": r.status}
            )
            if "smartcmobile.link" in r.url
            else None,
        )

        print(f"[1/4] Logging in as {username} ({'headed' if headed else 'headless'})...")
        try:
            await session.login(username, password)
        except TfaRequired:
            print("    TFA required - the portal just sent you a code.")
            code = await prompt("    Enter the code here (or leave empty to use the opened browser): ")
            if headed and not code:
                print("    Complete the TFA prompt in the browser window; waiting...")
                code = "SKIP"
            if code and code != "SKIP":
                await session.submit_tfa(code)
        except LoginFailed as exc:
            print(f"    LOGIN FAILED: {exc}")
            return 1

        if not session.access_token:
            print("    LOGIN FAILED: no access token captured.")
            return 1

        exp = session.token_expiry.strftime("%Y-%m-%d %H:%M UTC") if session.token_expiry else "?"
        print(f"    OK, access token captured (expires {exp}).")

        print("[2/4] Discovering accounts and meters...")
        meters = await session.list_meters()
        if meters:
            for m in meters:
                print(f"    account {m.account_number}  meter {m.meter_number}  {m.label}")
        else:
            print("    No meters parsed from discovery responses!")
            print("    -> The raw response shapes are in captures/spike_requests.json;")
            print("       also navigate to the Usage page in the browser if --headed.")

        if meters:
            m0 = meters[0]
            year, mon = (int(x) for x in month_arg.split("-"))
            print(f"[3/4] Fetching {month_arg} hourly usage for meter {m0.meter_number}...")
            entries = await session.fetch_month(m0.account_number, m0.meter_number, year, mon)
            print(f"    {len(entries)} hourly records returned.")
            if entries:
                records = [UsageRecord.from_api(e) for e in entries]
                months = compute_months(records)
                for mm in months:
                    print(
                        f"    {mm.key}: {mm.kwh} kWh | tiered ${mm.tiered_cost} "
                        f"tou ${mm.tou_cost} ulo ${mm.ulo_cost} "
                        f"(rates effective {mm.period_effective})"
                    )
                out = CAPTURES / f"spike_usage_{month_arg}.json"
                out.write_text(json.dumps(entries, indent=1))
                print(f"    raw data -> {out}")

        print("[4/4] Waiting 5s for any remaining dashboard calls...")
        await asyncio.sleep(5)

    with open(CAPTURES / "spike_requests.json", "w") as fh:
        json.dump(api_log, fh, indent=1)
    print(f"\n{len(api_log)} API calls logged -> captures/spike_requests.json")
    print("Login automation is working end to end." if session.access_token else "FAILED")
    return 0 if session.access_token else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
