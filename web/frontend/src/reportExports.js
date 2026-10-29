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
  "sessionId",
];

export function buildExportBundle({ run, results, network, configuration, logTail }) {
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
    log: logTail || null,
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
  const averageActions = latest.userCount
    ? Number(latest.actionCount || 0) / Number(latest.userCount)
    : null;
  const userTypes = countBy(nodes, "userType");
  const edgeTypes = countBy(edges, "actionType");

  const summaryCards = [
    ["Users", formatNumber(latest.userCount)],
    ["Actions", formatNumber(latest.actionCount)],
    ["Average quality", formatNumber(latest.avgActionQuality, 3)],
    ["Actions per user", formatNumber(averageActions, 2)],
  ];
  const runRows = [
    ["Run ID", bundle.run?.id],
    ["Status", bundle.run?.status],
    ["Started", formatDate(bundle.run?.startedAt)],
    ["Finished", formatDate(bundle.run?.finishedAt)],
    ["Return code", bundle.run?.returnCode],
    ["Analytics snapshots", series.length],
    ["Software runtime", formatDuration(runtime.software_runtime_seconds)],
    ["Simulated runtime", formatDuration(runtime.simulated_runtime_seconds)],
  ];
  const signalRows = [
    ["Converged", latest.converged == null ? null : latest.converged ? "Yes" : "No"],
    ["Convergence reason", latest.convergenceReason || lifecycle.analyticsConvergenceReason],
    ["Stop reason", lifecycle.analyticsStopReason || lifecycle.reason],
    ["Policy coverage", formatPercent(policy.policy_coverage)],
    ["Intervention ratio", formatPercent(policy.policy_intervention_ratio)],
    ["Average friends per user", formatNumber(networkStats.avg_friends_per_user, 2)],
    ["Average followers per user", formatNumber(networkStats.avg_followers_per_user, 2)],
  ];
  const networkRows = [
    ["Users / nodes", networkMeta.userCount ?? nodes.length],
    ["Returned interactions / edges", networkMeta.returnedEdgeCount ?? edges.length],
    ["Total interactions", networkMeta.interactionCount],
    ["Root posts", networkMeta.originalPostCount],
    ["Network export truncated", networkMeta.truncated == null ? null : networkMeta.truncated ? "Yes" : "No"],
  ];
  const configurationRows = Object.entries(flattenRecord(bundle.configuration || {}));

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SimSoM Analytics Report</title>
  <style>
    :root { color-scheme: light; --ink: #17212b; --muted: #647382; --line: #dce3e8; --mint: #0072b2; --blue: #0072b2; --soft: #f4f7f8; }
    * { box-sizing: border-box; }
    body { margin: 0; color: var(--ink); background: #fff; font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    main { width: min(1080px, calc(100% - 48px)); margin: 0 auto; padding: 54px 0 72px; }
    header.report-header { display: flex; justify-content: space-between; gap: 30px; align-items: flex-end; padding-bottom: 24px; border-bottom: 2px solid var(--ink); }
    .kicker { margin: 0 0 5px; color: var(--mint); font-size: 11px; font-weight: 750; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 0; font-size: 34px; line-height: 1.1; letter-spacing: -.035em; }
    .report-meta { color: var(--muted); font-size: 12px; text-align: right; }
    .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 28px 0; }
    .card { padding: 18px; border: 1px solid var(--line); border-radius: 10px; background: var(--soft); }
    .card span { color: var(--muted); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; }
    .card strong { display: block; margin-top: 7px; font-size: 26px; letter-spacing: -.03em; }
    section { margin-top: 34px; break-inside: avoid; }
    h2 { margin: 0 0 14px; font-size: 19px; letter-spacing: -.02em; }
    .chart { padding: 20px; border: 1px solid var(--line); border-radius: 10px; }
    .chart svg { display: block; width: 100%; height: auto; }
    .chart-axis { display: flex; justify-content: space-between; color: var(--muted); font-size: 11px; }
    .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 26px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { color: var(--muted); background: var(--soft); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; }
    td:first-child { color: var(--muted); width: 52%; }
    .configuration { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
    .configuration td { overflow-wrap: anywhere; }
    .note { padding: 13px 15px; border-left: 3px solid var(--blue); background: #f3f6fc; color: #47576a; }
    footer { margin-top: 46px; padding-top: 16px; color: var(--muted); border-top: 1px solid var(--line); font-size: 11px; }
    @media (max-width: 720px) { .cards, .columns { grid-template-columns: 1fr 1fr; } header.report-header { align-items: flex-start; flex-direction: column; } .report-meta { text-align: left; } }
    @media print { main { width: 100%; padding: 0; } .card, .chart { break-inside: avoid; } }
  </style>
</head>
<body>
  <main>
    <header class="report-header">
      <div><p class="kicker">Analytics export</p><h1>SimSoM Simulation Report</h1></div>
      <div class="report-meta">Generated ${escapeHtml(formatDate(bundle.metadata?.exportedAt))}<br>Run ${escapeHtml(bundle.run?.id || "not identified")}</div>
    </header>
    <div class="cards">${summaryCards.map(([label, value]) => `<div class="card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(displayValue(value))}</strong></div>`).join("")}</div>
    <section><h2>Action growth</h2><div class="chart">${actionChartSvg(series)}<div class="chart-axis"><span>${escapeHtml(formatDate(series[0]?.recordedAt))}</span><span>${escapeHtml(formatDate(latest.recordedAt))}</span></div></div></section>
    <div class="columns">
      <section><h2>Run context</h2>${keyValueTable(runRows)}</section>
      <section><h2>Quality and policy</h2>${keyValueTable(signalRows)}</section>
    </div>
    <div class="columns">
      <section><h2>Action mix</h2>${keyValueTable(Object.entries(actionMix).map(([key, value]) => [titleCase(key), formatPercent(value)]))}</section>
      <section><h2>Network summary</h2>${keyValueTable(networkRows)}</section>
    </div>
    <div class="columns">
      <section><h2>User types</h2>${keyValueTable(Object.entries(userTypes).map(([key, value]) => [titleCase(key), value]))}</section>
      <section><h2>Interaction types</h2>${keyValueTable(Object.entries(edgeTypes).map(([key, value]) => [titleCase(key), value]))}</section>
    </div>
    ${networkMeta.truncated ? `<section><p class="note">The network section contains the latest ${escapeHtml(formatNumber(networkMeta.returnedEdgeCount ?? edges.length))} interactions rather than all ${escapeHtml(formatNumber(networkMeta.interactionCount))} interactions.</p></section>` : ""}
    <section><h2>Configuration</h2><p class="note">These are the configuration values reported for this run.</p>${configurationRows.length ? keyValueTable(configurationRows, "configuration") : `<p>No run configuration was available.</p>`}</section>
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

function actionChartSvg(series) {
  const width = 900;
  const height = 250;
  const insetX = 44;
  const insetY = 24;
  const values = series.map((point) => Number(point.actionCount) || 0);
  const maximum = Math.max(...values, 1);
  const points = values.map((value, index) => {
    const x = values.length <= 1
      ? width / 2
      : insetX + (index / (values.length - 1)) * (width - insetX * 2);
    const y = height - insetY - (value / maximum) * (height - insetY * 2);
    return [x, y];
  });
  const polyline = points.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const y = height - insetY - ratio * (height - insetY * 2);
    return `<line x1="${insetX}" x2="${width - insetX}" y1="${y}" y2="${y}" stroke="#dce3e8"/><text x="${insetX - 8}" y="${y + 4}" text-anchor="end" fill="#647382" font-size="10">${escapeHtml(compactNumber(maximum * ratio))}</text>`;
  }).join("");
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Action count across analytics snapshots">${grid}${points.length ? `<polyline points="${polyline}" fill="none" stroke="#0072b2" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${points.map(([x, y]) => `<circle cx="${x}" cy="${y}" r="3.5" fill="#fff" stroke="#0072b2" stroke-width="2"/>`).join("")}` : `<text x="450" y="125" text-anchor="middle" fill="#647382">No analytics snapshots</text>`}</svg>`;
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

function compactNumber(value) {
  return Number(value).toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 });
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
