"""Fetch My Alectra hourly usage and compare Ontario RPP plans — skill entry point.

You log in MANUALLY in the opened browser window (password never touches
code or disk); the script picks the token up from the portal page, mines
the portal's own traffic for accounts/meters, fetches hourly usage month
by month (hard per-month timeout), and writes a plan-comparison report.

Usage:
  .venv/bin/python scripts/skill_fetch.py [--months 12] [--tesla-kwh 250]
          [--account N] [--meter N] [--login-timeout 300] [--username NAME]

Login modes:
  * default: you log in manually in the opened browser window.
  * --username NAME: the script fills the login form automatically (the page
    still solves reCAPTCHA/signature itself). The password comes from the
    ALECTRA_PASSWORD env var or ~/.alectra-credentials (JSON:
    {"username": "...", "password": "..."}, chmod 600). It never appears in
    argv, logs, or chat. If the account has 2FA, finish it in the window.

Outputs:
  ~/.cache/alectra-plan-compare/<account>_<meter>/<YYYY-MM>.json  (raw cache)
  <repo>/data/analysis_<account>_<meter>.json / .csv              (report)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alectra_plans import analysis  # noqa: E402
from alectra_plans.cache import UsageCache  # noqa: E402
from alectra_plans.client import AlectraError, AlectraSession  # noqa: E402
from alectra_plans.models import UsageRecord  # noqa: E402

OUT_DIR = REPO / "data"
PLAN_NAMES = {"tiered": "Tiered", "tou": "TOU", "ulo": "ULO"}


def month_start(months_back: int) -> date:
    today = date.today().replace(day=1)
    y, m = today.year, today.month
    for _ in range(months_back - 1):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return date(y, m, 1)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_password(username: str) -> str | None:
    """Password from $ALECTRA_PASSWORD or ~/.alectra-credentials (never argv)."""
    import os

    if os.environ.get("ALECTRA_PASSWORD"):
        return os.environ["ALECTRA_PASSWORD"]
    cred_file = Path.home() / ".alectra-credentials"
    if not cred_file.exists():
        return None
    mode = oct(cred_file.stat().st_mode & 0o777)
    if mode != "0o600":
        log(f"WARNING: {cred_file} is mode {mode}; chmod 600 it.")
    try:
        data = json.loads(cred_file.read_text())
    except json.JSONDecodeError:
        log(f"ERROR: {cred_file} is not valid JSON.")
        return None
    if isinstance(data, dict) and data.get("password"):
        if data.get("username") in (None, "", username):
            return data["password"]
        log(f"ERROR: {cred_file} is for user '{data.get('username')}', not '{username}'.")
        return None
    log(f"ERROR: {cred_file} must be {{\"username\": ..., \"password\": ...}}")
    return None


async def run(args: argparse.Namespace) -> int:
    async with AlectraSession(headless=False) as session:
        page = session.page
        await page.goto(
            "https://myalectra.alectrautilities.com/portal/#/login",
            wait_until="domcontentloaded",
        )
        await session.attach_recorder()

        if args.username:
            password = load_password(args.username)
            if not password:
                log("No password available (set ALECTRA_PASSWORD or create ~/.alectra-credentials).")
                return 2
            log(f"Auto-filling the login form for {args.username[:3]}*** in the browser...")
            try:
                await asyncio.wait_for(
                    session.login(args.username, password), timeout=90
                )
            except AlectraError as exc:
                if "one-time verification code" in str(exc) or "TFA" in str(exc).upper():
                    log("2FA required — enter the code in the browser window; waiting...")
                    if not await session.wait_for_manual_login(timeout_s=180):
                        log("TIMED OUT waiting for the 2FA code.")
                        return 2
                else:
                    log(f"LOGIN FAILED: {exc}")
                    return 1
        else:
            log("Browser window is open -> please log in to My Alectra in it.")
            log(f"Waiting up to {args.login_timeout:.0f}s for the login to complete...")
            if not await session.wait_for_manual_login(timeout_s=args.login_timeout):
                log("TIMED OUT waiting for login. Nothing was fetched.")
                return 2

        exp = session.token_expiry.strftime("%Y-%m-%d %H:%M UTC") if session.token_expiry else "?"
        log(f"Login captured (token expires {exp}). Letting the dashboard settle...")
        await page.wait_for_timeout(8000)

        log("Discovering accounts/meters from the portal's own traffic...")
        meters = []
        try:
            meters = await asyncio.wait_for(session.list_meters(), timeout=90)
        except (AlectraError, asyncio.TimeoutError) as exc:
            log(f"discovery failed: {exc}")
        for m in meters:
            log(f"  found: account {m.account_number} / meter {m.meter_number} {m.label}")
        if not meters and not (args.account and args.meter):
            log("No meter discovered and no --account/--meter given. Portal calls seen:")
            for url in session.captured_urls()[:25]:
                log(f"    {url[:140]}")
            return 3
        account = args.account or meters[0].account_number
        meter = args.meter or meters[0].meter_number
        if args.account or args.meter:
            log(f"Using account {account} / meter {meter} (manual override)")
        else:
            log(f"Using account {account} / meter {meter}")

        cache = UsageCache()
        start = month_start(args.months)
        end = date.today().replace(day=1)
        log(f"Fetching hourly usage {start} .. {end} (cache: {cache.root})")

        state = {"done": 0}

        async def on_month(done: int, total: int, key: str, records: int, elapsed: float, cached: bool):
            state["done"] = done
            src = "cache" if cached else f"{records} records in {elapsed:.1f}s"
            log(f"  [{done}/{total}] {key} -> {src}")

        entries = await session.fetch_range(
            account, meter, start, end, on_month_done=on_month, cache=cache
        )
        if not entries:
            log("Usage API returned zero records for the whole range.")
            return 4
        log(f"Fetched {len(entries)} hourly records total; analysing...")

        records = [UsageRecord.from_api(e) for e in entries]
        result = analysis.analyze(records)
        totals = result["totals"]

        OUT_DIR.mkdir(exist_ok=True)
        stem = f"analysis_{account}_{meter}"
        (OUT_DIR / f"{stem}.json").write_text(json.dumps(result, indent=1))

        months_rows = result["months"]
        csv_path = OUT_DIR / f"{stem}.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["month", "kwh", "tiered_cad", "tou_cad", "ulo_cad", "best"])
            for m in months_rows:
                writer.writerow(
                    [m["key"], m["kwh"], m["plans"]["tiered"], m["plans"]["tou"], m["plans"]["ulo"], m["best"] or ""]
                )

        print("\n=== Plan comparison (energy charge only, CAD) ===")
        print(f"{'Month':<10}{'kWh':>8}{'Tiered':>10}{'TOU':>10}{'ULO':>10}   Best")
        for m in months_rows:
            p = m["plans"]
            print(
                f"{m['key']:<10}{m['kwh']:>8.0f}"
                f"{p['tiered']:>10.2f}{p['tou']:>10.2f}"
                f"{(p['ulo'] if p['ulo'] is not None else float('nan')):>10.2f}   {PLAN_NAMES.get(m['best'], '-')}"
            )
        print(
            f"{'TOTAL':<10}{totals['kwh']:>8.0f}"
            f"{totals['tiered']:>10.2f}{totals['tou']:>10.2f}{totals['ulo']:>10.2f}   {PLAN_NAMES.get(totals['best'], '-')}"
        )
        if totals.get("savings_vs"):
            for plan, saving in totals["savings_vs"].items():
                print(f"  best saves ${saving:.2f} vs {PLAN_NAMES.get(plan, plan)}")

        if args.tesla_kwh:
            monthly = {
                m["key"]: args.tesla_kwh for m in months_rows
            }
            sim = analysis.simulate_tesla_shift(records, monthly)
            t = sim["totals"]
            print(f"\n=== EV simulation: {args.tesla_kwh} kWh/month overnight ===")
            print(f"  charged: {t['charged_kwh']:.0f} kWh total")
            print(f"  TOU: {t['tou']:.2f} -> {t['tou_shifted']:.2f} (shifted)")
            print(f"  ULO: {t['ulo']:.2f} -> {t['ulo_shifted']:.2f} (shifted)")
            print(f"  Tiered unchanged: {t['tiered']:.2f}")
            after = {"tiered": t["tiered"], "tou": t["tou_shifted"], "ulo": t["ulo_shifted"]}
            best = min(after, key=after.get)
            print(f"  -> cheapest after shifting: {PLAN_NAMES[best]} at ${after[best]:.2f}")
            (OUT_DIR / f"{stem}_tesla.json").write_text(json.dumps(sim, indent=1))

        log(f"Report written: {OUT_DIR / (stem + '.json')} (+ .csv)")
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--tesla-kwh", type=float, default=0.0)
    parser.add_argument("--account", default=None)
    parser.add_argument("--meter", default=None)
    parser.add_argument("--login-timeout", type=float, default=300.0)
    parser.add_argument(
        "--username",
        default=None,
        help="auto-fill the login form; password from ALECTRA_PASSWORD or ~/.alectra-credentials",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
