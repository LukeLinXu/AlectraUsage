"""My Alectra (SilverBlaze SCM) API client.

Login runs in a real Chromium page so reCAPTCHA v3 is solved by the portal
itself and every request carries a browser-grade TLS fingerprint — the API
host (alectra-svc.smartcmobile.link) sits behind Cloudflare and blocks
non-browser clients. After login, all API calls are made *inside that page*
via ``fetch`` for the same reason.

Endpoint knowledge:
- POST {API}/UsermanagementAPI/api/1/Login/auth        -> accessToken/refreshToken
- POST {API}/UsermanagementAPI/api/1/Login/auth/refresh
- POST {API}/UsermanagementAPI/api/1/users/SendTfa | VerifyTfa   (if TFA enabled)
- account/meter enumeration: login/SyncUserAccountDetails, then
  account/GetMetersByUuidOrAccountID (exact params learned at runtime)
- GET  {API}/UsageAPI/api/V1/Electric?AccountNumber=&MeterNumber=&From=&To=&Uom=kWh&Periodicity=HH
"""

from __future__ import annotations

import asyncio
import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

PORTAL_URL = "https://myalectra.alectrautilities.com/portal/#/login"
PORTAL_ORIGIN = "https://myalectra.alectrautilities.com"
API_BASE = "https://alectra-svc.smartcmobile.link"

# The API host sits behind Cloudflare: with Playwright's default
# HeadlessChrome UA the requests are 403-challenged (which the SPA surfaces
# as CORS errors) and no login response ever arrives. A real Chrome UA plus
# the AutomationControlled opt-out passes cleanly — verified 2026-10-23.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

_TAG_RE = re.compile(r"<[^>]+>")

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}?")


class AlectraError(Exception):
    """Fatal client error with a user-facing message."""


class LoginFailed(AlectraError):
    pass


class TfaRequired(AlectraError):
    """Raised when the account needs a one-time code; call submit_tfa()."""


@dataclass
class MeterInfo:
    account_number: str
    meter_number: str
    label: str = ""


@dataclass
class AlectraSession:
    """One logged-in browser session. Use as an async context manager."""

    headless: bool = True
    _pw = None
    _browser: Browser | None = None
    _context: BrowserContext | None = None
    page: Page | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expiry: datetime | None = None
    tfa_pending: bool = False
    last_login_response: dict = field(default_factory=dict)
    _captured: list[dict] = field(default_factory=list)  # SPA's own API traffic
    _recorder_attached: bool = False
    _token_candidates: list[str] = field(default_factory=list)
    _token_validated: bool = False

    async def __aenter__(self) -> "AlectraSession":
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless, args=LAUNCH_ARGS
        )
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        await self._context.add_init_script(_INIT_SCRIPT)
        self.page = await self._context.new_page()
        self.page.set_default_timeout(45_000)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        self.access_token = self.refresh_token = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    # ------------------------------------------------------------ login

    async def login(self, username: str, password: str) -> None:
        """Fill the portal login form and capture the auth response."""
        page = self.page
        await page.goto(PORTAL_URL, wait_until="domcontentloaded")
        await page.wait_for_selector("#username", state="visible", timeout=45_000)

        await page.fill("#username", username)
        await page.fill("#password", password)
        async with page.expect_response(
            lambda r: "/Login/auth" in r.url and "/refresh" not in r.url,
            timeout=30_000,
        ) as resp_info:
            await page.click('button:has-text("Log In")')

        body = await self._safe_json(await resp_info.value)
        self.last_login_response = _mask(body)
        token = _extract_access_token(body)
        if not token:
            await self._handle_no_token(body)
            return

        self._set_tokens(token, body)

        # Record the portal's own post-login traffic. The SPA loads the
        # dashboard and calls the account/meter endpoints itself — with the
        # exact right URLs, params, cookies and WAF state. We mine those
        # responses instead of guessing endpoint shapes: direct calls from
        # a fresh context hang behind Cloudflare even with a valid token.
        await self.attach_recorder()
        await page.wait_for_timeout(7000)  # let the dashboard fire its calls

    async def _record_response(self, response) -> None:
        try:
            url = response.url
            if "smartcmobile.link" not in url or "/Login/auth" in url:
                return
            body = await self._safe_json(response)
            post = None
            try:
                post = response.request.post_data
            except Exception:
                pass
            self._captured.append({"url": url, "json": body, "post": post})
        except Exception:
            pass

    def captured_urls(self) -> list[str]:
        return [c["url"] for c in self._captured]

    async def attach_recorder(self) -> None:
        """Start recording the portal's own API traffic (for mining later)."""
        if self._recorder_attached:
            return
        self._recorder_attached = True
        self._captured.clear()
        self.page.on("response", self._record_response)

    async def wait_for_manual_login(self, timeout_s: float = 300.0) -> bool:
        """Poll localStorage until the user finishes logging in the visible page.

        Returns True and stores the tokens on success. The password is typed
        by the user into the real portal — it never passes through code.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            raw = await self.page.evaluate("() => localStorage.getItem('tokenKey')")
            candidates = _collect_token_candidates(str(raw)) if raw else []
            if candidates:
                self._set_tokens(candidates[0], {})
                self._token_candidates = candidates
                await self._ensure_working_token()
                return True
            await asyncio.sleep(2)
        return False

    async def submit_tfa(self, code: str) -> None:
        """Enter the one-time code in the portal UI and wait for the token."""
        if not self.page:
            raise AlectraError("session not started")
        visible_inputs = await self.page.locator(
            "input:visible", has_not_text=""
        ).all()
        target = None
        for handle in visible_inputs:
            ident = (
                (await handle.get_attribute("id") or "")
                + (await handle.get_attribute("name") or "")
                + (await handle.get_attribute("aria-label") or "")
                + (await handle.get_attribute("placeholder") or "")
            ).lower()
            if any(word in ident for word in ("code", "otp", "tfa", "verification")):
                target = handle
                break
        if target is None:
            raise AlectraError(
                "Could not find the verification-code input on the page. "
                "Run with show_browser=true and enter the code manually."
            )
        await target.fill(code)
        await target.press("Enter")
        # Wait until any response carries an access token (VerifyTfa or a retry).
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            token = await self._token_from_localstorage()
            if token:
                self._set_tokens(token, {})
                self.tfa_pending = False
                await self._ensure_working_token()
                await self.page.wait_for_timeout(5000)  # dashboard calls -> _captured
                return
            await asyncio.sleep(1)
        raise LoginFailed("TFA code did not produce an access token within 30s.")

    async def _handle_no_token(self, body: dict) -> None:
        status = body.get("status") if isinstance(body, dict) else None
        message = ""
        if isinstance(status, dict):
            message = _TAG_RE.sub("", str(status.get("message") or "")).strip()
        if not message:
            message = str(body.get("Message") or body.get("message") or "")
        flat = json.dumps(body).lower()
        if "tfa" in flat or "two factor" in flat or "verification" in flat:
            self.tfa_pending = True
            raise TfaRequired(
                "This account requires a one-time verification code. "
                "Check your phone/email, then submit the code."
            )
        if "recaptcha" in flat:
            raise LoginFailed(
                "The portal rejected the reCAPTCHA check. "
                "Retry with show_browser=true."
            )
        raise LoginFailed(message or "Login rejected by the portal.")

    def _set_tokens(self, token: str, body: dict) -> None:
        self.access_token = token
        data = body.get("data") or {}
        if isinstance(data, dict):
            data = data.get("data") or data
        if isinstance(data, dict):
            self.refresh_token = data.get("refreshToken")
        self.token_expiry = _jwt_expiry(token)
        self._token_validated = False
        # The login response carries accessToken AND refreshToken, both JWTs;
        # field order in the JSON decides which one a naive regex grabs, so
        # keep every candidate and let _ensure_working_token pick the real one.
        self._token_candidates = _collect_token_candidates(body)
        if token not in self._token_candidates:
            self._token_candidates.insert(0, token)

    async def _ensure_working_token(self) -> None:
        """Validate a token against an authed endpoint; swap until one works."""
        if self._token_validated and self.access_token:
            return
        last_error: Exception | None = None
        for candidate in self._token_candidates:
            uuid_claim = _uuid_from_token(candidate)
            if not uuid_claim:
                continue
            self.access_token = candidate
            self.token_expiry = _jwt_expiry(candidate)
            try:
                await self.api(
                    "GET",
                    f"/AccountManagementAPI/api/1/Addresses/User/{uuid_claim}",
                    timeout_s=15,
                )
                self._token_validated = True
                return
            except AlectraError as exc:
                last_error = exc
        self.access_token = None
        raise AlectraError(
            f"None of the {len(self._token_candidates)} tokens from the login "
            f"response were accepted: {last_error}"
        )

    async def _token_from_localstorage(self) -> str | None:
        raw = await self.page.evaluate("() => localStorage.getItem('tokenKey')")
        if not raw:
            return None
        found = _JWT_RE.findall(str(raw))
        return found[0] if found else None

    @staticmethod
    async def _safe_json(response) -> dict:
        try:
            return await response.json()
        except Exception:
            try:
                return {"raw": (await response.text())[:2000]}
            except Exception:
                return {}

    # ------------------------------------------------------------ transport

    async def api(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        timeout_s: float = 25.0,
    ) -> dict:
        """Call the SCM API from inside the page (browser TLS + CORS + cf cookies).

        The in-page fetch carries an AbortController timeout: a Cloudflare
        challenge loop would otherwise hang the request forever.
        """
        if not self.page:
            raise AlectraError("session not started")
        if not self.access_token:
            raise AlectraError("not logged in")
        url = API_BASE + path
        if params:
            from urllib.parse import urlencode

            url += ("&" if "?" in url else "?") + urlencode(params)
        try:
            result = await self.page.evaluate(
                """async ({method, url, body, token, timeoutMs}) => {
                    return await new Promise((resolve) => {
                        const xhr = new XMLHttpRequest();
                        let settled = false;
                        const finish = (r) => {
                            if (!settled) { settled = true; clearTimeout(timer); resolve(r); }
                        };
                        const timer = setTimeout(
                            () => { xhr.abort(); finish({status: 0, body: 'CLIENT_ABORT'}); },
                            timeoutMs,
                        );
                        try {
                            // XHR, not fetch: fetch() hangs behind Cloudflare on this
                            // host while XHR (what the portal's axios uses) responds.
                            // No withCredentials: the API is bearer-token-only and the
                            // server sends no Access-Control-Allow-Credentials, so
                            // credentialed requests are rejected pre-flight.
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
                }""",
                {
                    "method": method,
                    "url": url,
                    "body": body,
                    "token": self.access_token,
                    "timeoutMs": int(timeout_s * 1000),
                },
            )
        except Exception as exc:
            raise AlectraError(
                f"Request to {path} failed or timed out after {timeout_s:.0f}s. "
                "If this repeats, restart and log in with 'show browser' enabled."
            ) from exc
        if result["status"] == 0:
            # transport-level failure (CORS/network/abort) — never swallow it:
            # an empty result would look like "no data for this month"
            raise AlectraError(f"Request to {path} failed at transport level ({result['body'][:80]}).")
        if result["status"] == 403 and "cloudflare" in result["body"].lower():
            raise AlectraError("Blocked by Cloudflare; retry in a browser session.")
        if result["status"] == 401:
            raise AlectraError("Session expired (401); log in again.")
        try:
            return json.loads(result["body"])
        except json.JSONDecodeError:
            return {"_status": result["status"], "_raw": result["body"][:2000]}

    # ------------------------------------------------------------ high level

    async def list_meters(self) -> list[MeterInfo]:
        """Discover accounts + meters for the logged-in user.

        Primary source: the SPA's own post-login traffic captured in
        ``login()`` — its responses contain the real account/meter records.
        Fallback: the ``Addresses/User/{uuid}`` endpoint the SPA uses, with
        the uuid taken from the JWT claims. Shapes are parsed defensively
        (nothing here is publicly documented).
        """
        await self._ensure_working_token()
        meters: list[MeterInfo] = []
        seen: set[tuple[str, str]] = set()

        def add_from(payload: dict) -> None:
            for acc in _harvest_accounts(payload) or [None]:
                for m in _harvest_meters(payload, acc):
                    key = (m.account_number, m.meter_number)
                    if key not in seen and m.meter_number:
                        seen.add(key)
                        meters.append(m)

        # 1) mine the portal's own traffic
        for cap in list(self._captured):
            add_from(cap["json"])
        if meters:
            return meters

        # 2) direct call to the endpoint the SPA itself uses
        uuid_claim = _uuid_from_token(self.access_token or "")
        if uuid_claim:
            try:
                payload = await self.api(
                    "GET",
                    f"/AccountManagementAPI/api/1/Addresses/User/{uuid_claim}",
                    timeout_s=25,
                )
                add_from(payload)
            except AlectraError:
                pass
            if meters:
                return meters

        # 3) last resort: the documented-by-bundle meter endpoint
        try:
            payload = await self.api(
                "GET",
                "/UsermanagementAPI/api/1/account/GetMetersByUuidOrAccountID",
                params={"uuid": uuid_claim} if uuid_claim else {},
                timeout_s=15,
            )
            add_from(payload)
        except AlectraError:
            pass
        return meters

    async def fetch_month(
        self, account_number: str, meter_number: str, year: int, month: int
    ) -> list[dict]:
        """Raw hourly usage entries for one calendar month."""
        first = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        payload = await self.api(
            "GET",
            "/UsageAPI/api/V1/Electric",
            params={
                "AccountNumber": account_number,
                "MeterNumber": meter_number,
                "From": first.isoformat(),
                "To": last_day.isoformat(),
                "Uom": "kWh",
                "Periodicity": "HH",
            },
        )
        if payload.get("StatusCode") not in (None, 200, "200", 0, "0"):
            raise AlectraError(
                f"Usage API error for {year}-{month:02d}: "
                f"{payload.get('Message') or json.dumps(payload)[:200]}"
            )
        result = payload.get("Result") or {}
        return result.get("electricUsages") or []

    async def fetch_range(
        self,
        account_number: str,
        meter_number: str,
        start: date,
        end: date,
        *,
        on_month_done=None,
        cache=None,
        month_timeout_s: float = 90.0,
    ) -> list[dict]:
        """Fetch months from ``start`` to ``end`` inclusive, with cache.

        Each month is hard-capped at ``month_timeout_s`` at the Python level
        (on top of the in-page fetch abort) so a wedged request can never
        stall a whole run silently.
        """
        entries: list[dict] = []
        months = _months_between(start, end)
        for i, (year, month) in enumerate(months):
            key = f"{year:04d}-{month:02d}"
            if cache and (cached := cache.load(account_number, meter_number, key)):
                entries.extend(cached)
                if on_month_done:
                    await _maybe_await(on_month_done(i + 1, len(months), key, len(cached), 0.0, True))
                continue
            t0 = asyncio.get_running_loop().time()
            try:
                data = await asyncio.wait_for(
                    self.fetch_month(account_number, meter_number, year, month),
                    timeout=month_timeout_s,
                )
            except asyncio.TimeoutError as exc:
                raise AlectraError(
                    f"Fetching {key} exceeded {month_timeout_s:.0f}s; aborted."
                ) from exc
            elapsed = asyncio.get_running_loop().time() - t0
            if cache:
                cache.store(account_number, meter_number, key, data)
            entries.extend(data)
            if on_month_done:
                await _maybe_await(
                    on_month_done(i + 1, len(months), key, len(data), elapsed, False)
                )
        return entries


def _months_between(start: date, end: date) -> list[tuple[int, int]]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


async def _maybe_await(value):
    if asyncio.iscoroutine(value):
        return await value
    return value


# ------------------------------------------------------------------ parsing


def _extract_access_token(body: dict) -> str | None:
    if not isinstance(body, dict):
        return None
    tokens = _JWT_RE.findall(json.dumps(body))
    return tokens[0] if tokens else None


def _find_key(node, key_name):
    """Depth-first search for a JSON field (case-insensitive) holding a JWT."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() == key_name.lower() and isinstance(v, str) and v.count(".") == 2:
                return v
        for v in node.values():
            found = _find_key(v, key_name)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key_name)
            if found:
                return found
    return None


def _collect_token_candidates(source) -> list[str]:
    """JWT candidates with any field literally named 'accessToken' first.

    The login response (and localStorage tokenKey) contain accessToken AND
    refreshToken, both JWTs; naive regex-first grabs whichever appears first
    and the API 401s on the refresh token.
    """
    candidates: list[str] = []
    try:
        access = _find_key(source, "accessToken")
        if access:
            candidates.append(access)
    except Exception:
        pass
    text = source if isinstance(source, str) else json.dumps(source, default=str)
    for tok in _JWT_RE.findall(text):
        if tok not in candidates:
            candidates.append(tok)
    return candidates


def _jwt_expiry(token: str) -> datetime | None:
    claims = _jwt_claims(token)
    if claims and (exp := claims.get("exp")):
        try:
            return datetime.utcfromtimestamp(int(exp))
        except (TypeError, ValueError, OSError):
            return None
    return None


def _uuid_from_token(token: str) -> str | None:
    claims = _jwt_claims(token)
    if claims and claims.get("Uuid"):
        return str(claims["Uuid"])
    return None


def _jwt_claims(token: str) -> dict | None:
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def _mask(body: dict) -> dict:
    """Copy of a response with JWTs redacted (safe to show/store)."""
    return json.loads(_JWT_RE.sub("<redacted-token>", json.dumps(body, default=str)))


def _harvest_accounts(payload: dict) -> list[dict]:
    """Find dicts anywhere in the payload that look like account records."""
    accounts: list[dict] = []
    seen: set[str] = set()

    def visit(node):
        if isinstance(node, dict):
            keys = {k.lower(): k for k in node}
            acc = (
                node.get(keys.get("accountnumber", "_"), None)
                if "accountnumber" in keys
                else None
            )
            if acc:
                sacc = str(acc)
                if sacc not in seen:
                    seen.add(sacc)
                    accounts.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return accounts


def _harvest_meters(payload: dict, account: dict | None) -> list[MeterInfo]:
    meters: list[MeterInfo] = []
    acc_no = ""
    if account:
        for key, value in account.items():
            if key.lower() == "accountnumber" and value:
                acc_no = str(value)
                break

    def visit(node):
        if isinstance(node, dict):
            keys = {k.lower(): k for k in node}
            meter = node.get(keys.get("meternumber", "_")) if "meternumber" in keys else None
            if meter:
                label = str(
                    node.get(keys.get("serviceaddress", ""))
                    or node.get(keys.get("address", ""))
                    or node.get(keys.get("accountnickname", ""))
                    or ""
                )
                meters.append(
                    MeterInfo(
                        account_number=str(node.get(keys.get("accountnumber", "")) or acc_no or ""),
                        meter_number=str(meter),
                        label=label,
                    )
                )
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return meters
