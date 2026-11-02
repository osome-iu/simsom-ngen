const ANALYTICS_COLUMNS = [
  "id",
  "recordedAt",
  "userCount",
  "actionCount",
  "avgActivityLevel",
  "avgActionQuality",
  "converged",
  "convergenceReason",
];

const NODE_COLUMNS = ["id", "label", "userType", "activityLevel", "quality"];

const EDGE_COLUMNS = [
  "id",
  "source",
  "target",
  "actionType",
  "timestamp",
  "parentActionId",
  "contentActionId",
  "quality",
  "appeal",
  "contentViewCount",
  "contentRepeatActorCount",
  "contentEmbeddingDimensions",
];

export function buildExportBundle({ run, results, network, configuration }) {
  return {
    metadata: {
      application: "SimSoM Social Media Simulator",
      formatVersion: 1,
      exportedAt: new Date().toISOString(),
    },
    run: run || {},
    analytics: results || { available: false, series: [] },
    network: network || { available: false, nodes: [], edges: [], meta: {} },
    configuration: configuration || null,
  };
}

export function analyticsCsv(series = []) {
  return recordsToCsv(series, ANALYTICS_COLUMNS);
}

export function nodesCsv(nodes = []) {
  return recordsToCsv(nodes, NODE_COLUMNS);
}

export function edgesCsv(edges = []) {
  return recordsToCsv(edges, EDGE_COLUMNS);
}

export function completeJson(bundle) {
  return `${JSON.stringify(bundle, null, 2)}\n`;
}

export function exportBaseName(bundle) {
  const source = bundle?.run?.startedAt || bundle?.metadata?.exportedAt || new Date().toISOString();
  const stamp = String(source)
    .replace(/\.\d{3}Z$/, "Z")
    .replace(/[^0-9]/g, "")
    .slice(0, 14);
  return `simsom-${stamp || "export"}`;
}

export function downloadTextFile(filename, contents, mimeType) {
  const blob = new Blob([contents], { type: `${mimeType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

export function buildHtmlReport(bundle) {
  const series = bundle.analytics?.series || [];
  const latest = series.at(-1) || {};
  const properties = latest.otherProperties || {};
  const runtime = properties.runtime || {};
  const policy = properties.policy_stats || {};
  const networkStats = properties.network_stats || {};
  const actionMix = properties.action_type_ratios || {};
  const nodes = bundle.network?.nodes || [];
  const edges = bundle.network?.edges || [];
  const networkMeta = bundle.network?.meta || {};
  const lifecycle = bundle.analytics?.lifecycle || {};
  const averageActions = properties.avg_actions_per_user ?? (
    latest.userCount
      ? Number(latest.actionCount || 0) / Number(latest.userCount)
      : null
  );
  const userTypes = countBy(nodes, "userType");
  const edgeTypes = countBy(edges, "actionType");
  const simulatedTimestamps = series
    .map((point) => Number(point.simulatorTimestampMs))
    .filter(Number.isFinite);
  const firstSimulatedTimestamp = simulatedTimestamps[0];
  const latestSimulatedTimestamp = Number(
    bundle.analytics?.currentSimulatorTimestampMs
      ?? latest.simulatorTimestampMs
      ?? simulatedTimestamps.at(-1),
  );
  const convergenceReason = latest.convergenceReason
    || lifecycle.analyticsConvergenceReason
    || lifecycle.analyticsStopReason
    || lifecycle.reason;
  const convergenceStatus = !series.length
    ? "Not evaluated"
    : latest.converged
      ? "Converged"
      : "Still evolving";

  const summaryCards = [
    ["Users / nodes", formatNumber(networkMeta.userCount ?? latest.userCount ?? nodes.length)],
    ["Interactions / edges", formatNumber(networkMeta.interactionCount ?? edges.length)],
    ["Avg. actions / user", formatNumber(averageActions, 2)],
    ["Original posts", formatNumber(networkMeta.originalPostCount)],
  ];
  const runRows = [
    ["Status", bundle.run?.status],
    ["Started", formatDate(bundle.run?.startedAt)],
    ["Finished", formatDate(bundle.run?.finishedAt)],
    ["Return code", bundle.run?.returnCode],
    ["Users", latest.userCount],
    ["Actions", latest.actionCount],
    ["Analytics snapshots", series.length],
    ["Software runtime", formatDuration(runtime.software_runtime_seconds)],
    ["Simulated runtime", formatDuration(runtime.simulated_runtime_seconds)],
  ];
  const signalRows = [
    ["Average action quality", formatNumber(latest.avgActionQuality, 3)],
    ["Policy coverage", formatPercent(policy.policy_coverage)],
    ["Intervention ratio", formatPercent(policy.policy_intervention_ratio)],
    ["Average friends per user", formatNumber(networkStats.avg_friends_per_user, 2)],
    ["Average followers per user", formatNumber(networkStats.avg_followers_per_user, 2)],
  ];
  const networkRows = [
    ["Users / nodes", networkMeta.userCount ?? nodes.length],
    ["Shown users / nodes", networkMeta.truncated ? networkMeta.returnedNodeCount ?? nodes.length : null],
    ["Returned interactions / edges", networkMeta.returnedEdgeCount ?? edges.length],
    ["Total interactions", networkMeta.interactionCount],
    ["Original posts", networkMeta.originalPostCount],
    ["K-core level", networkMeta.truncated ? networkMeta.kCoreLevel : null],
    ["Network export truncated", networkMeta.truncated == null ? null : networkMeta.truncated ? "Yes" : "No"],
  ];
  const configurationRows = Object.entries(flattenRecord(bundle.configuration || {}));

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SimSoM NGen Simulation Report</title>
  <style>
    :root { color-scheme: light; --ink: #17212b; --muted: #647382; --line: #dce3e8; --blue: #0072b2; --sky: #0072b2; --gold: #e69f00; --panel: #fff; --soft: #f4f7f8; --background: #fff; }
    * { box-sizing: border-box; }
    body { margin: 0; color: var(--ink); background: var(--background); font: 14px/1.55 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { width: min(1120px, calc(100% - 48px)); margin: 0 auto; padding: 44px 0 72px; }
    header.report-header { display: flex; justify-content: space-between; gap: 30px; align-items: flex-end; padding-bottom: 22px; border-bottom: 1px solid var(--line); }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 9px 14px; color: #07131d; background: var(--sky); font-weight: 750; }
    .kicker { margin: 0 0 5px; color: var(--sky); font-size: 11px; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 34px; line-height: 1.1; letter-spacing: -.035em; }
    .report-meta { color: var(--muted); font-size: 12px; text-align: right; }
    .cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 24px 0; }
    .card, .panel { border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
    .card { padding: 17px; background: var(--soft); }
    .card span { color: var(--muted); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
    .card strong { display: block; margin-top: 5px; font-size: 24px; letter-spacing: -.03em; }
    section { break-inside: avoid; }
    h2 { margin: 0; font-size: 19px; letter-spacing: -.02em; }
    .primary-grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(250px, .8fr); gap: 14px; align-items: start; }
    .timeline-panel { padding: 22px; }
    .panel-heading { display: flex; justify-content: space-between; gap: 24px; align-items: baseline; margin-bottom: 14px; }
    .quality-summary { color: var(--sky); font-size: 24px; font-weight: 750; letter-spacing: -.035em; white-space: nowrap; }
    .chart { min-width: 0; }
    .chart svg { display: block; width: 100%; height: auto; }
    .chart-axis { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; color: var(--muted); font-size: 11px; }
    .chart-axis strong { color: #526473; font-weight: 600; }
    .chart-axis span:last-child { text-align: right; }
    .side-column { display: grid; gap: 14px; }
    .time-card, .convergence-card { padding: 20px; }
    .time-card strong { display: block; margin-top: 8px; color: var(--ink); font: 700 clamp(1.25rem, 2.6vw, 1.8rem)/1.2 "SFMono-Regular", Consolas, monospace; }
    .status-row { display: flex; gap: 10px; align-items: flex-start; }
    .status-mark { width: 11px; height: 11px; flex: 0 0 auto; margin-top: 5px; border: 2px solid #677686; border-radius: 50%; }
    .status-mark.converged { border-color: var(--sky); background: var(--sky); box-shadow: 0 0 0 5px rgba(86,180,233,.08); }
    .convergence-card strong { display: block; font-size: 15px; }
    .convergence-card p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
    .details-heading { margin: 34px 0 14px; }
    .details-heading p { margin: 4px 0 0; color: var(--muted); font-size: 12px; }
    .columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-bottom: 14px; }
    .detail-panel { min-width: 0; padding: 20px; }
    .detail-panel h2 { margin-bottom: 12px; font-size: 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); background: var(--soft); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
    td:first-child { color: var(--muted); width: 52%; }
    tr:last-child td { border-bottom: 0; }
    .configuration { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .configuration td { overflow-wrap: anywhere; }
    .configuration-panel { margin-top: 14px; }
    .note { padding: 13px 15px; border-left: 3px solid var(--blue); background: #f3f6fc; color: #47576a; }
    .quality-grid { stroke: #dce3e8; stroke-width: 1; }
    .quality-axis-label { fill: var(--muted); font-size: 11px; }
    .quality-line { fill: none; stroke: var(--blue); stroke-width: 3; stroke-linecap: round; stroke-linejoin: round; }
    .quality-dot { fill: var(--panel); stroke: var(--blue); stroke-width: 2; }
    footer { margin-top: 46px; padding-top: 16px; color: var(--muted); border-top: 1px solid var(--line); font-size: 11px; }
    @media (max-width: 780px) { .cards, .columns { grid-template-columns: 1fr 1fr; } .primary-grid { grid-template-columns: 1fr; } header.report-header { align-items: flex-start; flex-direction: column; } .report-meta { text-align: left; } }
    @media (max-width: 520px) { main { width: min(100% - 28px, 1120px); } .cards, .columns { grid-template-columns: 1fr; } .panel-heading { align-items: flex-start; flex-direction: column; gap: 4px; } }
    @media print { main { width: 100%; padding: 0; } .card, .panel { break-inside: avoid; } .quality-dot { fill: #fff; } }
  </style>
</head>
<body>
  <main>
    <header class="report-header">
      <div class="brand"><span class="brand-mark">S</span><div><p class="kicker">Simulation report</p><h1>SimSoM NGen</h1></div></div>
      <div class="report-meta">Generated ${escapeHtml(formatDate(bundle.metadata?.exportedAt))}</div>
    </header>
    <div class="cards">${summaryCards.map(([label, value]) => `<div class="card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(displayValue(value))}</strong></div>`).join("")}</div>
    <div class="primary-grid">
      <section class="panel timeline-panel">
        <div class="panel-heading"><div><p class="kicker">Model signal</p><h2>Average action quality timeline</h2></div><span class="quality-summary">${escapeHtml(formatNumber(latest.avgActionQuality, 3))}</span></div>
        <div class="chart">${qualityChartSvg(series)}<div class="chart-axis"><span>${escapeHtml(formatSimulatedTime(firstSimulatedTimestamp))}</span><strong>Simulated time</strong><span>${escapeHtml(formatSimulatedTime(latestSimulatedTimestamp))}</span></div></div>
      </section>
      <aside class="side-column">
        <section class="panel time-card"><p class="kicker">Simulated time</p><strong>${escapeHtml(formatSimulatedClock(latestSimulatedTimestamp))}</strong></section>
        <section class="panel convergence-card"><div class="status-row"><span class="status-mark${latest.converged ? " converged" : ""}"></span><div><strong>${escapeHtml(convergenceStatus)}</strong><p>${escapeHtml(convergenceReason || (series.length ? "No convergence reason reported." : "No analytics snapshots were available."))}</p></div></div></section>
      </aside>
    </div>
    <div class="details-heading"><h2>Additional run analytics</h2><p>Quality, policy, runtime, network, and configuration details</p></div>
    <div class="columns">
      <section class="panel detail-panel"><h2>Quality &amp; policy</h2>${keyValueTable(signalRows)}</section>
      <section class="panel detail-panel"><h2>Run context</h2>${keyValueTable(runRows)}</section>
      <section class="panel detail-panel"><h2>Network summary</h2>${keyValueTable(networkRows)}</section>
    </div>
    <div class="columns">
      <section class="panel detail-panel"><h2>Action mix</h2>${keyValueTable(Object.entries(actionMix).map(([key, value]) => [titleCase(key), formatPercent(value)]))}</section>
      <section class="panel detail-panel"><h2>User types</h2>${keyValueTable(Object.entries(userTypes).map(([key, value]) => [titleCase(key), value]))}</section>
      <section class="panel detail-panel"><h2>Interaction types</h2>${keyValueTable(Object.entries(edgeTypes).map(([key, value]) => [titleCase(key), value]))}</section>
    </div>
    ${networkMeta.truncated ? `<p class="note">K-core filtering was applied at level ${escapeHtml(formatNumber(networkMeta.kCoreLevel))}. The export includes ${escapeHtml(formatNumber(networkMeta.returnedNodeCount ?? nodes.length))} of ${escapeHtml(formatNumber(networkMeta.userCount))} users and ${escapeHtml(formatNumber(networkMeta.returnedEdgeCount ?? edges.length))} of ${escapeHtml(formatNumber(networkMeta.interactionCount))} interactions.</p>` : ""}
    <section class="panel detail-panel configuration-panel"><h2>Configuration</h2><p class="note">Configuration values used for this dashboard run.</p>${configurationRows.length ? keyValueTable(configurationRows, "configuration") : `<p>No run configuration was available.</p>`}</section>
    <footer>Generated by the SimSoM local web dashboard. The downloadable CSV and JSON exports retain the underlying values for further analysis.</footer>
  </main>
</body>
</html>\n`;
}

function recordsToCsv(records, preferredColumns) {
  const flattened = records.map((record) => flattenRecord(record));
  const discovered = new Set(flattened.flatMap((record) => Object.keys(record)));
  const columns = [
    ...preferredColumns.filter((column) => discovered.has(column) || records.length === 0),
    ...[...discovered].filter((column) => !preferredColumns.includes(column)).sort(),
  ];
  return `${[
    columns.map(csvCell).join(","),
    ...flattened.map((record) => columns.map((column) => csvCell(record[column])).join(",")),
  ].join("\n")}\n`;
}

function flattenRecord(value, prefix = "", output = {}) {
  if (isPlainObject(value) && Object.keys(value).length) {
    for (const [key, child] of Object.entries(value)) {
      flattenRecord(child, prefix ? `${prefix}.${key}` : key, output);
    }
  } else if (prefix) {
    output[prefix] = Array.isArray(value) || isPlainObject(value)
      ? JSON.stringify(value)
      : value;
  }
  return output;
}

function csvCell(value) {
  if (value === null || value === undefined) return "";
  let text = typeof value === "object" ? JSON.stringify(value) : String(value);
  if (typeof value === "string" && /^[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function qualityChartSvg(series) {
  const width = 760;
  const height = 250;
  const plotLeft = 50;
  const plotRight = 20;
  const plotTop = 20;
  const plotBottom = 24;
  const samples = series.flatMap((point, index) => {
    const value = Number(point.avgActionQuality);
    if (!Number.isFinite(value)) return [];
    return [{
      index,
      value: Math.max(0, Math.min(1, value)),
      simulatedTimestamp: Number(point.simulatorTimestampMs),
    }];
  });
  if (!samples.length) {
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="No average action quality snapshots"><text x="${width / 2}" y="${height / 2}" text-anchor="middle" class="quality-axis-label">No analytics snapshots</text></svg>`;
  }

  const values = samples.map(({ value }) => value);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const padding = Math.max((maximum - minimum) * 0.12, 0.002);
  const lowerBound = Math.max(0, minimum - padding);
  const upperBound = Math.min(1, maximum + padding);
  const valueRange = upperBound - lowerBound || 0.01;
  const firstTimestamp = samples[0].simulatedTimestamp;
  const lastTimestamp = samples.at(-1).simulatedTimestamp;
  const hasTimeRange = Number.isFinite(firstTimestamp)
    && Number.isFinite(lastTimestamp)
    && lastTimestamp > firstTimestamp;
  const plotWidth = width - plotLeft - plotRight;
  const plotHeight = height - plotTop - plotBottom;
  const points = samples.map((sample, position) => {
    const x = samples.length === 1
      ? plotLeft + plotWidth / 2
      : hasTimeRange && Number.isFinite(sample.simulatedTimestamp)
        ? plotLeft + ((sample.simulatedTimestamp - firstTimestamp) / (lastTimestamp - firstTimestamp)) * plotWidth
        : plotLeft + (position / (samples.length - 1)) * plotWidth;
    const y = height - plotBottom - ((sample.value - lowerBound) / valueRange) * plotHeight;
    return { ...sample, x, y };
  });
  const polyline = points.map(({ x, y }) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${points[0].x.toFixed(1)},${height - plotBottom} ${polyline} ${points.at(-1).x.toFixed(1)},${height - plotBottom}`;
  const ticks = [upperBound, (upperBound + lowerBound) / 2, lowerBound];
  const grid = ticks.map((value) => {
    const y = height - plotBottom - ((value - lowerBound) / valueRange) * plotHeight;
    return `<line class="quality-grid" x1="${plotLeft}" x2="${width - plotRight}" y1="${y}" y2="${y}"/><text class="quality-axis-label" x="${plotLeft - 8}" y="${y + 4}" text-anchor="end">${escapeHtml(formatNumber(value, 3))}</text>`;
  }).join("");
  const dots = points.map(({ x, y, value, simulatedTimestamp }) => (
    `<circle class="quality-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.5"><title>${escapeHtml(`${formatSimulatedTime(simulatedTimestamp)} · Average action quality ${formatNumber(value, 3)}`)}</title></circle>`
  )).join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Average action quality over simulated time"><defs><linearGradient id="quality-area-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#0072b2" stop-opacity=".3"/><stop offset="100%" stop-color="#0072b2" stop-opacity="0"/></linearGradient></defs>${grid}<polygon points="${area}" fill="url(#quality-area-fill)"/><polyline class="quality-line" points="${polyline}"/>${dots}</svg>`;
}

function keyValueTable(rows, className = "") {
  const availableRows = rows.filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!availableRows.length) return "<p>No data available.</p>";
  return `<table class="${className}"><thead><tr><th>Measure</th><th>Value</th></tr></thead><tbody>${availableRows.map(([label, value]) => `<tr><td>${escapeHtml(titleCase(label))}</td><td>${escapeHtml(displayValue(value))}</td></tr>`).join("")}</tbody></table>`;
}

function countBy(records, field) {
  return records.reduce((counts, record) => {
    const value = record?.[field] || "unknown";
    counts[value] = (counts[value] || 0) + 1;
    return counts;
  }, {});
}

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function displayValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return formatNumber(value, Number.isInteger(value) ? 0 : 3);
  return String(value);
}

function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPercent(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return `${formatNumber(Number(value) * 100, 1)}%`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "—";
  const numeric = Number(seconds);
  if (numeric < 60) return `${formatNumber(numeric, 1)} seconds`;
  if (numeric >= 60 * 60) {
    const hours = Math.floor(numeric / (60 * 60));
    const minutes = Math.floor((numeric % (60 * 60)) / 60);
    return `${hours} hr ${minutes} min`;
  }
  return `${Math.floor(numeric / 60)} min ${Math.round(numeric % 60)} sec`;
}

function formatSimulatedTime(value) {
  if (!Number.isFinite(Number(value)) || Number(value) < 0) return "Not available";
  const totalSeconds = Math.floor(Number(value) / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days) return `${days}d ${hours % 24}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

function formatSimulatedClock(value) {
  if (!Number.isFinite(Number(value)) || Number(value) < 0) return "Not available";
  const totalSeconds = Math.floor(Number(value) / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const clock = [hours, minutes, seconds]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return days ? `${days}d ${clock}` : clock;
}

function formatDate(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "medium" });
}

function titleCase(value) {
  return String(value)
    .replaceAll("_", " ")
    .replaceAll(".", " · ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
