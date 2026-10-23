# ⚡ Alectra Plan Compare

**Which Ontario electricity rate plan is cheapest for *your* actual usage?**
Tiered, Time-of-Use (TOU), or Ultra-Low Overnight (ULO) — computed from your real
hourly smart-meter data, not averages or guesses.

Log in to My Alectra from a small local web app, pick one of your meters, pull the
last 6–24 months of hourly usage, and see exactly what each plan would have cost,
month by month — plus an EV/Tesla overnight-charging simulation.

**哪档电价最划算？** 用你自己的逐小时电表数据算 Tiered / TOU / ULO 三种方案，
支持 Tesla 深夜充电挪峰模拟。中文说明见下方。

---

## Quick start

Requires Python 3.11+ (uses [Playwright](https://playwright.dev) Chromium; download
it once):

```bash
git clone <this-repo> && cd alectra-plan-compare
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
.venv/bin/alectra-plans          # opens http://127.0.0.1:8765 in your browser
```

No account handy? Click **“Try with sample data”** on the login screen to see the
full report with bundled real (anonymized-shaped) monthly data.

## How it works

1. **Log in** — the app drives the real My Alectra portal in a Chromium window
   (headless by default). The portal's own reCAPTCHA runs as usual, so you don't
   solve puzzles. If your account has two-factor authentication, the app asks for
   the one-time code.
2. **Pick a meter** — accounts can have several meters; the app lists what your
   account has (or enter the account + meter numbers manually).
3. **Fetch** — one API request per month of hourly data (results cached locally in
   `~/.cache/alectra-plan-compare/`, so re-runs are instant).
4. **Compare** — every hour is priced on all three plans using the OEB rate period
   that was actually in effect that month, with Ontario holidays treated as
   off-peak. Delivery/regulatory charges and HST are excluded (identical on all
   plans).

### The EV simulation

Enter your monthly charging kWh; the tool removes them from your highest-usage
hours (keeping 1 kWh/h as base house load) and re-prices them at the 11pm–7am
rate. That's exact for TOU and ULO because 23:00–07:00 is a single flat block in
both plans.

## Privacy & security

- Runs **entirely on your machine**, binds to `127.0.0.1` only.
- Your password goes once to Alectra's login endpoint through the app's browser and
  is never written to disk or logs. Tokens stay in memory and die with the process.
- Fetched usage data is cached under `~/.cache/alectra-plan-compare/` (0600
  permissions) purely so you don't re-download it.

## Honest caveats

- **Unofficial.** This drives a portal that has no public API. Alectra can change
  it at any time; if login breaks, check for an updated repo. Don't hammer it —
  the tool fetches one month per request.
- **Energy charge only.** Delivery, regulatory, HST, and the Ontario Electricity
  Rebate are the same regardless of plan, so they're excluded from the
  comparison. The absolute bill totals will differ from your actual bill.
- Tiered thresholds assume residential (600 kWh summer / 1000 kWh winter).
- Rate tables live in `alectra_plans/rates.py`, transcribed from the
  [OEB](https://www.oeb.ca/rates-and-your-bill/electricity-rates). The OEB now
  sets prices once a year (Nov 1); add the new period there each autumn.

## Rate data (verified from OEB, ¢/kWh)

| Effective | TOU off/mid/on | Tiered T1/T2 | ULO night / wknd-off / mid / on |
|---|---|---|---|
| Nov 1 2026 | 9.1 / 14.6 / 18.9 | 11.2 / 13.2 | 3.7 / 9.1 / 14.6 / 37.7 |
| Nov 1 2025 | 9.8 / 15.7 / 20.3 | 12.0 / 14.2 | 3.9 / 9.8 / 15.7 / 39.1 |
| Nov 1 2024 | 7.6 / 12.2 / 15.8 | 9.3 / 11.0 | 2.8 / 7.6 / 12.2 / 28.4 |
| Nov 1 2023 | 8.7 / 12.2 / 18.2 | 10.3 / 12.5 | 2.8 / 8.7 / 12.2 / 28.6 |

---

## 中文说明

这个工具回答一个问题：**按你的真实用电习惯，哪种电价方案最便宜？**

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
.venv/bin/alectra-plans
```

- 启动后自动打开 `http://127.0.0.1:8765`,输入 My Alectra 用户名密码(只在本机直传
  Alectra,不落盘；开了双因素会提示输验证码)。
- 选择电表 → 抓取过去 6–24 个月的逐小时数据(按月缓存,重跑秒出)。
- 结果页:三方案逐月对比 + 总账 + 每月最便宜方案 + 用电时段曲线 + CSV 导出。
- **Tesla 模拟**:输入每月充电度数,按 23:00–07:00 重算,看挪峰后哪个方案反超。

结论参考(基于本机真实数据):不挪峰时 Tiered 在低用电月最划算;只要把 Tesla 充电
全部放到深夜 11 点后,**ULO 就是最便宜的方案**。

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/          # rate engine + real-data integration tests
.venv/bin/python scripts/spike_login.py <user> <pass> --month 2025-06
                                 # end-to-end smoke test with your own credentials
.venv/bin/python scripts/recon_login.py [--headed]   # inspect the portal login page
```

Project layout:

```
alectra_plans/
  rates.py     # OEB rate tables, holidays, per-hour pricing engine
  analysis.py  # monthly aggregation, totals, Tesla shift simulation
  client.py    # Playwright login + in-page fetch transport + meter discovery
  cache.py     # local 0600 JSON cache (~/.cache/alectra-plan-compare)
  server.py    # FastAPI app + static frontend (4-step wizard)
scripts/       # recon & smoke-test utilities
tests/         # pytest incl. real captured usage fixtures
```

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Alectra Utilities or the OEB.
