const coverageStrip = document.getElementById("coverage-strip");
const refreshButton = document.getElementById("refresh-dashboard");
const pricingForm = document.getElementById("pricing-form");
const monthlyBars = document.getElementById("monthly-bars");
const machineMonthlyGrid = document.getElementById("machine-monthly-grid");
const monthDrawer = document.getElementById("month-drawer");
const monthDrawerBackdrop = document.getElementById("month-drawer-backdrop");
const monthDrawerClose = document.getElementById("month-drawer-close");
const monthDailyBars = document.getElementById("month-daily-bars");
const scrollTopButton = document.getElementById("scroll-top-button");
const tokenActivity = document.getElementById("token-activity");
const activitySummary = document.getElementById("activity-summary");
const activityScopeSelect = document.getElementById("activity-scope");
const activityModeButtons = Array.from(document.querySelectorAll("[data-activity-mode]"));
const refreshButtonDefaultLabel = refreshButton ? refreshButton.textContent : "Refresh";
let dashboardPayload = null;
let activeDrawerMonth = null;
let activeDrawerDay = null;
let activeDrawerMachineHost = null;
let activeDrawerTypeScope = "month";
let activeActivityMode = "daily";
let activeActivityScope = "fleet";
let scrollTopUpdatePending = false;

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "n/a";
  }
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function formatTokens(value) {
  return formatNumber(value, 0);
}

function formatPercent(value) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return `${formatNumber(value, 2)}%`;
}

function formatUsd(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "n/a";
  }
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(Number(value));
}

function formatCompactNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "n/a";
  }
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: Number(value) >= 1000000 ? 1 : 0,
  }).format(Number(value));
}

function setHtml(id, html) {
  document.getElementById(id).innerHTML = html;
}

function sleep(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderDataAttributes(attributes) {
  return Object.entries(attributes || {})
    .filter(([name, value]) => /^[a-zA-Z0-9_-]+$/.test(name) && value !== null && value !== undefined)
    .map(([name, value]) => `data-${name}="${escapeHtml(value)}"`)
    .join(" ");
}

function normaliseBarLabel(label, key) {
  let text = String(label ?? "");
  if (key === "day" && /^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return text.slice(5);
  }
  if (key === "month" && /^\d{4}-\d{2}$/.test(text)) {
    const [year, month] = text.split("-");
    return new Date(Number(year), Number(month) - 1, 1).toLocaleDateString(undefined, {
      month: "short",
      year: "2-digit",
    });
  }
  return text;
}

function formatMonthName(month) {
  if (!/^\d{4}-\d{2}$/.test(String(month))) {
    return String(month || "Month");
  }
  const [year, monthNumber] = String(month).split("-");
  return new Date(Number(year), Number(monthNumber) - 1, 1).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

function formatDayName(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(day))) {
    return String(day || "Day");
  }
  const [year, month, dayOfMonth] = String(day).split("-");
  return new Date(Number(year), Number(month) - 1, Number(dayOfMonth)).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function parseDateKey(day) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ""));
  if (!match) {
    return null;
  }
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function dateKey(date) {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}

function monthKeyFromDate(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function addDays(date, days) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days);
}

function startOfWeek(date) {
  return addDays(date, -date.getDay());
}

function endOfWeek(date) {
  return addDays(date, 6 - date.getDay());
}

function todayDate() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

function formatActivityMonth(date) {
  return date.toLocaleDateString(undefined, { month: "short" });
}

function activityModeLabel(mode = activeActivityMode) {
  if (mode === "weekly") {
    return "Weekly";
  }
  if (mode === "monthly") {
    return "Monthly";
  }
  if (mode === "cumulative") {
    return "Cumulative";
  }
  return "Daily";
}

function niceStep(rawValue) {
  if (!rawValue || rawValue <= 0) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(rawValue));
  const residual = rawValue / magnitude;
  if (residual <= 1) {
    return magnitude;
  }
  if (residual <= 2) {
    return 2 * magnitude;
  }
  if (residual <= 5) {
    return 5 * magnitude;
  }
  return 10 * magnitude;
}

function buildAxis(maxValue, tickCount = 5) {
  const safeMax = Math.max(Number(maxValue || 0), 0);
  if (safeMax === 0) {
    return { top: 1, ticks: [1, 0.75, 0.5, 0.25, 0] };
  }
  const step = niceStep(safeMax / Math.max(tickCount - 1, 1));
  const top = Math.max(step, Math.ceil(safeMax / step) * step);
  const ticks = [];
  for (let value = top; value >= 0; value -= step) {
    ticks.push(Math.max(0, value));
  }
  if (ticks[ticks.length - 1] !== 0) {
    ticks.push(0);
  }
  return { top, ticks };
}

function renderCoverage(payload) {
  const source = payload.source;
  const fleet = payload.fleet || {};
  const pills = [
    `Snapshot ${new Date(payload.generated_at).toLocaleString()}`,
    payload.coverage.db_name ? `DB ${payload.coverage.db_name}` : "No DB",
    `${formatTokens(fleet.combined_thread_count)} fleet threads`,
    `${payload.fleet?.reachable_remote_count ?? 0}/${payload.fleet?.configured_remote_count ?? 0} remotes reachable`,
    source.data_quality?.detailed_usage_available ? "Detailed response logs present" : "Thread totals only",
    source.first_seen_local && source.last_seen_local
      ? `${source.first_seen_local} -> ${source.last_seen_local}`
      : "No coverage window",
  ];
  coverageStrip.innerHTML = pills.map((text) => `<div class="pill">${escapeHtml(text)}</div>`).join("");
}

function renderMetricCards(payload) {
  const source = payload.source;
  const fleet = payload.fleet || {};
  const stats = source.stats || {};
  const cost = fleet.cost || {};
  const headlineCost =
    cost.api_equivalent_cost_total_usd ?? cost.rough_cost_total_usd ?? cost.usage_cost_total_usd ?? null;
  const items = [
    ["Fleet Tokens", formatTokens(fleet.combined_total_tokens)],
    ["Fleet 30 Days", formatTokens(fleet.combined_30d_tokens)],
    ["Fleet 7 Days", formatTokens(fleet.combined_7d_tokens)],
    ["Fleet Threads", formatTokens(fleet.combined_thread_count)],
    ["Local Tokens", formatTokens(source.total)],
    ["Remote Tokens", formatTokens((fleet.combined_total_tokens || 0) - (source.total || 0))],
    ["Local Avg / Thread", formatTokens(stats.avg_tokens_per_thread)],
    ["Local Median / Thread", formatTokens(stats.median_tokens_per_thread)],
    ["Local P90 / Thread", formatTokens(stats.p90_tokens_per_thread)],
    ["Local Largest Thread", formatTokens(stats.largest_thread_tokens)],
    ["Machines", formatTokens(fleet.machine_count)],
    ["Fleet API-Equivalent", headlineCost !== null && headlineCost !== undefined ? formatUsd(headlineCost) : "not set"],
  ];
  setHtml(
    "summary-metrics",
    items
      .map(
        ([label, value]) => `
          <div class="metric">
            <div class="metric-label">${label}</div>
            <div class="metric-value">${value}</div>
          </div>
        `
      )
      .join("")
  );
}

function renderFleetPanel(payload) {
  const fleet = payload.fleet || {};
  const cost = fleet.cost || {};
  const items = [
    ["Local Tokens", formatTokens(payload.source.total)],
    ["Local 30 Days", formatTokens(payload.source.last_30_days)],
    ["Local 7 Days", formatTokens(payload.source.last_7_days)],
    ["Machines", formatTokens(fleet.machine_count)],
    ["Reachable Remotes", formatTokens(fleet.reachable_remote_count)],
    [
      "Remote Tokens",
      formatTokens((fleet.combined_total_tokens || 0) - (payload.source.total || 0)),
    ],
    [
      "Remote 30 Days",
      formatTokens((fleet.combined_30d_tokens || 0) - (payload.source.last_30_days || 0)),
    ],
    [
      "Remote 7 Days",
      formatTokens((fleet.combined_7d_tokens || 0) - (payload.source.last_7_days || 0)),
    ],
    [
      "Fleet API-Equivalent",
      cost.api_equivalent_cost_total_usd !== null && cost.api_equivalent_cost_total_usd !== undefined
        ? formatUsd(cost.api_equivalent_cost_total_usd)
        : cost.rough_cost_total_usd !== null && cost.rough_cost_total_usd !== undefined
          ? formatUsd(cost.rough_cost_total_usd)
        : cost.usage_cost_total_usd !== null && cost.usage_cost_total_usd !== undefined
          ? formatUsd(cost.usage_cost_total_usd)
          : "n/a",
    ],
  ];
  setHtml(
    "fleet-metrics",
    items
      .map(
        ([label, value]) => `
          <div class="metric">
            <div class="metric-label">${label}</div>
            <div class="metric-value">${value}</div>
          </div>
        `
      )
      .join("")
  );
  const details = [
    ["Configured remotes", formatTokens(fleet.configured_remote_count)],
    ["Combined threads", formatTokens(fleet.combined_thread_count)],
    ["Local threads", formatTokens(payload.source.thread_count)],
    ["Remote threads", formatTokens((fleet.combined_thread_count || 0) - (payload.source.thread_count || 0))],
  ];
  setHtml(
    "fleet-details",
    details
      .map(
        ([label, value]) => `
          <div class="detail-box">
            <strong>${label}:</strong> ${value}
          </div>
        `
      )
      .join("")
  );
}

function renderAxisChart(rows, options) {
  const {
    labelKey,
    valueKey,
    emptyText,
    height = 188,
    axisTop,
    labelFormatter,
    metaFormatter,
    statusClass,
    tooltipFormatter,
    barDataAttrs,
    barActionLabel,
  } = options;
  if (!rows || rows.length === 0) {
    return `<div class="empty-state">${emptyText}</div>`;
  }
  const cleanRows = rows.map((row) => ({
    ...row,
    [valueKey]: Number(row[valueKey] || 0),
  }));
  const derivedMax = Math.max(...cleanRows.map((row) => row[valueKey]), 0);
  const axis = buildAxis(axisTop ?? derivedMax);
  return `
    <div class="chart-shell">
      <div class="chart-y-axis">
        ${axis.ticks
          .map(
            (tick) => `
              <div class="chart-tick-label">${formatCompactNumber(tick)}</div>
            `
          )
          .join("")}
      </div>
      <div class="chart-plot">
        <div class="chart-grid">
          ${axis.ticks
            .map(
              (tick) => `
                <div class="chart-grid-line${tick === 0 ? " is-zero" : ""}"></div>
              `
            )
            .join("")}
        </div>
        <div class="chart-bars">
          ${cleanRows
            .map((row) => {
              const value = row[valueKey];
              const ratio = axis.top > 0 ? value / axis.top : 0;
              const label = labelFormatter
                ? labelFormatter(row)
                : normaliseBarLabel(row[labelKey], labelKey);
              const meta = metaFormatter ? metaFormatter(row) : "";
              const toneClass = statusClass ? statusClass(row) : "";
              const tooltip = tooltipFormatter
                ? tooltipFormatter(row)
                : `${row[labelKey]}: ${formatTokens(value)}`;
              const actionLabel = barActionLabel ? barActionLabel(row) : "";
              const dataAttrs = barDataAttrs ? renderDataAttributes(barDataAttrs(row)) : "";
              const cardBody = `
                  <div class="chart-value">${formatCompactNumber(value)}</div>
                  <div class="chart-column" style="height:${height}px">
                    <div class="chart-bar ${toneClass}" style="height:${Math.max(ratio * 100, value > 0 ? 6 : 2)}%" title="${escapeHtml(tooltip)}"></div>
                  </div>
                  <div class="chart-label">${escapeHtml(label)}</div>
                  ${meta ? `<div class="chart-meta">${escapeHtml(meta)}</div>` : ""}
              `;
              if (actionLabel) {
                return `
                  <button class="chart-bar-card chart-bar-button" type="button" ${dataAttrs} aria-label="${escapeHtml(actionLabel)}">
                    ${cardBody}
                  </button>
                `;
              }
              return `
                <div class="chart-bar-card" ${dataAttrs}>
                  ${cardBody}
                </div>
              `;
            })
            .join("")}
        </div>
      </div>
    </div>
  `;
}

function renderMachineMonthlyGrid(payload) {
  const machines = payload.fleet?.machines || [];
  const chartRows = machines.map((row) => {
    const monthly = [...(row.monthly || [])]
      .sort((left, right) => String(left.month).localeCompare(String(right.month)))
      .slice(-8);
    return {
      label: row.label === "Local machine" ? "Local machine" : row.label,
      host: row.host,
      status: row.status || "configured",
      monthly,
      thread_count: row.thread_count || 0,
      total_tokens: row.total_tokens || 0,
      cache_notice: row.cache_notice,
      error: row.error,
    };
  });
  if (!chartRows.length) {
    setHtml("machine-monthly-grid", `<div class="empty-state">No machine monthly data.</div>`);
    return;
  }
  const axisTop = Math.max(
    ...chartRows.flatMap((machine) => machine.monthly.map((row) => Number(row.value || 0))),
    0
  );
  setHtml(
    "machine-monthly-grid",
    chartRows
      .map((machine) => {
        let statusText = machine.host === "local" ? "local DB" : "live over SSH";
        if (machine.status === "cached") {
          statusText = machine.cache_notice ? `cached snapshot · ${machine.cache_notice}` : "cached snapshot";
        } else if (machine.status === "error") {
          statusText = machine.error || "unreachable";
        }
        return `
          <section class="machine-monthly-card">
            <div class="machine-monthly-head">
              <div>
                <div class="machine-monthly-title">${escapeHtml(machine.label)}</div>
                <div class="machine-monthly-meta">${escapeHtml(statusText)}</div>
              </div>
              <div class="machine-monthly-stat">${formatCompactNumber(machine.total_tokens)} total</div>
            </div>
            ${renderAxisChart(machine.monthly, {
              labelKey: "month",
              valueKey: "value",
              emptyText: "No monthly history for this machine.",
              height: 156,
              axisTop,
              labelFormatter: (row) => normaliseBarLabel(row.month, "month"),
              statusClass: () => {
                if (machine.status === "cached") {
                  return "is-cached";
                }
                if (machine.status === "error") {
                  return "is-error";
                }
                return machine.host === "local" ? "is-local" : "is-live";
              },
              tooltipFormatter: (row) =>
                `${machine.label} · ${row.month}: ${formatTokens(row.value)} tokens`,
              barDataAttrs: (row) => ({
                "month-drawer-month": row.month,
                "month-drawer-machine": machine.host,
              }),
              barActionLabel: (row) =>
                `Open daily usage for ${machine.label} in ${formatMonthName(row.month)}`,
            })}
          </section>
        `;
      })
      .join("")
  );
}

function normaliseActivityRows(rows) {
  const byDay = new Map();
  for (const row of rows || []) {
    const parsed = parseDateKey(row.day);
    if (!parsed) {
      continue;
    }
    const day = dateKey(parsed);
    const bucket = byDay.get(day) || { day, value: 0, thread_count: 0 };
    bucket.value += Number(row.value || 0);
    bucket.thread_count += Number(row.thread_count || 0);
    byDay.set(day, bucket);
  }
  return [...byDay.values()].sort((left, right) => left.day.localeCompare(right.day));
}

function activityRowsForScope(payload) {
  if (activeActivityScope !== "fleet") {
    const machine = (payload.fleet?.machines || []).find((row) => row.host === activeActivityScope);
    if (machine) {
      return machine.daily || [];
    }
    activeActivityScope = "fleet";
  }
  return payload.fleet?.combined_daily || [];
}

function activityMonthlyRowsForScope(payload) {
  if (activeActivityScope !== "fleet") {
    const machine = (payload.fleet?.machines || []).find((row) => row.host === activeActivityScope);
    if (machine) {
      return machine.monthly || [];
    }
  }
  return payload.fleet?.combined_monthly || [];
}

function activityScopeLabel(payload) {
  if (activeActivityScope === "fleet") {
    return "Fleet";
  }
  const machine = (payload.fleet?.machines || []).find((row) => row.host === activeActivityScope);
  return machine ? machineDisplayName(machine) : "Fleet";
}

function renderActivityScopeOptions(payload) {
  if (!activityScopeSelect) {
    return;
  }
  const options = [
    { value: "fleet", label: "Fleet" },
    ...(payload.fleet?.machines || []).map((machine) => ({
      value: machine.host,
      label: machineDisplayName(machine),
    })),
  ];
  if (!options.some((option) => option.value === activeActivityScope)) {
    activeActivityScope = "fleet";
  }
  activityScopeSelect.innerHTML = options
    .map(
      (option) => `
        <option value="${escapeHtml(option.value)}"${option.value === activeActivityScope ? " selected" : ""}>
          ${escapeHtml(option.label)}
        </option>
      `
    )
    .join("");
}

function buildActivityTimeline(rows, mode) {
  const normalizedRows = normaliseActivityRows(rows);
  const today = todayDate();
  const latestRow = normalizedRows.length ? normalizedRows[normalizedRows.length - 1] : null;
  const latestDate = latestRow ? parseDateKey(latestRow.day) : null;
  const endDate = latestDate && latestDate > today ? latestDate : today;
  const startDate = addDays(endDate, -364);
  const startKey = dateKey(startDate);
  const endKey = dateKey(endDate);
  const gridStart = startOfWeek(startDate);
  const gridEnd = endOfWeek(endDate);
  const rawByDay = new Map(normalizedRows.map((row) => [row.day, row]));
  const valuesByDay = new Map();
  const rollingValues = [];
  let rollingTotal = 0;
  let cumulativeTotal = 0;

  for (let date = startDate; date <= endDate; date = addDays(date, 1)) {
    const day = dateKey(date);
    const raw = rawByDay.get(day) || { day, value: 0, thread_count: 0 };
    const rawValue = Number(raw.value || 0);
    rollingValues.push(rawValue);
    rollingTotal += rawValue;
    if (rollingValues.length > 7) {
      rollingTotal -= rollingValues.shift();
    }
    cumulativeTotal += rawValue;
    const weeklyValue = rollingTotal;
    const cumulativeValue = cumulativeTotal;
    let value = rawValue;
    if (mode === "weekly") {
      value = weeklyValue;
    } else if (mode === "cumulative") {
      value = cumulativeValue;
    }
    valuesByDay.set(day, {
      day,
      value,
      rawValue,
      weeklyValue,
      cumulativeValue,
      thread_count: Number(raw.thread_count || 0),
      inRange: true,
    });
  }

  const weeks = [];
  const cells = [];
  let weekIndex = 0;
  for (let weekStart = gridStart; weekStart <= gridEnd; weekStart = addDays(weekStart, 7)) {
    weekIndex += 1;
    weeks.push({ index: weekIndex, start: dateKey(weekStart) });
    for (let offset = 0; offset < 7; offset += 1) {
      const date = addDays(weekStart, offset);
      const day = dateKey(date);
      const inRange = day >= startKey && day <= endKey;
      const values = valuesByDay.get(day) || {
        day,
        value: 0,
        rawValue: 0,
        weeklyValue: 0,
        cumulativeValue: 0,
        thread_count: 0,
      };
      cells.push({
        ...values,
        day,
        date,
        inRange,
        weekIndex,
        dayOfWeek: offset,
        month: monthKeyFromDate(date),
      });
    }
  }

  return {
    normalizedRows,
    cells,
    values: [...valuesByDay.values()],
    weeks,
    startDate,
    endDate,
    startKey,
    endKey,
  };
}

function buildActivityMonthLabels(timeline) {
  const labels = [];
  const seen = new Set();
  for (const week of timeline.weeks) {
    const weekStart = parseDateKey(week.start);
    let label = "";
    for (let offset = 0; offset < 7; offset += 1) {
      const date = addDays(weekStart, offset);
      const day = dateKey(date);
      if (day < timeline.startKey || day > timeline.endKey) {
        continue;
      }
      const month = monthKeyFromDate(date);
      if (!seen.has(month)) {
        seen.add(month);
        label = formatActivityMonth(date);
        break;
      }
    }
    labels.push({ index: week.index, label });
  }
  return labels;
}

function activityLevel(value, maxValue) {
  if (!value || value <= 0 || !maxValue || maxValue <= 0) {
    return 0;
  }
  return Math.max(1, Math.ceil(Math.min(value / maxValue, 1) * 5));
}

function activityCellTitle(cell, mode) {
  const dayTokens = `${formatTokens(cell.rawValue)} tokens`;
  const threads = `${formatTokens(cell.thread_count)} threads`;
  if (mode === "weekly") {
    return `${formatDayName(cell.day)} · ${formatTokens(cell.weeklyValue)} tokens over 7 days · ${dayTokens} on day`;
  }
  if (mode === "cumulative") {
    return `${formatDayName(cell.day)} · ${formatTokens(cell.cumulativeValue)} cumulative tokens · ${dayTokens} on day`;
  }
  return `${formatDayName(cell.day)} · ${dayTokens} · ${threads}`;
}

function renderActivitySummary(payload, timeline, scopeLabel) {
  if (!activitySummary) {
    return;
  }
  const activeDays = timeline.values.filter(
    (row) => Number(row.rawValue || 0) > 0 || Number(row.thread_count || 0) > 0
  );
  const rawTotal = sumRows(timeline.values, "rawValue");
  const peak = [...timeline.values].sort((left, right) => Number(right.value || 0) - Number(left.value || 0))[0];
  const peakLabel = activeActivityMode === "weekly" ? "Peak Week" : activeActivityMode === "cumulative" ? "Range Total" : "Peak Day";
  const peakValue = peak ? `${formatCompactNumber(peak.value)} · ${peak.day.slice(5)}` : "n/a";
  const items = [
    ["Scope", scopeLabel],
    ["Mode", activityModeLabel()],
    ["Raw Tokens", formatTokens(rawTotal)],
    ["Active Days", formatTokens(activeDays.length)],
    [peakLabel, peakValue],
  ];
  activitySummary.innerHTML = items
    .map(
      ([label, value]) => `
        <div class="activity-stat">
          <div class="metric-label">${escapeHtml(label)}</div>
          <div class="activity-stat-value">${escapeHtml(value)}</div>
        </div>
      `
    )
    .join("");
}

function updateActivityModeButtons() {
  for (const button of activityModeButtons) {
    const isActive = button.dataset.activityMode === activeActivityMode;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-selected", isActive ? "true" : "false");
  }
}

function buildMonthlyActivityRows(rows) {
  const byMonth = new Map(
    (rows || []).map((row) => [
      String(row.month || ""),
      {
        ...row,
        month: String(row.month || ""),
        value: Number(row.value || 0),
        thread_count: Number(row.thread_count || 0),
      },
    ])
  );
  const currentMonth = monthKeyFromDate(todayDate());
  const validMonths = [...byMonth.keys()].filter((month) => /^\d{4}-\d{2}$/.test(month)).sort();
  const latestMonth = validMonths[validMonths.length - 1];
  const endMonth = latestMonth && latestMonth > currentMonth ? latestMonth : currentMonth;
  const [endYear, endMonthNumber] = endMonth.split("-").map(Number);

  return Array.from({ length: 12 }, (_, index) => {
    const date = new Date(endYear, endMonthNumber - 12 + index, 1);
    const month = monthKeyFromDate(date);
    return byMonth.get(month) || { month, value: 0, thread_count: 0 };
  });
}

function renderMonthlyTokenActivity(payload, scopeLabel) {
  const rows = buildMonthlyActivityRows(activityMonthlyRowsForScope(payload));
  const maxValue = Math.max(...rows.map((row) => row.value), 0);
  const activeMonths = rows.filter((row) => row.value > 0 || row.thread_count > 0);
  const rangeTotal = sumRows(rows);
  const peak = [...rows].sort((left, right) => right.value - left.value)[0];
  const summaryItems = [
    ["Scope", scopeLabel],
    ["Mode", activityModeLabel()],
    ["Range Tokens", formatTokens(rangeTotal)],
    ["Active Months", formatTokens(activeMonths.length)],
    ["Peak Month", peak ? `${formatCompactNumber(peak.value)} · ${normaliseBarLabel(peak.month, "month")}` : "n/a"],
  ];
  activitySummary.innerHTML = summaryItems
    .map(
      ([label, value]) => `
        <div class="activity-stat">
          <div class="metric-label">${escapeHtml(label)}</div>
          <div class="activity-stat-value">${escapeHtml(value)}</div>
        </div>
      `
    )
    .join("");

  tokenActivity.innerHTML = `
    <div class="monthly-activity-grid" role="grid" aria-label="${escapeHtml(`${scopeLabel} monthly token activity for the last twelve months`)}">
      ${rows
        .map((row) => {
          const title = `${formatMonthName(row.month)} · ${formatTokens(row.value)} tokens · ${formatTokens(row.thread_count)} threads`;
          return `
            <button
              class="monthly-activity-cell activity-level-${activityLevel(row.value, maxValue)}"
              type="button"
              ${renderDataAttributes({
                "activity-month": row.month,
                "activity-machine": activeActivityScope === "fleet" ? "" : activeActivityScope,
              })}
              aria-label="${escapeHtml(title)}"
              title="${escapeHtml(title)}"
            >
              <span class="monthly-activity-label">${escapeHtml(normaliseBarLabel(row.month, "month"))}</span>
              <strong>${escapeHtml(formatCompactNumber(row.value))}</strong>
              <span>${escapeHtml(`${formatTokens(row.thread_count)} threads`)}</span>
            </button>
          `;
        })
        .join("")}
    </div>
  `;
}

function renderTokenActivity(payload) {
  if (!tokenActivity) {
    return;
  }
  renderActivityScopeOptions(payload);
  updateActivityModeButtons();
  const scopeLabel = activityScopeLabel(payload);
  if (activeActivityMode === "monthly") {
    renderMonthlyTokenActivity(payload, scopeLabel);
    return;
  }
  const timeline = buildActivityTimeline(activityRowsForScope(payload), activeActivityMode);
  const maxValue = Math.max(...timeline.values.map((row) => Number(row.value || 0)), 0);
  const monthLabels = buildActivityMonthLabels(timeline);
  renderActivitySummary(payload, timeline, scopeLabel);

  tokenActivity.innerHTML = `
    <div class="activity-map-scroll">
      <div class="activity-map-inner" style="--activity-columns: ${timeline.weeks.length}">
        <div class="activity-weekdays" aria-hidden="true">
          <span></span>
          <span>Mon</span>
          <span></span>
          <span>Wed</span>
          <span></span>
          <span>Fri</span>
          <span></span>
        </div>
        <div
          class="activity-grid"
          role="grid"
          aria-label="${escapeHtml(`${scopeLabel} ${activityModeLabel().toLowerCase()} token activity from ${formatDayName(dateKey(timeline.startDate))} to ${formatDayName(dateKey(timeline.endDate))}`)}"
        >
          ${timeline.cells
            .map((cell) => {
              const level = activityLevel(cell.value, maxValue);
              const title = cell.inRange ? activityCellTitle(cell, activeActivityMode) : "";
              const dataAttrs = cell.inRange
                ? renderDataAttributes({
                    "activity-day": cell.day,
                    "activity-machine": activeActivityScope === "fleet" ? "" : activeActivityScope,
                  })
                : "";
              return `
                <button
                  class="activity-cell activity-level-${level}${cell.inRange ? "" : " is-outside-range"}"
                  type="button"
                  style="grid-column: ${cell.weekIndex}; grid-row: ${cell.dayOfWeek + 1}"
                  ${dataAttrs}
                  ${cell.inRange ? "" : "disabled"}
                  aria-label="${escapeHtml(title || "Outside visible range")}"
                  title="${escapeHtml(title)}"
                >
                  <span class="sr-only">${escapeHtml(title)}</span>
                </button>
              `;
            })
            .join("")}
        </div>
        <div class="activity-months" aria-hidden="true">
          ${monthLabels
            .map(
              (item) => `
                <span style="grid-column: ${item.index}">${escapeHtml(item.label)}</span>
              `
            )
            .join("")}
        </div>
      </div>
    </div>
  `;
}

function sumRows(rows, key = "value") {
  return (rows || []).reduce((total, row) => total + Number(row[key] || 0), 0);
}

function dailyRowsForMonth(rows, month) {
  return [...(rows || [])]
    .filter((row) => String(row.day || "").startsWith(`${month}-`))
    .sort((left, right) => String(left.day).localeCompare(String(right.day)));
}

function filledDailyRowsForMonth(rows, month) {
  if (!/^\d{4}-\d{2}$/.test(String(month))) {
    return dailyRowsForMonth(rows, month);
  }
  const [year, monthNumber] = String(month).split("-").map((part) => Number(part));
  const daysInMonth = new Date(year, monthNumber, 0).getDate();
  const byDay = new Map(dailyRowsForMonth(rows, month).map((row) => [row.day, row]));
  return Array.from({ length: daysInMonth }, (_, index) => {
    const day = `${month}-${String(index + 1).padStart(2, "0")}`;
    const existing = byDay.get(day);
    return {
      ...(existing || {}),
      day,
      value: existing ? Number(existing.value || 0) : 0,
      thread_count: existing ? Number(existing.thread_count || 0) : 0,
    };
  });
}

function defaultDayForMonth(rows) {
  const activeRows = (rows || []).filter(
    (row) => Number(row.value || 0) > 0 || Number(row.thread_count || 0) > 0
  );
  const candidates = activeRows.length ? activeRows : rows || [];
  if (!candidates.length) {
    return null;
  }
  return [...candidates].sort((left, right) => {
    const valueDelta = Number(right.value || 0) - Number(left.value || 0);
    if (valueDelta !== 0) {
      return valueDelta;
    }
    return String(right.day).localeCompare(String(left.day));
  })[0].day;
}

function machineStatusText(machine) {
  if (machine.host === "local") {
    return "local DB";
  }
  if (machine.status === "cached") {
    return "cached snapshot";
  }
  if (machine.status === "error") {
    return "unreachable";
  }
  return "live over SSH";
}

function findMachineByHost(host) {
  if (!host) {
    return null;
  }
  return (dashboardPayload?.fleet?.machines || []).find((machine) => machine.host === host) || null;
}

function machineDisplayName(machine) {
  if (!machine) {
    return "Fleet";
  }
  if (!machine.host || machine.host === "local") {
    return machine.label;
  }
  return `${machine.label} (${machine.host})`;
}

function tokenUsageForRow(row) {
  const total = Number(row?.total_tokens ?? row?.value ?? 0);
  const input = Number(row?.input_tokens || 0);
  const cached = Math.min(Number(row?.cached_input_tokens || 0), input);
  const output = Number(row?.output_tokens || 0);
  const reasoning = Math.min(Number(row?.reasoning_output_tokens || 0), output);
  return {
    total,
    input,
    cached,
    uncached: Number(row?.uncached_input_tokens ?? Math.max(input - cached, 0)),
    output,
    reasoning,
    nonReasoning: Number(row?.non_reasoning_output_tokens ?? Math.max(output - reasoning, 0)),
    unclassified: Number(row?.unclassified_tokens ?? Math.max(total - input - output, 0)),
    threadCount: Number(row?.thread_count || 0),
    typedThreadCount: Number(row?.typed_thread_count || 0),
    coverage: Number(row?.detail_coverage_pct || 0),
  };
}

function renderTokenTypeDetail(row) {
  const usage = tokenUsageForRow(row);
  const rows = [
    { label: "Input", value: usage.input, tone: "input" },
    { label: "Cached input", value: usage.cached, parent: usage.input, child: true, tone: "cached" },
    { label: "Uncached input", value: usage.uncached, parent: usage.input, child: true, tone: "uncached" },
    { label: "Output", value: usage.output, tone: "output" },
    { label: "Reasoning output", value: usage.reasoning, parent: usage.output, child: true, tone: "reasoning" },
    {
      label: "Non-reasoning output",
      value: usage.nonReasoning,
      parent: usage.output,
      child: true,
      tone: "non-reasoning",
    },
  ];
  if (usage.unclassified > 0) {
    rows.push({ label: "Unclassified", value: usage.unclassified, tone: "unclassified" });
  }
  const detailRows = rows
    .map((item) => {
      const totalShare = usage.total ? (item.value * 100) / usage.total : 0;
      const parentShare = item.parent ? (item.value * 100) / item.parent : null;
      return `
        <div class="token-type-row${item.child ? " is-child" : ""}">
          <div class="token-type-label">${escapeHtml(item.label)}</div>
          <div class="token-type-track" aria-hidden="true">
            <div class="token-type-fill is-${item.tone}" style="width:${Math.max(totalShare, item.value > 0 ? 1 : 0)}%"></div>
          </div>
          <div class="token-type-value">
            <strong>${formatTokens(item.value)}</strong>
            <span>${formatPercent(totalShare)} total${parentShare === null ? "" : ` · ${formatPercent(parentShare)} parent`}</span>
          </div>
        </div>
      `;
    })
    .join("");
  return `
    <div class="token-type-total">
      <span>Total</span>
      <strong>${formatTokens(usage.total)}</strong>
    </div>
    ${detailRows}
  `;
}

function renderMonthDrawer(month) {
  if (!dashboardPayload || !monthDrawer) {
    return;
  }

  const scopedMachine = findMachineByHost(activeDrawerMachineHost);
  const scopeLabel = machineDisplayName(scopedMachine);
  const monthlyRows = scopedMachine ? scopedMachine.monthly || [] : dashboardPayload.fleet?.combined_monthly || [];
  const dailyRows = scopedMachine ? scopedMachine.daily || [] : dashboardPayload.fleet?.combined_daily || [];
  const monthlyRow = monthlyRows.find((row) => row.month === month);
  const monthDailyRows = filledDailyRowsForMonth(dailyRows, month);
  const monthlyTotal = Number(monthlyRow?.value ?? sumRows(monthDailyRows));
  const dailyTotal = sumRows(monthDailyRows);
  const activeDays = monthDailyRows.filter((row) => Number(row.value || 0) > 0 || Number(row.thread_count || 0) > 0).length;
  const dailyDetailIsComplete = monthlyTotal === dailyTotal;
  if (!activeDrawerDay || !String(activeDrawerDay).startsWith(`${month}-`)) {
    activeDrawerDay = defaultDayForMonth(monthDailyRows);
  }
  const selectedDayRow = monthDailyRows.find((row) => row.day === activeDrawerDay) || {
    day: activeDrawerDay,
    value: 0,
    thread_count: 0,
  };
  const selectedDayTokens = Number(selectedDayRow.value || 0);
  const selectedDayThreads = Number(selectedDayRow.thread_count || 0);
  const selectedDayShare = monthlyTotal ? (selectedDayTokens * 100.0) / monthlyTotal : null;
  const drawerMachines = scopedMachine ? [scopedMachine] : dashboardPayload.fleet?.machines || [];
  const selectedMachineRows = drawerMachines
    .map((machine) => {
      const machineDayRow = (machine.daily || []).find((row) => row.day === activeDrawerDay);
      const tokens = Number(machineDayRow?.value || 0);
      const threadCount = Number(machineDayRow?.thread_count || 0);
      const hasDayData = scopedMachine || tokens > 0 || threadCount > 0;
      return {
        title: machineDisplayName(machine),
        value: formatTokens(tokens),
        sortValue: tokens,
        meta: [
          `${formatTokens(threadCount)} threads`,
          machineStatusText(machine),
          selectedDayTokens && !scopedMachine ? `${formatPercent((tokens * 100.0) / selectedDayTokens)} of day` : "",
          scopedMachine && selectedDayShare !== null ? `${formatPercent(selectedDayShare)} of machine month` : "",
        ]
          .filter(Boolean)
          .join(" · "),
        hasDayData,
      };
    })
    .filter((row) => row.hasDayData)
    .sort((left, right) => right.sortValue - left.sortValue);
  const metaParts = [
    scopeLabel,
    activeDrawerDay ? formatDayName(activeDrawerDay) : formatMonthName(month),
    `${formatTokens(monthlyTotal)} tokens in ${formatMonthName(month)}`,
    `${formatTokens(activeDays)} active days`,
  ];
  if (!dailyDetailIsComplete) {
    metaParts.push(`${formatTokens(dailyTotal)} tokens have daily detail`);
  }

  document.getElementById("month-drawer-title").textContent =
    scopedMachine ? `${scopedMachine.label} · ${formatMonthName(month)}` : formatMonthName(month);
  document.getElementById("month-drawer-meta").textContent = metaParts.join(" · ");
  document.getElementById("month-machine-list-title").textContent = scopedMachine ? "Machine" : "Machines";
  setHtml(
    "month-drawer-metrics",
    (scopedMachine
      ? [
          ["Day Tokens", formatTokens(selectedDayTokens)],
          ["Day Threads", formatTokens(selectedDayThreads)],
          ["Share of Machine Month", formatPercent(selectedDayShare)],
          ["Machine Month", formatTokens(monthlyTotal)],
        ]
      : [
          ["Day Tokens", formatTokens(selectedDayTokens)],
          ["Day Threads", formatTokens(selectedDayThreads)],
          ["Share of Month", formatPercent(selectedDayShare)],
          ["Active Machines", formatTokens(selectedMachineRows.length)],
        ])
      .map(
        ([label, value]) => `
          <div class="metric">
            <div class="metric-label">${label}</div>
            <div class="metric-value">${value}</div>
          </div>
        `
      )
      .join("")
  );

  const typeDetailRow =
    activeDrawerTypeScope === "day"
      ? selectedDayRow
      : monthlyRow || { value: monthlyTotal, thread_count: monthDailyRows.reduce((sum, row) => sum + Number(row.thread_count || 0), 0) };
  const typeUsage = tokenUsageForRow(typeDetailRow);
  const typePeriodLabel =
    activeDrawerTypeScope === "day" && activeDrawerDay
      ? formatDayName(activeDrawerDay)
      : formatMonthName(month);
  setHtml("month-token-types", renderTokenTypeDetail(typeDetailRow));
  document.getElementById("token-type-detail-meta").textContent = [
    typePeriodLabel,
    `${formatTokens(typeUsage.typedThreadCount)}/${formatTokens(typeUsage.threadCount)} threads with typed detail`,
    `${formatPercent(typeUsage.coverage)} coverage`,
  ].join(" · ");
  monthDrawer.querySelectorAll("[data-token-type-scope]").forEach((button) => {
    const selected = button.dataset.tokenTypeScope === activeDrawerTypeScope;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", selected ? "true" : "false");
  });

  document.getElementById("month-daily-bars").innerHTML = renderAxisChart(monthDailyRows, {
    labelKey: "day",
    valueKey: "value",
    emptyText: "No daily data for this month.",
    height: 240,
    labelFormatter: (row) => row.day.slice(-2),
    metaFormatter: (row) => (row.thread_count ? `${formatTokens(row.thread_count)} th` : ""),
    statusClass: (row) => (row.day === activeDrawerDay ? "is-selected" : ""),
    tooltipFormatter: (row) =>
      `${row.day}: ${formatTokens(row.value)} tokens · ${formatTokens(row.thread_count)} threads`,
    barDataAttrs: (row) => ({ "month-drawer-day": row.day }),
    barActionLabel: (row) => `Show stats for ${formatDayName(row.day)}`,
  });

  setHtml(
    "month-machine-list",
    renderRowList(selectedMachineRows, {
      emptyText: "No machine data for this day.",
      title: (row) => row.title,
      value: (row) => row.value,
      meta: (row) => row.meta,
    })
  );
}

function openMonthDrawer(month, machineHost = null, day = null) {
  const scopedMachineHost = machineHost || null;
  if (activeDrawerMonth !== month || activeDrawerMachineHost !== scopedMachineHost) {
    activeDrawerDay = null;
  }
  if (day) {
    activeDrawerDay = day;
  }
  activeDrawerTypeScope = day ? "day" : "month";
  activeDrawerMonth = month;
  activeDrawerMachineHost = scopedMachineHost;
  renderMonthDrawer(month);
  monthDrawer.hidden = false;
  monthDrawerBackdrop.hidden = false;
  monthDrawer.setAttribute("aria-hidden", "false");
  window.requestAnimationFrame(() => {
    monthDrawer.classList.add("is-open");
    monthDrawerBackdrop.classList.add("is-open");
  });
}

function closeMonthDrawer() {
  activeDrawerMonth = null;
  activeDrawerDay = null;
  activeDrawerMachineHost = null;
  activeDrawerTypeScope = "month";
  monthDrawer.classList.remove("is-open");
  monthDrawerBackdrop.classList.remove("is-open");
  monthDrawer.setAttribute("aria-hidden", "true");
  window.setTimeout(() => {
    if (!activeDrawerMonth) {
      monthDrawer.hidden = true;
      monthDrawerBackdrop.hidden = true;
    }
  }, 180);
}

function updateScrollTopButton() {
  if (!scrollTopButton) {
    return;
  }
  const shouldShow = window.scrollY > 360;
  scrollTopButton.classList.toggle("is-visible", shouldShow);
  if (shouldShow) {
    scrollTopButton.removeAttribute("aria-hidden");
    scrollTopButton.tabIndex = 0;
  } else {
    scrollTopButton.setAttribute("aria-hidden", "true");
    scrollTopButton.tabIndex = -1;
  }
}

function requestScrollTopButtonUpdate() {
  if (scrollTopUpdatePending) {
    return;
  }
  scrollTopUpdatePending = true;
  window.requestAnimationFrame(() => {
    scrollTopUpdatePending = false;
    updateScrollTopButton();
  });
}

function renderHighlights(source) {
  const highlights = source.highlights || [];
  setHtml(
    "highlights",
    highlights.length
      ? highlights.map((item) => `<div class="callout-item">${escapeHtml(item)}</div>`).join("")
      : `<div class="empty-state">No highlights available.</div>`
  );
}

function renderSummaryDetails(source) {
  const details = [
    ["Data source", source.db_path],
    ["Coverage", `${source.first_seen_local || "n/a"} -> ${source.last_seen_local || "n/a"}`],
    ["Logs", source.data_quality ? `${formatTokens(source.data_quality.log_rows)} rows` : "n/a"],
    [
      "Detailed usage events",
      source.data_quality ? formatTokens(source.data_quality.response_events) : "n/a",
    ],
    ["Notes", source.notes],
  ];
  setHtml(
    "summary-details",
    details
      .map(
        ([label, value]) => `
          <div class="detail-box">
            <strong>${label}:</strong> ${value}
          </div>
        `
      )
      .join("")
  );
}

function renderRowList(rows, options = {}) {
  if (!rows || rows.length === 0) {
    return `<div class="empty-state">${options.emptyText || "No data available."}</div>`;
  }
  return rows
    .map((row) => {
      const title = options.title(row);
      const value = options.value ? options.value(row) : "";
      const meta = options.meta ? options.meta(row) : "";
      return `
        <div class="row-item">
          <div class="row-main">
            <div class="row-title">${escapeHtml(title)}</div>
            ${meta ? `<div class="row-meta">${escapeHtml(meta)}</div>` : ""}
          </div>
          ${value ? `<div class="row-value">${escapeHtml(value)}</div>` : ""}
        </div>
      `;
    })
    .join("");
}

function renderBreakdowns(payload) {
  const source = payload.source;
  const fleet = payload.fleet || {};
  setHtml(
    "workspace-list",
    renderRowList(fleet.workspaces, {
      emptyText: "No workspace data.",
      title: (row) => row.label,
      value: (row) => formatTokens(row.total_tokens),
      meta: (row) => `${row.thread_count} threads · avg ${formatTokens(row.avg_tokens)} · ${formatPercent(row.share_pct)}`,
    })
  );

  setHtml(
    "model-list",
    renderRowList(fleet.models, {
      emptyText: "No model data.",
      title: (row) => row.label,
      value: (row) => formatTokens(row.total_tokens),
      meta: (row) => `${row.thread_count} threads · avg ${formatTokens(row.avg_tokens)} · ${formatPercent(row.share_pct)}`,
    })
  );

  setHtml(
    "effort-list",
    renderRowList(source.effort_breakdown, {
      emptyText: "No reasoning-effort data.",
      title: (row) => row.reasoning_effort,
      value: (row) => formatTokens(row.total_tokens),
      meta: (row) => `${row.thread_count} threads · ${formatPercent(row.share_pct)}`,
    })
  );

  setHtml(
    "entry-list",
    renderRowList(source.source_breakdown, {
      emptyText: "No source breakdown available.",
      title: (row) => row.label,
      value: (row) => formatTokens(row.total_tokens),
      meta: (row) => `${row.thread_count} threads · avg ${formatTokens(row.avg_tokens)} · ${formatPercent(row.share_pct)}`,
    })
  );

  setHtml(
    "top-thread-list",
    renderRowList(source.top_threads, {
      emptyText: "No heavy threads recorded.",
      title: (row) => row.title_short,
      value: (row) => formatTokens(row.tokens_used),
      meta: (row) =>
        `${row.cwd_short} · ${row.model} · ${row.reasoning_effort} · ${formatPercent(row.share_pct)} · ${row.updated_local}`,
    })
  );

  setHtml(
    "recent-thread-list",
    renderRowList(source.recent_threads, {
      emptyText: "No recent threads recorded.",
      title: (row) => row.title_short,
      value: (row) => formatTokens(row.tokens_used),
      meta: (row) => `${row.cwd_short} · ${row.model} · ${row.updated_local}`,
    })
  );

  const environmentRows = [
    ...(source.approval_breakdown || []).map((row) => ({
      title: `Approval: ${row.approval_mode}`,
      value: formatTokens(row.total_tokens),
      meta: `${row.thread_count} threads · ${formatPercent(row.share_pct)}`,
    })),
    ...(source.sandbox_breakdown || []).map((row) => ({
      title: `Sandbox: ${row.label}`,
      value: formatTokens(row.total_tokens),
      meta: `${row.thread_count} threads · ${formatPercent(row.share_pct)}`,
    })),
  ];
  setHtml(
    "environment-list",
    renderRowList(environmentRows, {
      emptyText: "No environment data.",
      title: (row) => row.title,
      value: (row) => row.value,
      meta: (row) => row.meta,
    })
  );
}

function renderRemoteMachines(payload) {
  const rows = (payload.fleet?.machines || []).map((row) => {
    const dedupeNote = row.deduplicated_subagent_count
      ? ` · ${formatTokens(row.deduplicated_subagent_count)} repeated subagent counters excluded`
      : "";
    if (row.status === "error") {
      return {
        title: `${row.label}${row.host && row.host !== "local" ? ` (${row.host})` : ""}`,
        value: "unreachable",
        meta: row.error || row.status,
      };
    }
    if (row.status === "cached") {
      return {
        title: `${row.label} (${row.host})`,
        value: formatTokens(row.total_tokens),
        meta: `cached snapshot · ${formatTokens(row.thread_count)} threads · 30d ${formatTokens(row.tokens_30d)} · 7d ${formatTokens(row.tokens_7d)}${dedupeNote}${row.cache_notice ? ` · ${row.cache_notice}` : ""}`,
      };
    }
    return {
      title: `${row.label}${row.host && row.host !== "local" ? ` (${row.host})` : ""}`,
      value: formatTokens(row.total_tokens),
      meta: `${formatTokens(row.thread_count)} threads · 30d ${formatTokens(row.tokens_30d)} · 7d ${formatTokens(row.tokens_7d)}${dedupeNote}`,
    };
  });
  setHtml(
    "remote-machine-list",
    renderRowList(rows, {
      emptyText: "No machines configured.",
      title: (row) => row.title,
      value: (row) => row.value,
      meta: (row) => row.meta,
    })
  );
}

function renderCostPanel(payload) {
  const pricing = payload.fleet?.pricing || payload.source?.pricing || {};
  const cost = payload.fleet?.cost || payload.source?.cost || {};
  let details;
  if (cost.kind === "official_api_equivalent") {
    details = [
      ["Pricing label", pricing.label || "not set"],
      ["API-equivalent total", cost.api_equivalent_cost_total_usd !== null && cost.api_equivalent_cost_total_usd !== undefined ? formatUsd(cost.api_equivalent_cost_total_usd) : "not set"],
      ["Latest month", cost.latest_month_api_equivalent_cost_usd !== null && cost.latest_month_api_equivalent_cost_usd !== undefined ? formatUsd(cost.latest_month_api_equivalent_cost_usd) : "not set"],
      ["Pricing coverage", formatPercent(cost.pricing_coverage_pct)],
      ["Unpriced tokens", formatTokens(cost.unpriced_tokens)],
      ["Cached-input cost", formatUsd(cost.cached_input_cost_usd)],
      ["Uncached-input cost", formatUsd(cost.uncached_input_cost_usd)],
      ["Output cost", formatUsd(cost.output_cost_usd)],
      ["Effective rate / 1M", cost.effective_rate_per_million !== null && cost.effective_rate_per_million !== undefined ? formatUsd(cost.effective_rate_per_million) : "n/a"],
      ["Rates updated", cost.rates_updated_at || "not set"],
      ["Meaning", "API-equivalent estimate, not your Codex subscription invoice"],
      ["Method", cost.notes || "Measured token types priced by public model"],
    ];
  } else if (cost.kind === "official_gpt54_rough") {
    details = [
      ["Pricing label", pricing.label || "not set"],
      ["Rough total", cost.rough_cost_total_usd !== null && cost.rough_cost_total_usd !== undefined ? formatUsd(cost.rough_cost_total_usd) : "not set"],
      ["Input-only floor", cost.input_only_cost_total_usd !== null && cost.input_only_cost_total_usd !== undefined ? formatUsd(cost.input_only_cost_total_usd) : "not set"],
      ["Output-only ceiling", cost.output_only_cost_total_usd !== null && cost.output_only_cost_total_usd !== undefined ? formatUsd(cost.output_only_cost_total_usd) : "not set"],
      ["Cached-input floor", cost.cached_input_cost_total_usd !== null && cost.cached_input_cost_total_usd !== undefined ? formatUsd(cost.cached_input_cost_total_usd) : "not set"],
      ["Latest month rough", cost.latest_month_rough_cost_usd !== null && cost.latest_month_rough_cost_usd !== undefined ? formatUsd(cost.latest_month_rough_cost_usd) : "not set"],
      ["Assumed rate / 1M", cost.assumed_rate_per_million !== null && cost.assumed_rate_per_million !== undefined ? formatUsd(cost.assumed_rate_per_million) : "not set"],
      ["Method", cost.notes || "Official pricing-based rough estimate"],
    ];
  } else {
    details = [
      ["Pricing label", pricing.label || "not set"],
      ["Usage estimate", cost.usage_cost_total_usd !== null && cost.usage_cost_total_usd !== undefined ? formatUsd(cost.usage_cost_total_usd) : "not set"],
      ["Latest month estimate", cost.latest_month_cost_usd !== null && cost.latest_month_cost_usd !== undefined ? formatUsd(cost.latest_month_cost_usd) : "not set"],
      ["Flat monthly", cost.monthly_flat_usd !== null && cost.monthly_flat_usd !== undefined ? formatUsd(cost.monthly_flat_usd) : "not set"],
    ];
  }
  setHtml(
    "cost-overview",
    details
      .map(
        ([label, value]) => `
          <div class="detail-box">
            <strong>${label}:</strong> ${value}
          </div>
        `
      )
      .join("")
  );

  if (pricing.kind === "custom_block") {
    pricingForm.elements.pricingLabel.value = pricing.label || "";
    pricingForm.elements.usdPerBlock.value = pricing.usd_per_block ?? "";
    pricingForm.elements.unitsPerBlock.value = pricing.units_per_block ?? "";
    pricingForm.elements.monthlyFlatUsd.value = pricing.monthly_flat_usd ?? "";
  } else {
    pricingForm.reset();
  }
}

function renderDashboard(payload) {
  dashboardPayload = payload;
  const source = payload.source;
  const monthlyRows = [...(payload.fleet?.combined_monthly || [])]
    .sort((left, right) => String(left.month).localeCompare(String(right.month)))
    .slice(-8);
  const machineRows = (payload.fleet?.machines || []).map((row) => ({
    label: row.label === "Local machine" ? "Local" : row.label,
    host: row.host,
    status: row.status || "live",
    value: row.total_tokens || 0,
    thread_count: row.thread_count || 0,
    cache_notice: row.cache_notice,
  }));
  renderCoverage(payload);
  renderMetricCards(payload);
  renderFleetPanel(payload);
  renderTokenActivity(payload);
  monthlyBars.innerHTML = renderAxisChart(monthlyRows, {
    labelKey: "month",
    valueKey: "value",
    emptyText: "No monthly data.",
    labelFormatter: (row) => normaliseBarLabel(row.month, "month"),
    tooltipFormatter: (row) => `${row.month}: ${formatTokens(row.value)} tokens`,
    barDataAttrs: (row) => ({ "month-drawer-month": row.month }),
    barActionLabel: (row) => `Open daily usage for ${formatMonthName(row.month)}`,
  });
  document.getElementById("machine-bars").innerHTML = renderAxisChart(machineRows, {
    labelKey: "label",
    valueKey: "value",
    emptyText: "No machine activity.",
    labelFormatter: (row) => row.label,
    metaFormatter: (row) => {
      if (row.status === "cached") {
        return "cached snapshot";
      }
      if (row.status === "error") {
        return "unreachable";
      }
      return row.host === "local" ? "local DB" : "live over SSH";
    },
    statusClass: (row) => {
      if (row.status === "cached") {
        return "is-cached";
      }
      if (row.status === "error") {
        return "is-error";
      }
      return row.host === "local" ? "is-local" : "is-live";
    },
    tooltipFormatter: (row) => {
      const parts = [`${row.label}: ${formatTokens(row.value)} tokens`];
      if (row.thread_count) {
        parts.push(`${formatTokens(row.thread_count)} threads`);
      }
      if (row.status === "cached" && row.cache_notice) {
        parts.push(row.cache_notice);
      } else if (row.status === "cached") {
        parts.push("cached snapshot");
      } else if (row.status === "error") {
        parts.push("unreachable");
      } else if (row.host !== "local") {
        parts.push("live over SSH");
      }
      return parts.join(" · ");
    },
  });
  renderHighlights(source);
  renderSummaryDetails(source);
  renderBreakdowns(payload);
  renderRemoteMachines(payload);
  renderMachineMonthlyGrid(payload);
  renderCostPanel(payload);
  if (activeDrawerMonth) {
    renderMonthDrawer(activeDrawerMonth);
  }
}

function setRefreshButtonState(state) {
  if (!refreshButton) {
    return;
  }
  refreshButton.classList.toggle("is-refreshing", state === "loading");
  refreshButton.classList.toggle("is-complete", state === "complete");
  refreshButton.disabled = state === "loading";
  if (state === "loading") {
    refreshButton.textContent = "Refreshing";
    return;
  }
  if (state === "complete") {
    refreshButton.textContent = "Updated";
    return;
  }
  refreshButton.textContent = refreshButtonDefaultLabel;
}

async function fetchDashboard(options = {}) {
  const { withRefreshFeedback = false } = options;
  const startedAt = Date.now();
  if (withRefreshFeedback) {
    setRefreshButtonState("loading");
  }
  const response = await fetch("/api/dashboard");
  const payload = await response.json();
  if (!response.ok) {
    if (withRefreshFeedback) {
      setRefreshButtonState("idle");
    }
    throw new Error(payload.error || "Failed to load dashboard");
  }
  renderDashboard(payload);
  if (withRefreshFeedback) {
    const elapsed = Date.now() - startedAt;
    if (elapsed < 1000) {
      await sleep(1000 - elapsed);
    }
    setRefreshButtonState("complete");
    await sleep(420);
    setRefreshButtonState("idle");
  }
}

async function savePricing(form) {
  const input = new FormData(form);
  const payload = {
    pricingLabel: input.get("pricingLabel"),
    usdPerBlock: input.get("usdPerBlock"),
    unitsPerBlock: input.get("unitsPerBlock"),
    monthlyFlatUsd: input.get("monthlyFlatUsd"),
  };
  const response = await fetch("/api/pricing/codex", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || "Pricing save failed");
  }
  await fetchDashboard();
}

async function clearPricing() {
  const response = await fetch("/api/clear/codex-pricing", { method: "POST" });
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || "Pricing clear failed");
  }
  pricingForm.reset();
  await fetchDashboard();
}

pricingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await savePricing(pricingForm);
  } catch (error) {
    window.alert(error.message);
  }
});

document.getElementById("clear-pricing").addEventListener("click", async () => {
  try {
    await clearPricing();
  } catch (error) {
    window.alert(error.message);
  }
});

refreshButton.addEventListener("click", () => {
  fetchDashboard({ withRefreshFeedback: true }).catch((error) => {
    setRefreshButtonState("idle");
    window.alert(error.message);
  });
});

if (activityScopeSelect) {
  activityScopeSelect.addEventListener("change", () => {
    activeActivityScope = activityScopeSelect.value || "fleet";
    if (dashboardPayload) {
      renderTokenActivity(dashboardPayload);
    }
  });
}

for (const button of activityModeButtons) {
  button.addEventListener("click", () => {
    const requestedMode = button.dataset.activityMode || "daily";
    if (requestedMode === activeActivityMode) {
      return;
    }
    activeActivityMode = requestedMode;
    if (dashboardPayload) {
      renderTokenActivity(dashboardPayload);
    }
  });
}

if (tokenActivity) {
  tokenActivity.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) {
      return;
    }
    const monthTrigger = event.target.closest("[data-activity-month]");
    if (monthTrigger) {
      openMonthDrawer(
        monthTrigger.dataset.activityMonth,
        monthTrigger.dataset.activityMachine || null
      );
      return;
    }
    const trigger = event.target.closest("[data-activity-day]");
    if (!trigger) {
      return;
    }
    const day = trigger.dataset.activityDay;
    if (!day) {
      return;
    }
    openMonthDrawer(day.slice(0, 7), trigger.dataset.activityMachine || null, day);
  });
}

monthlyBars.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const trigger = event.target.closest("[data-month-drawer-month]");
  if (!trigger) {
    return;
  }
  openMonthDrawer(trigger.dataset.monthDrawerMonth);
});

machineMonthlyGrid.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const trigger = event.target.closest("[data-month-drawer-month]");
  if (!trigger) {
    return;
  }
  openMonthDrawer(trigger.dataset.monthDrawerMonth, trigger.dataset.monthDrawerMachine);
});

monthDailyBars.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const trigger = event.target.closest("[data-month-drawer-day]");
  if (!trigger || !activeDrawerMonth) {
    return;
  }
  activeDrawerDay = trigger.dataset.monthDrawerDay;
  activeDrawerTypeScope = "day";
  renderMonthDrawer(activeDrawerMonth);
});

monthDrawer.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const trigger = event.target.closest("[data-token-type-scope]");
  if (!trigger || !activeDrawerMonth) {
    return;
  }
  activeDrawerTypeScope = trigger.dataset.tokenTypeScope === "day" ? "day" : "month";
  renderMonthDrawer(activeDrawerMonth);
});

monthDrawerClose.addEventListener("click", closeMonthDrawer);
monthDrawerBackdrop.addEventListener("click", closeMonthDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && activeDrawerMonth) {
    closeMonthDrawer();
  }
});

if (scrollTopButton) {
  scrollTopButton.tabIndex = -1;
  scrollTopButton.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  window.addEventListener("scroll", requestScrollTopButtonUpdate, { passive: true });
  window.addEventListener("resize", requestScrollTopButtonUpdate);
  updateScrollTopButton();
}

fetchDashboard().catch((error) => window.alert(error.message));
