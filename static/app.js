const coverageStrip = document.getElementById("coverage-strip");
const refreshButton = document.getElementById("refresh-dashboard");
const pricingForm = document.getElementById("pricing-form");
const refreshButtonDefaultLabel = refreshButton ? refreshButton.textContent : "Refresh";

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
    cost.rough_cost_total_usd ?? cost.usage_cost_total_usd ?? null;
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
    ["Fleet Rough Cost", headlineCost !== null && headlineCost !== undefined ? formatUsd(headlineCost) : "not set"],
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
      "Fleet Rough Cost",
      cost.rough_cost_total_usd !== null && cost.rough_cost_total_usd !== undefined
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
              return `
                <div class="chart-bar-card">
                  <div class="chart-value">${formatCompactNumber(value)}</div>
                  <div class="chart-column" style="height:${height}px">
                    <div class="chart-bar ${toneClass}" style="height:${Math.max(ratio * 100, value > 0 ? 6 : 2)}%" title="${escapeHtml(tooltip)}"></div>
                  </div>
                  <div class="chart-label">${escapeHtml(label)}</div>
                  ${meta ? `<div class="chart-meta">${escapeHtml(meta)}</div>` : ""}
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
            })}
          </section>
        `;
      })
      .join("")
  );
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
        meta: `cached snapshot · ${formatTokens(row.thread_count)} threads · 30d ${formatTokens(row.tokens_30d)} · 7d ${formatTokens(row.tokens_7d)}${row.cache_notice ? ` · ${row.cache_notice}` : ""}`,
      };
    }
    return {
      title: `${row.label}${row.host && row.host !== "local" ? ` (${row.host})` : ""}`,
      value: formatTokens(row.total_tokens),
      meta: `${formatTokens(row.thread_count)} threads · 30d ${formatTokens(row.tokens_30d)} · 7d ${formatTokens(row.tokens_7d)}`,
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

function renderCostPanel(source) {
  const pricing = source.pricing || {};
  const cost = source.cost || {};
  let details;
  if (cost.kind === "official_gpt54_rough") {
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
  document.getElementById("monthly-bars").innerHTML = renderAxisChart(monthlyRows, {
    labelKey: "month",
    valueKey: "value",
    emptyText: "No monthly data.",
    labelFormatter: (row) => normaliseBarLabel(row.month, "month"),
    tooltipFormatter: (row) => `${row.month}: ${formatTokens(row.value)} tokens`,
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
  renderCostPanel(source);
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

fetchDashboard().catch((error) => window.alert(error.message));
