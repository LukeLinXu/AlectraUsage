# ⚡ Alectra Plan Compare

**Which Ontario electricity rate plan is cheapest for *your* actual usage?**
Tiered, Time-of-Use (TOU), or Ultra-Low Overnight (ULO) — computed hour by hour
from your real smart-meter data, not averages or guesses.

Point it at your My Alectra account and it will log in, list the meters on your
account, pull up to 24 months of hourly usage, and price every hour on all
three plans using the OEB rate schedule that was actually in effect each month.
Includes an EV / Tesla overnight-charging simulation.

**中文教程在下方(中文说明部分)。**

---

## Features


- 🔐 **Automated portal login** in a real Chromium window — the portal's own
  reCAPTCHA runs as usual, nothing to solve. Supports 2FA.
- 📊 **Meter discovery** — accounts can hold several meters; the tool lists
  them (or type the account + meter numbers manually).
- ⏱ **Hourly accuracy** — every hour is classified into its TOU/ULO period
  (season, weekday/weekend, statutory holidays) and priced with the rate
  schedule in effect that month. Tiering is applied per month per OEB rules.
- 🚗 **EV simulation** — re-prices N kWh/month of charging into the
  11pm–7am window and shows which plan wins after the shift.
- 📈 **Web UI** (four-step wizard with charts) **and** a headless CLI —
  same engine.
- 💾 **Local cache** — fetched months are stored locally; re-runs are instant.
- 🧩 **ZCode skill bundle** — folder you can drop into an AI agent as a skill
  and share with friends.

## Requirements

- macOS / Linux with **Python 3.11+**
- An **Alectra Utilities** account (My Alectra portal) — this tool speaks
  Alectra's portal API only
- ~200 MB disk for the Playwright Chromium download

## Quick start (web UI)

```bash
git clone git@github.com:LukeLinXu/AlectraUsage.git
cd AlectraUsage
python3.12 -m venv .venv            # any Python ≥ 3.11
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
.venv/bin/alectra-plans             # opens http://127.0.0.1:8765
```

Then in the browser page that opens:

1. **Log in** with your My Alectra username/password. If your account has
   two-factor authentication, an input for the one-time code appears.
   If login seems to hang, tick *“Show the browser window”* and retry.
2. **Pick a meter** from the list (or expand *Enter account + meter manually*).
3. **Fetch** — choose 6/12/18/24 months back; one API request per month, with
   a progress bar.
4. **Results** — per-month and total costs on all three plans, which plan wins
   each month, an hourly-usage profile chart, the Tesla simulation panel, and
   a CSV download.

No account handy? Click **“Try with sample data”** to see the full report with
bundled fixtures.

## CLI / skill usage

```bash
.venv/bin/python scripts/skill_fetch.py --username <name> --months 12 --tesla-kwh 250
```

| Flag | Default | Meaning |
|---|---|---|
| `--months` | `12` | How far back (1–36; current partial month included) |
| `--tesla-kwh` | off | Simulate N kWh/month of EV charging, all overnight |
| `--username` | manual login | Auto-fill the login form; password read from `~/.alectra-credentials` or `$ALECTRA_PASSWORD` |
| `--account` / `--meter` | auto-discovered | Skip meter discovery |
| `--login-timeout` | `300` | Seconds to wait for a manual login |

Without `--username`, a Chromium window opens and **you log in yourself** —
the script never sees the password. With `--username`, create the credentials
file first (it stays on your machine, chmod 600):

```bash
cat > ~/.alectra-credentials <<'EOF'
{"username": "you@example.com", "password": "secret"}
EOF
chmod 600 ~/.alectra-credentials
```

Output: a per-month comparison table in the terminal plus
`data/analysis_<account>_<meter>.json` and `.csv`. Raw months are cached in
`~/.cache/alectra-plan-compare/` (files 0600) — delete the folder to refetch.

### As a ZCode skill

The `~/.agents/skills/alectra-plan-compare/` folder bundles this project with
`setup.sh` (one-shot env bootstrap) and `save_credentials.py` (interactive
hidden password prompt, run it yourself in a terminal). Copy the folder into
the recipient's `~/.agents/skills/`, run setup once, then just tell the agent
*“run the alectra skill for the last 12 months”*. See its `SKILL.md`.

## Understanding the numbers

- **Energy charge only.** Delivery, regulatory, HST and the Ontario
  Electricity Rebate are identical across plans, so they're excluded — your
  real bill will be higher, but the *ranking* of plans is unaffected.
- The **current (partial) month** under-states a full month.
- Tiered assumes **residential** thresholds (600 kWh/month May–Oct,
  1,000 kWh/month Nov–Apr).
- Sanity check: every fetched hour carries the charge Alectra's own billing
  recorded for it; the tool shows it as *“Alectra billed”* in the web UI, and
  the test suite asserts the engine reproduces it.

## How it works

```
Playwright Chromium ── login on the real portal (reCAPTCHA v3 by the page,
                       optional TFA) ──> access + refresh token
        │
        └─ in-page XHR (browser TLS — passes Cloudflare) ──>
             AccountManagementAPI  → meters on your account
             UsageAPI/api/V1/Electric?Periodicity=HH  → hourly kWh, month by month
        │
   rate engine (alectra_plans/rates.py)
     OEB schedules by effective date + Ontario holiday calendar
        │
   analysis: Tiered / TOU / ULO per hour → month → totals → EV simulation
```

Verified details, for the curious:

- The portal is a **SilverBlaze "Smart Customer Mobile"** SPA; login is
  `POST UsermanagementAPI/api/1/Login/auth` and the usage endpoint is
  `GET UsageAPI/api/V1/Electric?AccountNumber=&MeterNumber=&From=&To=&Uom=kWh&Periodicity=HH`.
- The API host sits behind **Cloudflare**: non-browser TLS gets 403, and
  plain `fetch()` from the page silently hangs — the working transport is an
  in-page **XMLHttpRequest**, same as the portal's own axios calls.
- The login response contains **accessToken and refreshToken, both JWTs** —
  only the former works on business endpoints (the token is validated against
  an authenticated endpoint before use).
- Meter discovery mines the portal's **own post-login traffic** (the dashboard
  calls account/meter endpoints itself) instead of guessing shapes.

## Privacy & security

- Runs **entirely on your machine**; the web UI binds to `127.0.0.1` only.
- The password goes **once** to Alectra's login endpoint through the app's
  browser and is never written to disk or logs. Tokens stay in memory and die
  with the process.
- Cached usage data lives under `~/.cache/alectra-plan-compare/` (0600) so you
  don't re-download it.
- Test fixtures in `tests/data/` are real usage shapes with all identifiers
  replaced.

## Rate data (from the OEB, ¢/kWh)

| Effective | TOU off/mid/on | Tiered T1/T2 | ULO night / wknd-off / mid / on |
|---|---|---|---|
| Nov 1 2026 | 9.1 / 14.6 / 18.9 | 11.2 / 13.2 | 3.7 / 9.1 / 14.6 / 37.7 |
| Nov 1 2025 | 9.8 / 15.7 / 20.3 | 12.0 / 14.2 | 3.9 / 9.8 / 15.7 / 39.1 |
| Nov 1 2024 | 7.6 / 12.2 / 15.8 | 9.3 / 11.0 | 2.8 / 7.6 / 12.2 / 28.4 |
| Nov 1 2023 | 8.7 / 12.2 / 18.2 | 10.3 / 12.5 | 2.8 / 8.7 / 12.2 / 28.6 |

Sources: [OEB electricity rates](https://www.oeb.ca/rates-and-your-bill/electricity-rates)
· [historical rates](https://www.oeb.ca/rates-and-your-bill/electricity-rates/historical-electricity-rates)
· holiday schedule per [Alectra TOU page](https://alectrautilities.com/time-use-pricing).

The OEB now sets prices **once a year on Nov 1**. When a new period starts,
append it to `RPP_PERIODS` in `alectra_plans/rates.py` (and the new year's
holidays to `HOLIDAYS`) — then `pytest tests/` should stay green.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Login hangs with no response | Retry with the *show browser* box ticked; watch the window |
| `Fetching YYYY-MM exceeded 90s` | WAF/network wedge; rerun once (cached months are skipped) |
| `401` / session expired | Token died mid-run; rerun |
| No meters discovered | Enter account + meter manually (they're on your bill) |
| Wrong-plan numbers vs your bill | Are you comparing *total* bill? This tool ranks energy charge only |

## 中文说明

这个工具回答一个问题:**按你的真实用电习惯,哪种电价方案最便宜?**

安省电费有三种方案可以自己选:

- **Tiered 阶梯价**:不看时间,按月用量分级(夏 600 度/冬 1000 度以内一个价)
- **TOU 分时**:什么时候用电决定价格,工作日白天贵、晚 7 点后和周末便宜
- **ULO 超低谷**:每天 23:00–7:00 只要 3.9¢(全场最低),但工作日 16:00–21:00 高达 39.1¢

**怎么选(按生活方式):**

- 🏠 **WFH 全天在家** → 大概率 **Tiered**:白天的用电躲不开任何高价时段
- 🚶 **早出晚归** → 用电习惯好(洗碗机洗衣塞到晚 7 点后和周末)可以试 TOU,否则 Tiered
- 🚗 **有电车能半夜充** → **无脑 ULO**:实测把充电挪到半夜后 ULO 从 $1,166 降到 $974,反超全场

上手三步(见上面 Quick start):装好 → 浏览器里登录 My Alectra → 选电表抓 12 个月。
或者用命令行:`scripts/skill_fetch.py --username 你的用户名 --months 12 --tesla-kwh 250`
(先按上面的模板创建 `~/.alectra-credentials`,密码只存在本机)。

隐私:全部本地运行;密码不落盘、不经过任何第三方;原始数据缓存在本机便于重跑。

## Development

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest tests/          # 29 tests: rate engine + real-data cross-checks
.venv/bin/python scripts/recon_login.py [--headed]   # inspect the portal login page
```

Layout: `alectra_plans/` (rates · analysis · client · cache · server + static)
· `scripts/` (skill entry + diagnostics) · `tests/`.

## Disclaimer

Unofficial tool for Alectra customers, for querying **your own** account data.
The portal has no public API and can change at any time; be gentle (the tool
fetches one month per request). Not affiliated with Alectra Utilities or the
OEB. MIT — see [LICENSE](LICENSE).
