/* Alectra Plan Compare — front-end logic (vanilla JS). */

const $ = (id) => document.getElementById(id);
const state = { sessionId: null, jobId: null, analysis: null, charts: {} };

const money = (v) =>
  v === null || v === undefined ? "—" : "$" + Number(v).toFixed(2);
const show = (id) => $(id).classList.remove("hidden");
const hide = (id) => $(id).classList.add("hidden");

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!resp.ok) {
    let detail = `${resp.status} error`;
    try {
      detail = (await resp.json()).detail || detail;
    } catch {}
    throw new Error(detail);
  }
  return resp.json();
}

/* ---------------------------------------------------------------- step 1 */

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("login-btn");
  btn.disabled = true;
  hide("login-error");
  try {
    const body = {
      username: $("username").value.trim(),
      password: $("password").value,
      show_browser: $("show-browser").checked,
    };
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify(body),
    });
    state.sessionId = data.session_id;
    if (data.status === "tfa_required") {
      show("tfa-row");
      $("tfa-code").focus();
      $("login-btn").textContent = "Verify code";
      btn.disabled = false;
      return;
    }
    await discoverMeters();
  } catch (err) {
    $("login-error").textContent = err.message;
    show("login-error");
    btn.disabled = false;
  }
});

async function discoverMeters() {
  hide("step-login");
  show("step-meter");
  $("meter-list").innerHTML = '<p class="hint">Looking up your meters…</p>';
  try {
    const data = await api(`/api/meters?session_id=${state.sessionId}`);
    showMeterStep(data.meters || [], data.warning);
  } catch (err) {
    showMeterStep([], err.message);
  }
}

$("tfa-code").addEventListener("keydown", async (ev) => {
  if (ev.key !== "Enter") return;
  ev.preventDefault();
  const code = $("tfa-code").value.trim();
  if (!code || !state.sessionId) return;
  try {
    await api("/api/tfa", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId, code }),
    });
    await discoverMeters();
  } catch (err) {
    $("login-error").textContent = err.message;
    show("login-error");
  }
});

$("demo-btn").addEventListener("click", async () => {
  try {
    const data = await api("/api/demo");
    state.sessionId = data.session_id;
    state.jobId = data.job_id;
    await loadResults();
  } catch (err) {
    $("login-error").textContent = err.message;
    show("login-error");
  }
});

/* ---------------------------------------------------------------- step 2 */

function showMeterStep(meters, warning) {
  currentMeters = meters || [];
  hide("step-login");
  show("step-meter");
  const list = $("meter-list");
  list.innerHTML = "";
  if (warning) {
    $("meter-error").textContent = warning;
    show("meter-error");
  } else {
    hide("meter-error");
  }
  if (!currentMeters.length) {
    $("manual-meter").open = true;
    $("manual-meter").querySelector("summary").textContent =
      "Meter lookup returned nothing — enter account + meter manually";
  }
  currentMeters.forEach((m, i) => {
    const row = document.createElement("label");
    row.className = "meter-option";
    row.innerHTML = `<input type="radio" name="meter" value="${i}" ${i === 0 ? "checked" : ""}>
      <div><strong>${m.meter_number}</strong> · account ${m.account_number}
      ${m.label ? `<div class="addr">${m.label}</div>` : ""}</div>`;
    list.appendChild(row);
  });
}

$("fetch-btn").addEventListener("click", async () => {
  hide("meter-error");
  const selected = document.querySelector('input[name="meter"]:checked');
  let account, meter;
  if (selected) {
    const m = currentMeters[Number(selected.value)];
    account = m.account_number;
    meter = m.meter_number;
  } else {
    account = $("manual-account").value.trim();
    meter = $("manual-meter-number").value.trim();
  }
  if (!account || !meter) {
    $("meter-error").textContent = "Pick a meter or enter account + meter numbers.";
    show("meter-error");
    return;
  }
  hide("step-meter");
  show("step-progress");
  try {
    const data = await api("/api/usage", {
      method: "POST",
      body: JSON.stringify({
        session_id: state.sessionId,
        account_number: account,
        meter_number: meter,
        months: Number($("months-back").value),
      }),
    });
    state.jobId = data.job_id;
    pollStatus();
  } catch (err) {
    $("progress-error").textContent = err.message;
    show("progress-error");
  }
});

let currentMeters = [];

/* ---------------------------------------------------------------- step 3 */

async function pollStatus() {
  try {
    const s = await api(
      `/api/usage/status/${state.jobId}?session_id=${state.sessionId}`
    );
    const pct = s.total ? Math.round((s.done / s.total) * 100) : 0;
    $("progress-bar").style.width = pct + "%";
    $("progress-text").textContent =
      s.state === "done"
        ? "Done."
        : `${s.done} of ${s.total} months fetched… (one request per month, be patient)`;
    if (s.state === "running") {
      setTimeout(pollStatus, 2000);
    } else if (s.state === "done") {
      await loadResults();
    } else {
      $("progress-error").textContent = s.error || "Fetch failed.";
      show("progress-error");
    }
  } catch (err) {
    $("progress-error").textContent = err.message;
    show("progress-error");
  }
}

/* ---------------------------------------------------------------- step 4 */

async function loadResults() {
  state.analysis = await api(
    `/api/analysis/${state.jobId}?session_id=${state.sessionId}`
  );
  hide("step-progress");
  hide("step-login");
  show("step-results");
  renderResults(state.analysis);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const PLAN_LABEL = { tiered: "Tiered", tou: "Time-of-Use", ulo: "Ultra-Low Overnight" };

function renderResults(a) {
  const t = a.totals;
  const n = a.months.length;
  $("verdict").innerHTML =
    `Over the last <strong>${n} month${n > 1 ? "s" : ""}</strong> ` +
    `(<span class="hint">${a.months[0]?.label} – ${a.months[n - 1]?.label}</span>), ` +
    `the cheapest plan for your usage is <strong>${PLAN_LABEL[t.best] || t.best}</strong> at ` +
    `<strong>${money(t[t.best])}</strong>` +
    (t.savings_vs
      ? ` — saving ${Object.entries(t.savings_vs)
          .map(([p, v]) => `${money(v)} vs ${PLAN_LABEL[p]}`)
          .join(", ")}.`
      : ".");

  $("summary-cards").innerHTML = ["tiered", "tou", "ulo"]
    .filter((p) => t[p] !== null)
    .map(
      (p) => `
      <div class="plan-card ${p === t.best ? "best" : ""}">
        ${p === t.best ? '<span class="badge">CHEAPEST</span>' : ""}
        <div class="name c-${p}">${PLAN_LABEL[p]}</div>
        <div class="amount">${money(t[p])}</div>
        <div class="per">${Math.round((t.kwh / Math.max(n, 1)) * 10) / 10} kWh/mo avg</div>
      </div>`
    )
    .join("");

  /* table */
  const rows = a.months
    .map((m) => {
      const plans = m.plans;
      const bestName = PLAN_LABEL[m.best] || "";
      return `<tr>
        <td>${m.label}</td>
        <td>${Math.round(m.kwh)}</td>
        <td>${money(plans.tiered)}</td>
        <td>${money(plans.tou)}</td>
        <td>${money(plans.ulo)}</td>
        <td class="best-cell">${bestName}</td>
        <td>${money(m.api_amount)}</td>
      </tr>`;
    })
    .join("");
  $("monthly-table").innerHTML = `
    <thead><tr><th>Month</th><th>kWh</th><th>Tiered</th><th>TOU</th><th>ULO</th><th>Best</th><th>Alectra billed</th></tr></thead>
    <tbody>${rows}
      <tr class="total"><td>Total</td><td>${Math.round(t.kwh)}</td>
      <td>${money(t.tiered)}</td><td>${money(t.tou)}</td><td>${money(t.ulo)}</td>
      <td class="best-cell">${PLAN_LABEL[t.best] || ""}</td><td></td></tr>
    </tbody>`;

  renderMonthlyChart(a);
  renderProfileChart(a.hourly_profile);
  $("tesla-out").innerHTML = "";
}

function renderMonthlyChart(a) {
  const ctx = $("monthly-chart");
  state.charts.monthly?.destroy();
  state.charts.monthly = new Chart(ctx, {
    type: "bar",
    data: {
      labels: a.months.map((m) => m.label),
      datasets: [
        { label: "Tiered", data: a.months.map((m) => m.plans.tiered), backgroundColor: "#64748b" },
        { label: "TOU", data: a.months.map((m) => m.plans.tou), backgroundColor: "#0b6bcb" },
        { label: "ULO", data: a.months.map((m) => m.plans.ulo), backgroundColor: "#7c3aed" },
      ],
    },
    options: {
      responsive: true,
      scales: { y: { title: { display: true, text: "CAD" } } },
    },
  });
}

function renderProfileChart(profile) {
  state.charts.profile?.destroy();
  state.charts.profile = new Chart($("profile-chart"), {
    type: "line",
    data: {
      labels: [...Array(24).keys()].map((h) => `${h}:00`),
      datasets: [
        {
          label: "Weekdays",
          data: profile.weekday,
          borderColor: "#0b6bcb",
          tension: 0.3,
        },
        {
          label: "Weekends & holidays",
          data: profile.weekend,
          borderColor: "#0e8a4d",
          tension: 0.3,
        },
      ],
    },
    options: {
      responsive: true,
      scales: { y: { title: { display: true, text: "avg kWh" } } },
    },
  });
}

/* --------------------------------------------------------------- tesla */

$("tesla-btn").addEventListener("click", async () => {
  const kwh = Number($("tesla-kwh").value || 0);
  if (kwh <= 0) return;
  const btn = $("tesla-btn");
  btn.disabled = true;
  try {
    const sim = await api(`/api/analysis/${state.jobId}/tesla?session_id=${state.sessionId}`, {
      method: "POST",
      body: JSON.stringify({ kwh_per_month: kwh }),
    });
    renderTesla(sim, kwh);
  } catch (err) {
    $("tesla-out").innerHTML = `<p class="error">${err.message}</p>`;
  }
  btn.disabled = false;
});

function renderTesla(sim, kwh) {
  const t = sim.totals;
  const ranked = [
    { name: "Tiered", now: t.tiered, after: t.tiered, cls: "c-tiered" },
    { name: "TOU", now: t.tou, after: t.tou_shifted, cls: "c-tou" },
    { name: "ULO", now: t.ulo, after: t.ulo_shifted, cls: "c-ulo" },
  ].filter((r) => r.now !== null && r.now !== undefined);
  ranked.sort((x, y) => x.after - y.after);
  const winner = ranked[0];
  const baseBest = Math.min(t.tiered ?? Infinity, t.tou ?? Infinity, t.ulo ?? Infinity);
  const improvement = baseBest - winner.after;

  $("tesla-out").innerHTML = `
    <div class="verdict small">
      Charging <strong>${Math.round(t.charged_kwh)} kWh</strong> overnight (11pm–7am):
      <strong>${winner.name}</strong> wins at <strong>${money(winner.after)}</strong>
      ${improvement > 0.5 ? `— about ${money(improvement)} better than your best plan without shifting.` : "— no meaningful change."}
    </div>
    <table>
      <thead><tr><th>Plan</th><th>As used</th><th>With overnight charging</th></tr></thead>
      <tbody>
        ${ranked
          .map(
            (r) => `<tr>
              <td class="${r.cls}">${r.name}</td>
              <td>${money(r.now)}</td>
              <td class="${r.after === winner.after ? "best-cell" : ""}">${money(r.after)}</td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>
    <p class="hint">Monthly charging assumed: ${kwh} kWh, all landing between 11pm and 7am. Tiered doesn't change — same total kWh.</p>`;
}

/* ------------------------------------------------------------- actions */

$("csv-btn").addEventListener("click", () => {
  window.open(`/api/export/${state.jobId}.csv?session_id=${state.sessionId}`);
});

$("restart-btn").addEventListener("click", () => {
  if (state.sessionId) {
    api(`/api/logout?session_id=${state.sessionId}`, { method: "POST" }).catch(() => {});
  }
  location.reload();
});
