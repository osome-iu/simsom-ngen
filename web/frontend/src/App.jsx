import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import NetworkGraph, {
  NETWORK_ACTION_COLORS,
  NETWORK_USER_COLORS,
} from "./NetworkGraph.jsx";
import {
  analyticsCsv,
  buildExportBundle,
  buildHtmlReport,
  completeJson,
  downloadTextFile,
  edgesCsv,
  exportBaseName,
  nodesCsv,
  simulationOutcomeLabel,
} from "./reportExports.js";

const REFRESH_INTERVAL_MS = 5000;
const NETWORK_EDGE_LIMIT = 100000;
const NETWORK_NODE_LIMIT = 50000;

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function number(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function percent(value) {
  if (value === null || value === undefined || value === "") return "—";
  return `${number(Number(value) * 100, 1)}%`;
}

function duration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 60) return `${number(seconds, 1)}s`;
  if (seconds >= 60 * 60) {
    const hours = Math.floor(seconds / (60 * 60));
    const minutes = Math.floor((seconds % (60 * 60)) / 60);
    return `${hours}h ${minutes}m`;
  }
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function clone(value) {
  return value ? JSON.parse(JSON.stringify(value)) : value;
}

function withValueAtPath(source, path, value) {
  const next = clone(source);
  let current = next;
  path.slice(0, -1).forEach((key) => { current = current[key]; });
  current[path.at(-1)] = value;
  return next;
}

function App() {
  const dashboardRef = useRef(null);
  const [run, setRun] = useState({ status: "idle", output: [] });
  const [results, setResults] = useState({ available: true, series: [] });
  const [numProcs, setNumProcs] = useState(8);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedConfig, setAdvancedConfig] = useState(null);
  const [defaultAdvancedConfig, setDefaultAdvancedConfig] = useState(null);
  const [networkGraph, setNetworkGraph] = useState({
    available: true,
    nodes: [],
    edges: [],
    meta: {},
  });
  const [networkLoading, setNetworkLoading] = useState(false);
  const resultsLoadingRef = useRef(false);
  const currentRunRef = useRef(null);
  const [error, setError] = useState("");
  const [launchError, setLaunchError] = useState("");

  useEffect(() => {
    if ("scrollRestoration" in window.history) {
      window.history.scrollRestoration = "manual";
    }
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
  }, []);

  const refresh = useCallback(async () => {
    try {
      const nextRun = await api("/api/runs/current");
      currentRunRef.current = nextRun;
      setRun(nextRun);
      setError("");
      if (resultsLoadingRef.current) return;
      resultsLoadingRef.current = true;
      try {
        const nextResults = await api("/api/results");
        if (currentRunRef.current?.id !== nextRun.id) return;
        const currentRun = currentRunRef.current;
        const setupInProgress = (
          currentRun.status === "running" || currentRun.status === "stopping"
        ) && (!currentRun.setup?.available || currentRun.setup?.status === "running");
        setResults(setupInProgress ? { available: true, series: [] } : nextResults);
      } finally {
        resultsLoadingRef.current = false;
      }
    } catch (requestError) {
      setError(requestError.message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = window.setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    api("/api/config/defaults")
      .then(({ config }) => {
        setDefaultAdvancedConfig(config);
        setAdvancedConfig(clone(config));
      })
      .catch((requestError) => setError(requestError.message));
  }, []);

  async function startRun() {
    setLaunchError("");
    try {
      const body = { numProcs: Number(numProcs) };
      if (advancedConfig) body.config = advancedConfig;
      const nextRun = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify(body),
      });
      currentRunRef.current = nextRun;
      setRun(nextRun);
      setLaunchError("");
      setResults({ available: true, series: [] });
      setNetworkGraph({ available: true, nodes: [], edges: [], meta: {} });
      window.requestAnimationFrame(() => {
        dashboardRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      window.setTimeout(refresh, 250);
    } catch (requestError) {
      setLaunchError(requestError.message);
    }
  }

  async function stopRun() {
    setError("");
    try {
      setRun(await api("/api/runs/current/stop", { method: "POST", body: "{}" }));
    } catch (requestError) {
      setError(requestError.message);
    }
  }

  const series = results.series || [];
  const latest = series.at(-1);

  useEffect(() => {
    const actionCount = latest?.actionCount;
    if (actionCount === undefined || actionCount === null) {
      return undefined;
    }

    let active = true;
    setNetworkLoading(true);
    api(`/api/network?edgeLimit=${NETWORK_EDGE_LIMIT}&nodeLimit=${NETWORK_NODE_LIMIT}`)
      .then((graph) => {
        if (active) setNetworkGraph(graph);
      })
      .catch((requestError) => {
        if (active) {
          setNetworkGraph({
            available: false,
            message: requestError.message,
            nodes: [],
            edges: [],
            meta: {},
          });
        }
      })
      .finally(() => {
        if (active) setNetworkLoading(false);
      });
    return () => { active = false; };
  }, [latest?.actionCount, run.id, run.status]);

  const properties = latest?.otherProperties || {};
  const runtime = properties.runtime || {};
  const policy = properties.policy_stats || {};
  const network = properties.network_stats || {};
  const actionMix = properties.action_type_ratios || {};
  const avgActionsPerUser = properties.avg_actions_per_user ?? (
    latest?.userCount ? latest.actionCount / latest.userCount : null
  );
  const isRunning = run.status === "running" || run.status === "stopping";

const runStartedAt = Date.parse(run.startedAt || "");
const lifecycleStartedAt = Date.parse(results.lifecycle?.startedAt || "");

// The database can retain results from an earlier run. Do not expose its
// clock value until analytics confirms that a snapshot belongs to the run
// currently owned by this dashboard process.
const hasCurrentRunSnapshot = Boolean(
  run.id
  && latest
  && Number.isFinite(runStartedAt)
  && Number.isFinite(lifecycleStartedAt)
  && lifecycleStartedAt >= runStartedAt
);

const simulatedClockTimestamp = hasCurrentRunSnapshot
  ? results.currentSimulatorTimestampMs ?? latest.simulatorTimestampMs
  : null;

const convergenceStatus = simulationOutcomeLabel({
  hasAnalytics: Boolean(latest),
  converged: latest?.converged,
  shouldTerminate: results.lifecycle?.shouldTerminate,
  runStatus: run.status,
});

const convergenceReason = latest?.convergenceReason
  || results.lifecycle?.analyticsStopReason
  || results.lifecycle?.reason;

  const statusCopy = useMemo(() => ({
    idle: "Ready",
    running: "Simulation running",
    stopping: "Stopping simulation",
    completed: "Run complete",
    failed: "Run failed",
  }[run.status] || run.status), [run.status]);

  function updateAdvancedConfig(path, value) {
    setAdvancedConfig((current) => withValueAtPath(current, path, value));
  }

  function setCriterionEnabled(criterion, checked) {
    if (!advancedConfig || !defaultAdvancedConfig) return;
    const criteria = { ...advancedConfig.termination.criteria_functions };
    if (checked) {
      criteria[criterion] = clone(
        defaultAdvancedConfig.termination.criteria_functions[criterion],
      );
    } else {
      delete criteria[criterion];
    }
    updateAdvancedConfig(["termination", "criteria_functions"], criteria);
  }

  return (
    <main className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <header className="topbar">
        <a className="brand" href="#top" aria-label="SimSoM home">
          <span className="brand-mark">S</span>
          <span>SimSoM <i>NGen</i></span>
        </a>
        <span className={`status status-${run.status}`}>
          <span className="status-dot" />{statusCopy}
        </span>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="eyebrow">Social systems, made observable</p>
          <h1>SimSoM NGen</h1>
          <p className="hero-description">
            Launch a distributed SimSoM NGen run and follow behavior, quality, policy,
            and network signals as they emerge.
          </p>
        </div>

        <div className="launch-card">
          <div>
            <p className="card-kicker">New simulation</p>
            <h2>Simulation inputs</h2>
          </div>
          <BasicRunConfig
            config={advancedConfig}
            disabled={isRunning}
            onChange={updateAdvancedConfig}
            onCriterionChange={setCriterionEnabled}
          />
          <div className="launch-options-row">
            <label className="process-input">
              <span>MPI processes</span>
              <input
                type="number"
                min="4"
                max="64"
                value={numProcs}
                disabled={isRunning}
                onChange={(event) => setNumProcs(event.target.value)}
              />
            </label>
            <button
              className={`advanced-toggle ${advancedOpen ? "is-open" : ""}`}
              type="button"
              onClick={() => setAdvancedOpen((current) => !current)}
              disabled={isRunning}
            >
              <span><i>⌘</i> Advanced configuration</span>
              <b>{advancedOpen ? "Hide" : "Customize"}</b>
            </button>
          </div>
          <div className="button-row">
            <button className="button button-primary" onClick={startRun} disabled={isRunning || !advancedConfig}>
              <span className="play-icon">▶</span>
              {isRunning ? "Running…" : "Run simulator"}
            </button>
            {isRunning && (
              <button className="button button-quiet" onClick={stopRun}>Stop</button>
            )}
          </div>
          <p className="launch-note">
            {advancedOpen ? "A validated run-specific config will be generated from these settings." : "Population and stopping settings will be applied with the remaining defaults."}
          </p>
        </div>
      </section>

      {advancedOpen && (
        <AdvancedConfig
          config={advancedConfig}
          defaults={defaultAdvancedConfig}
          disabled={isRunning}
          onClose={() => setAdvancedOpen(false)}
          onChange={updateAdvancedConfig}
          onReset={() => setAdvancedConfig(clone(defaultAdvancedConfig))}
        />
      )}

      {(launchError || error) && (
        <div className="alert">
          <strong>{launchError ? "Cannot start simulation" : "Connection issue"}</strong>
          <span>{launchError || error}</span>
        </div>
      )}
      <section ref={dashboardRef} className="dashboard-section">
        <div className="section-heading">
          <div className="section-heading-actions">
            <span className="last-updated">
              {latest ? `Snapshot ${series.length} · ${new Date(latest.recordedAt).toLocaleTimeString()}` : "No snapshots yet"}
            </span>
            <ExportMenu
              run={run}
              results={results}
              network={networkGraph}
              configuration={run.config || advancedConfig || defaultAdvancedConfig}
            />
          </div>
        </div>

        <div className="dashboard-grid">
            <Panel className="network-panel" title="User interaction network">
              <NetworkVisualization graph={networkGraph} loading={networkLoading} setup={run.setup} runStatus={run.status} avgActionsPerUser={avgActionsPerUser} hasAnalyticsSnapshot={hasCurrentRunSnapshot} firstSnapshotPercent={firstAnalyticsIntervalProgress(run, results)} />
            </Panel>

            <div className="dashboard-side-column">
              <SimulatedClock
                timestamp={simulatedClockTimestamp}
                isRunning={isRunning}
              />
              <Panel className="quality-timeline-panel" title="Average content quality timeline">
                <QualityTimeline series={series} />
                <div className="convergence-row">
                  <span className={`convergence-mark ${latest?.converged ? "is-converged" : ""}`} />
                  <div>
                    <strong>{convergenceStatus}</strong>
                    <p>{convergenceReason || (latest ? "No convergence reason reported." : "Convergence details will appear with the analytics.")}</p>
                  </div>
                </div>
              </Panel>
            </div>

            <details className="additional-analytics">
              <summary>
                <span>
                  <strong>Additional run analytics</strong>
                  <small>Quality, policy, runtime, network, and behavior statistics</small>
                </span>
              </summary>
              <div className="additional-analytics-grid">
                <Panel kicker="Model signals" title="Quality & policy">
                  <SignalRow label="Average content quality" value={latest?.avgActionQuality} />
                  <SignalRow label="Policy coverage" value={policy.policy_coverage} />
                  <SignalRow label="Intervention ratio" value={policy.policy_intervention_ratio} />
                </Panel>

                <Panel kicker="Scale & time" title="Run context">
                  <StatList rows={[
                    ["Software runtime", duration(runtime.software_runtime_seconds)],
                    ["Simulated runtime", duration(runtime.simulated_runtime_seconds)],
                    ["Average users followed", number(network.avg_friends_per_user, 2)],
                  ]} />
                </Panel>

                <Panel kicker="Behavior mix" title="Interaction types">
                  <ActionDonut values={actionMix} />
                </Panel>
              </div>
            </details>
        </div>
      </section>
    </main>
  );
}

function ExportMenu({ run, results, network, configuration }) {
  const menuRef = useRef(null);
  const hasAnalytics = Boolean(results?.series?.length);
  const hasNodes = Boolean(network?.nodes?.length);
  const hasEdges = Boolean(network?.edges?.length);
  const runActive = run?.status === "running" || run?.status === "stopping";

  function download(kind) {
    const bundle = buildExportBundle({ run, results, network, configuration });
    const baseName = exportBaseName(bundle);
    const exports = {
      html: [`${baseName}-report.html`, buildHtmlReport(bundle), "text/html"],
      analytics: [`${baseName}-analytics.csv`, analyticsCsv(results.series), "text/csv"],
      nodes: [`${baseName}-interaction-network-users.csv`, nodesCsv(network.nodes), "text/csv"],
      edges: [`${baseName}-interaction-network-edges.csv`, edgesCsv(network.edges), "text/csv"],
      json: [`${baseName}-complete.json`, completeJson(bundle), "application/json"],
    };
    const selected = exports[kind];
    if (!selected) return;
    downloadTextFile(...selected);
    if (menuRef.current) menuRef.current.open = false;
  }

  return (
    <details ref={menuRef} className={`export-menu ${hasAnalytics ? "" : "is-disabled"}`.trim()}>
      <summary
        aria-disabled={!hasAnalytics}
        onClick={(event) => {
          if (!hasAnalytics) event.preventDefault();
        }}
      >
        <span>↓</span> Export results
      </summary>
      <div className="export-menu-popover">
        <ExportOption
          title="Standalone HTML report"
          description="Summary, chart, network statistics, and configuration"
          onClick={() => download("html")}
        />
        <ExportOption
          title="Analytics CSV"
          description="Every analytics snapshot and nested measurement"
          onClick={() => download("analytics")}
        />
        <ExportOption
          title="Displayed interaction-network users CSV"
          description="Users displayed in the current visualization"
          disabled={!hasNodes}
          onClick={() => download("nodes")}
        />
        <ExportOption
          title="Displayed interaction-network edges CSV"
          description="Interactions displayed in the current visualization"
          disabled={!hasEdges}
          onClick={() => download("edges")}
        />
        <ExportOption
          title="Initial follower network CSV"
          description="Initial follower network"
          href="/api/exports/follower-network.csv"
          onClick={() => { if (menuRef.current) menuRef.current.open = false; }}
        />
        <ExportOption
          title="Database SQL (.sql.gz)"
          description="Compressed SQL with schema, data, and saved simulator clock"
          disabled={runActive}
          href="/api/exports/database.sql.gz"
          onClick={() => { if (menuRef.current) menuRef.current.open = false; }}
        />
        <ExportOption
          title="Complete JSON"
          description="Run, analytics, network, configuration, and log data"
          onClick={() => download("json")}
        />
      </div>
    </details>
  );
}

function ExportOption({ title, description, disabled = false, onClick, href }) {
  if (href) {
    return (
      <a
        href={disabled ? undefined : href}
        download
        aria-disabled={disabled}
        className={disabled ? "is-disabled" : ""}
        onClick={(event) => {
          if (disabled) {
            event.preventDefault();
            return;
          }
          onClick?.();
        }}
      >
        <strong>{title}</strong>
        <span>{description}</span>
      </a>
    );
  }
  return (
    <button type="button" disabled={disabled} onClick={onClick}>
      <strong>{title}</strong>
      <span>{description}</span>
    </button>
  );
}

function BasicRunConfig({ config, disabled, onChange, onCriterionChange }) {
  if (!config) {
    return <div className="basic-config-loading">Loading simulation inputs…</div>;
  }

  const normal = config.setup_db.user_types.normal;
  const bad = config.setup_db.user_types.bad;
  const criteria = config.termination.criteria_functions;
  const maxActions = criteria.max_avg_actions_per_user;
  const qualityEma = criteria.exponential_moving_average_action_quality;

  return (
    <div className="basic-config">
      <section className="basic-config-group population-config-group">
        <header>
          <strong>Population size</strong>
          <span>Users created at the start of the run</span>
        </header>
        <div className="field-pair">
          <ConfigField label="Normal" value={normal.num_nodes} min={0} max={1000000} step={100} onChange={(value) => onChange(["setup_db", "user_types", "normal", "num_nodes"], value)} disabled={disabled} />
          <ConfigField label="Bad Actors" value={bad.num_nodes} min={0} max={1000000} step={10} onChange={(value) => onChange(["setup_db", "user_types", "bad", "num_nodes"], value)} disabled={disabled} />
        </div>
      </section>

      <section className="basic-config-group">
        <header>
          <strong>Stopping criteria</strong>
          <span>The run stops when the first enabled criterion is met</span>
        </header>
        <div className="basic-stopping-controls">
          <div className="basic-stopping-option">
            <Toggle
              label="Stop on convergence"
              description="Stop when average content quality stabilizes."
              checked={Boolean(qualityEma)}
              onChange={(checked) => onCriterionChange("exponential_moving_average_action_quality", checked)}
              disabled={disabled || (Boolean(qualityEma) && !maxActions)}
            />
          </div>
          <div className={`basic-stopping-option${maxActions ? " has-threshold" : ""}`}>
            <Toggle
              label="Stop at average actions per user"
              description="Stop when the configured average is reached."
              checked={Boolean(maxActions)}
              onChange={(checked) => onCriterionChange("max_avg_actions_per_user", checked)}
              disabled={disabled || (Boolean(maxActions) && !qualityEma)}
            />
            {maxActions && (
              <ConfigField
                label="Limit"
                value={maxActions.threshold}
                min={0}
                step="any"
                onChange={(value) => onChange(["termination", "criteria_functions", "max_avg_actions_per_user", "threshold"], value)}
                disabled={disabled}
              />
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function AdvancedConfig({ config, defaults, disabled, onClose, onChange, onReset }) {
  if (!config || !defaults) {
    return <section className="advanced-section advanced-loading">Loading simulator defaults…</section>;
  }

  const normal = config.setup_db.user_types.normal;
  const bad = config.setup_db.user_types.bad;
  const recsys = config.recsys;
  const worker = config.worker;
  const actions = config.user_behavior.p_actions;
  const policy = config.policy;
  const analytics = config.analytics;
  const termination = config.termination;

  const criteria = termination.criteria_functions;
  const qualityEma = criteria.exponential_moving_average_action_quality;

  return (
    <section className="advanced-section">
      <button
        type="button"
        className="advanced-close"
        aria-label="Close advanced configuration"
        title="Close advanced configuration"
        onClick={onClose}
      >
        ×
      </button>
      <header className="advanced-header">
        <div>
          <p className="eyebrow">Run-specific overrides</p>
          <h2>Advanced configuration</h2>
          <p>Adjust the model without editing the JSONC file. Unexposed infrastructure settings keep their defaults.</p>
        </div>
        <button className="button button-quiet reset-button" onClick={onReset} disabled={disabled}>Reset defaults</button>
      </header>

      <div className="config-grid">
        <ConfigGroup title="Population behavior" subtitle="Lurking and quality distributions">
          <div className="field-pair">
            <ConfigField label="Normal lurker ratio" value={normal.lurker_ratio} min={0} max={0.99} step={0.01} onChange={(value) => onChange(["setup_db", "user_types", "normal", "lurker_ratio"], value)} disabled={disabled} />
            <ConfigField label="Bad actor lurker ratio" value={bad.lurker_ratio} min={0} max={0.99} step={0.01} onChange={(value) => onChange(["setup_db", "user_types", "bad", "lurker_ratio"], value)} disabled={disabled} />
          </div>
          <div className="field-pair">
            <ConfigField label="Normal quality weight" value={normal.user_quality_distribution.w} min={0} max={1} step={0.05} onChange={(value) => onChange(["setup_db", "user_types", "normal", "user_quality_distribution", "w"], value)} disabled={disabled} />
            <ConfigField label="Bad actor quality weight" value={bad.user_quality_distribution.w} min={0} max={1} step={0.05} onChange={(value) => onChange(["setup_db", "user_types", "bad", "user_quality_distribution", "w"], value)} disabled={disabled} />
          </div>
          <details className="sub-settings">
            <summary>Distribution shape</summary>
            <div className="field-pair">
              <ConfigField label="Normal lambda slope" value={normal.user_quality_distribution.lambda_slope} min={0.001} step={0.001} onChange={(value) => onChange(["setup_db", "user_types", "normal", "user_quality_distribution", "lambda_slope"], value)} disabled={disabled} />
              <ConfigField label="Bad actor lambda slope" value={bad.user_quality_distribution.lambda_slope} min={0.001} step={0.001} onChange={(value) => onChange(["setup_db", "user_types", "bad", "user_quality_distribution", "lambda_slope"], value)} disabled={disabled} />
            </div>
          </details>
        </ConfigGroup>

        <ConfigGroup title="Feeds & workers" subtitle="Candidate pools and write throughput">
          <div className="field-pair three-fields">
            <ConfigField label="Feed size" value={recsys.feed_size} min={0} step={1} onChange={(value) => onChange(["recsys", "feed_size"], value)} disabled={disabled} />
            <ConfigField label="Inventory size multiplier" value={recsys.inventory_composition.size_multiplier} min={1} step={1} onChange={(value) => onChange(["recsys", "inventory_composition", "size_multiplier"], value)} disabled={disabled} />
            <ConfigField label="Out-of-network ratio" value={recsys.inventory_composition.out_of_network_ratio} min={0} max={1} step={0.05} onChange={(value) => onChange(["recsys", "inventory_composition", "out_of_network_ratio"], value)} disabled={disabled} />
          </div>
          <div className="field-pair">
            <ConfigField label="Engagement lookback × population" value={recsys.engagement_action_lookback_population_multiplier} min={0.1} step={0.5} onChange={(value) => onChange(["recsys", "engagement_action_lookback_population_multiplier"], value)} disabled={disabled} />
          </div>
          <div className="field-pair three-fields">
            <ConfigField label="Worker batch" value={worker.batch_size} min={1} step={1} onChange={(value) => onChange(["worker", "batch_size"], value)} disabled={disabled} />
            <ConfigField label="Insert chunk" value={worker.action_insert_chunk_size} min={1} step={1} onChange={(value) => onChange(["worker", "action_insert_chunk_size"], value)} disabled={disabled} />
            <ConfigField label="Idle sleep (ms)" value={worker.idle_sleep_milliseconds} min={0} step={1} onChange={(value) => onChange(["worker", "idle_sleep_milliseconds"], value)} disabled={disabled} />
          </div>
        </ConfigGroup>

        <ConfigGroup className="actions-config" title="Action behavior" subtitle="Mean and standard deviation by action type">
          <div className="distribution-table">
            <div className="distribution-head"><span>Action</span><span>Mean</span><span>Std. dev.</span></div>
            {Object.entries(actions).map(([action, values]) => (
              <div className="distribution-row" key={action}>
                <strong>{action}</strong>
                <input type="number" min="0" step="0.01" value={values[0]} disabled={disabled} aria-label={`${action} mean`} onChange={(event) => onChange(["user_behavior", "p_actions", action], [parseNumericInput(event.target.value), values[1]])} />
                <input type="number" min="0" step="0.01" value={values[1]} disabled={disabled} aria-label={`${action} standard deviation`} onChange={(event) => onChange(["user_behavior", "p_actions", action], [values[0], parseNumericInput(event.target.value)])} />
              </div>
            ))}
          </div>
        </ConfigGroup>

        <ConfigGroup title="Policy & analysis" subtitle="Moderation, polling, and run completion">
          <Toggle label="Enable policy process" description="Evaluate eligible content against the quality threshold." checked={policy.enabled} onChange={(checked) => onChange(["policy", "enabled"], checked)} disabled={disabled} />
          <div className="field-pair three-fields">
            <ConfigField label="Quality threshold" value={policy.rule_functions.below_quality_threshold.threshold} min={0} max={1} step={0.05} onChange={(value) => onChange(["policy", "rule_functions", "below_quality_threshold", "threshold"], value)} disabled={disabled} />
            <ConfigField label="Policy poll (ms)" value={policy.poll_interval_milliseconds} min={0} step={100} onChange={(value) => onChange(["policy", "poll_interval_milliseconds"], value)} disabled={disabled} />
            <ConfigField label="Analytics interval" value={analytics.evaluation_interval.value} min={0} step="any" onChange={(value) => onChange(["analytics", "evaluation_interval", "value"], value)} disabled={disabled} />
          </div>
          <div className="field-pair">
            <ConfigSelect
              label="Analytics interval unit"
              value={analytics.evaluation_interval.unit}
              options={[
                ["simulated_hours", "Simulated hours"],
                ["average_actions_per_user", "Average actions per user"],
              ]}
              onChange={(value) => onChange(["analytics", "evaluation_interval", "unit"], value)}
              disabled={disabled}
            />
          </div>
          <div className="toggle-stack">
            {qualityEma && (
              <div className="field-pair">
                <ConfigField label="EMA change threshold" value={qualityEma.threshold} min={0} step="any" onChange={(value) => onChange(["termination", "criteria_functions", "exponential_moving_average_action_quality", "threshold"], value)} disabled={disabled} />
                <ConfigField label="EMA rho" value={qualityEma.rho} min={0} max={1} step="any" onChange={(value) => onChange(["termination", "criteria_functions", "exponential_moving_average_action_quality", "rho"], value)} disabled={disabled} />
              </div>
            )}
            <Toggle label="Dump database on completion" description="Export the configured schema after the final analytics pass." checked={config.controller.dump_on_termination} onChange={(checked) => onChange(["controller", "dump_on_termination"], checked)} disabled={disabled} />
          </div>
        </ConfigGroup>
      </div>
    </section>
  );
}

function ConfigGroup({ title, subtitle, className = "", children }) {
  return (
    <article className={`config-group ${className}`}>
      <header><div><h3>{title}</h3><p>{subtitle}</p></div></header>
      {children}
    </article>
  );
}

function ConfigField({ label, value, onChange, disabled, min, max, step }) {
  return (
    <label className="config-field">
      <span>{label}</span>
      <input type="number" value={value} min={min} max={max} step={step} disabled={disabled} onChange={(event) => onChange(parseNumericInput(event.target.value))} />
    </label>
  );
}

function ConfigSelect({ label, value, options, onChange, disabled }) {
  return (
    <label className="config-field">
      <span>{label}</span>
      <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, optionLabel]) => (
          <option value={optionValue} key={optionValue}>{optionLabel}</option>
        ))}
      </select>
    </label>
  );
}

function parseNumericInput(value) {
  return value === "" ? "" : Number(value);
}

function Toggle({ label, description, checked, onChange, disabled }) {
  return (
    <label className="toggle-row">
      <span><strong>{label}</strong><small>{description}</small></span>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
      <i />
    </label>
  );
}

function Panel({ kicker, title, className = "", children }) {
  return (
    <article className={`panel ${className}`}>
      <header>
        {kicker && <p>{kicker}</p>}
        <h3>{title}</h3>
      </header>
      {children}
    </article>
  );
}

function NetworkVisualization({ graph, loading, setup, runStatus, avgActionsPerUser, hasAnalyticsSnapshot, firstSnapshotPercent }) {
  const meta = graph.meta || {};
  const [selection, setSelection] = useState(null);
  const isLiveRun = runStatus === "running" || runStatus === "stopping";
  const isFiltered = Boolean(meta.kCoreApplied || meta.truncated);

  useEffect(() => {
    setSelection((current) => {
      if (!current) return null;
      const items = current.kind === "node" ? graph.nodes : graph.edges;
      const updatedItem = items?.find((item) => item.id === current.item.id);
      return updatedItem ? { ...current, item: updatedItem } : null;
    });
  }, [graph]);

  if (!graph.available) {
    return <div className="network-visualization"><NetworkProgress setup={setup} runStatus={runStatus} hasAnalyticsSnapshot={hasAnalyticsSnapshot} firstSnapshotPercent={firstSnapshotPercent} /><div className="network-empty"><strong>Network unavailable</strong><p>{graph.message}</p></div></div>;
  }
  if (!graph.nodes?.length) {
    return <div className="network-visualization"><NetworkProgress setup={setup} runStatus={runStatus} hasAnalyticsSnapshot={hasAnalyticsSnapshot} firstSnapshotPercent={firstSnapshotPercent} /><div className="network-empty"><strong>No network rows yet</strong><p>Users and post interactions will appear as the simulation initializes.</p></div></div>;
  }

  return (
    <div className="network-visualization">
      <div className="network-meta">
        <div className="network-meta-status" aria-live="polite">
          {loading && <em>Refreshing…</em>}
        </div>
        <div className="network-meta-stats">
            <div><strong>{number(meta.userCount)}</strong><span>{isFiltered ? "total users" : "users"}</span></div>
            {isFiltered && <div><strong>{number(meta.returnedNodeCount)}</strong><span>shown users</span></div>}
            <div><strong>{number(meta.interactionCount)}</strong><span>{isFiltered ? "total interactions" : "interactions"}</span></div>
            {isFiltered && <div><strong>{number(meta.returnedInteractionCount)}</strong><span>shown interactions</span></div>}
            <div><strong>{number(avgActionsPerUser, 2)}</strong><span>avg. actions / user</span></div>
            <div><strong>{number(meta.originalPostCount)}</strong><span>original posts</span></div>
        </div>
      </div>
      {meta.truncated && (
        <div className="network-sample-notice">
          {isLiveRun
            ? <>Live network snapshot. K-core filtering is currently applied at level k={number(meta.kCoreLevel)}. Showing {number(meta.returnedNodeCount)} users and {number(meta.returnedInteractionCount)} interactions. Final totals are unavailable until the simulation finishes.</>
            : <>Network generated successfully. K-core filtering was applied at level k={number(meta.kCoreLevel)}. Showing {number(meta.returnedNodeCount)} of {number(meta.userCount)} users and {number(meta.returnedInteractionCount)} of {number(meta.interactionCount)} interactions.</>}
        </div>
      )}
      <div className="network-workspace">
        <NetworkGraph
          graph={graph}
          selection={selection}
          onSelectionChange={setSelection}
        />
      </div>
      <NetworkDetailModal graph={graph} selection={selection} onClose={() => setSelection(null)} />
      <div className="network-footnote">
        <span>Drag to rotate · shift-drag to pan · scroll to zoom · select a node or edge for details</span>
        <span>
          Directed weighted edges connect an acting user to the parent post author. Edge width represents interaction frequency.
          {meta.truncated ? " Displaying the k-core-filtered network." : ""}
        </span>
      </div>
    </div>
  );
}

function firstAnalyticsIntervalProgress(run, results) {
  const interval = run.config?.analytics?.evaluation_interval;
  const simulatedTime = results.currentSimulatorTimestampMs;
  if (interval?.unit !== "simulated_hours" || simulatedTime == null) return null;
  const intervalMs = Number(interval.value) * 60 * 60 * 1000;
  const tickMs = Number(simulatedTime);
  if (!Number.isFinite(intervalMs) || intervalMs <= 0 || !Number.isFinite(tickMs)) return null;
  return Math.max(0, Math.min(100, tickMs * 100 / intervalMs));
}

function NetworkProgress({ setup, runStatus, hasAnalyticsSnapshot, firstSnapshotPercent }) {
  const isActive = runStatus === "running" || runStatus === "stopping";
  if (setup?.status === "complete") {
    if (!isActive) return null;
    const simulating = !hasAnalyticsSnapshot;
    const hasIntervalProgress = simulating && Number.isFinite(firstSnapshotPercent)
      && firstSnapshotPercent < 100;
    return (
      <div className="network-progress" aria-live="polite">
        <div className="network-progress-heading">
          <strong>{simulating ? "Simulating interactions" : "Loading interaction network"}</strong>
          {hasIntervalProgress && <span>{number(firstSnapshotPercent, 1)}%</span>}
        </div>
        <div className={`network-progress-track${hasIntervalProgress ? "" : " is-indeterminate"}`} role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={hasIntervalProgress ? firstSnapshotPercent : undefined}>
          <div className="network-progress-fill" style={{ width: hasIntervalProgress ? `${firstSnapshotPercent}%` : undefined }} />
        </div>
        <div className="network-progress-detail">
          {simulating ? "Workers are creating posts and interactions. The first network view appears after the analytics check." : "Preparing the latest network view…"}
        </div>
      </div>
    );
  }
  const current = Number(setup?.progress_current);
  const total = Number(setup?.progress_total);
  const hasProgress = setup?.progress_current != null && setup?.progress_total != null
    && Number.isFinite(current) && Number.isFinite(total) && total > 0;
  const percentValue = hasProgress
    ? Math.max(0, Math.min(100, Number(setup.progress_percent ?? current * 100 / total)))
    : 0;
  const stageLabels = {
    user_planning: "Planning users",
    user_generation: "Generating users",
    network_generation: "Generating follower network",
    network_import: "Importing follower network",
    database_schema: "Preparing database",
    database_load: "Loading database",
    database_indexes: "Building database indexes",
    setup: "Finishing setup",
  };
  const stage = stageLabels[setup?.stage] || "Starting simulation setup";
  const phase = setup?.phase ? ` · ${setup.phase.replaceAll("_", " ")}` : "";
  const detail = hasProgress
    ? `${number(current)} of ${number(total)} ${setup.progress_unit === "source_users" ? "source users processed" : setup.progress_unit || "items"}${setup.network_total_users ? ` · ${number(setup.network_total_users)} total users` : ""}`
    : (setup?.rows_sent != null ? `${number(setup.rows_sent)} rows loaded` : null);
  const details = [
    detail,
    setup?.total_edges !== undefined ? `${number(setup.total_edges)} edges generated` : null,
  ].filter(Boolean);

  if (!isActive && !hasProgress) return null;
  return (
    <div className="network-progress" aria-live="polite">
      <div className="network-progress-heading">
        <span><strong>{stage}</strong>{phase}</span>
        {hasProgress && <span>{number(percentValue, 1)}%</span>}
      </div>
      <div className={`network-progress-track${hasProgress ? "" : " is-indeterminate"}`} role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={hasProgress ? percentValue : undefined}>
        <div className="network-progress-fill" style={{ width: hasProgress ? `${percentValue}%` : undefined }} />
      </div>
      {details.length > 0 && <div className="network-progress-detail">{details.join(" · ")}</div>}
    </div>
  );
}

function NetworkDetailModal({ graph, selection, onClose }) {
  useEffect(() => {
    if (!selection) return undefined;
    const closeOnEscape = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selection, onClose]);

  if (!selection) return null;

  const closeOnBackdrop = (event) => {
    if (event.target === event.currentTarget) onClose();
  };

  if (selection.kind === "node") {
    const node = selection.item;
    const outgoing = graph.edges.filter((edge) => edge.source === node.id);
    const incoming = graph.edges.filter((edge) => edge.target === node.id);
    const outgoingCount = outgoing.reduce(
      (total, edge) => total + Number(edge.interactionCount || 1), 0,
    );
    const incomingCount = incoming.reduce(
      (total, edge) => total + Number(edge.interactionCount || 1), 0,
    );
    const neighbors = new Set([
      ...outgoing.map((edge) => edge.target),
      ...incoming.map((edge) => edge.source),
    ]);
    neighbors.delete(node.id);
    return (
      <NetworkModalFrame
        titleId="network-detail-title"
        onClose={onClose}
        onBackdrop={closeOnBackdrop}
      >
        <InspectorHeader
          color={NETWORK_USER_COLORS[node.userType] || "#56b4e9"}
          kicker="Selected user"
          title={node.label || `User ${node.id}`}
          badge={node.userType === "bad" ? "bad actor" : node.userType}
        />
        <dl className="network-detail-list">
          <Detail label="Activity level" value={number(node.activityLevel, 3)} />
          <Detail label="User quality" value={number(node.quality, 3)} />
          <Detail label="Outgoing interactions" value={number(outgoingCount)} />
          <Detail label="Incoming interactions" value={number(incomingCount)} />
          <Detail label="Connected users" value={number(neighbors.size)} />
        </dl>
        {graph.meta?.truncated && (
          <small>Connection counts reflect the interactions currently loaded into the graph.</small>
        )}
      </NetworkModalFrame>
    );
  }

  const edge = selection.item;
  const actor = graph.nodes.find((node) => node.id === edge.source);
  const parentAuthor = graph.nodes.find((node) => node.id === edge.target);
  const interactions = Array.isArray(edge.interactions) && edge.interactions.length
    ? edge.interactions
    : [edge];
  return (
    <NetworkModalFrame
      titleId="network-detail-title"
      onClose={onClose}
      onBackdrop={closeOnBackdrop}
    >
      <InspectorHeader
        color={NETWORK_ACTION_COLORS[edge.actionType] || "#b7c4ce"}
        kicker="Selected edge"
        title={`${edge.actionType.charAt(0).toUpperCase()}${edge.actionType.slice(1)}`}
      />
      <dl className="network-detail-list">
        <Detail label="Actor" value={actor?.label || `User ${edge.source}`} />
        <Detail label="Parent author" value={parentAuthor?.label || `User ${edge.target}`} />
        {interactions.length === 1 && (
          <>
            <Detail
              label="Simulated time"
              value={simulatedTime(Number(interactions[0].timestamp)) || "—"}
            />
            <Detail label="Content quality" value={number(interactions[0].quality, 3)} />
            <Detail label="Content appeal" value={number(interactions[0].appeal, 3)} />
            <Detail label="Content views" value={number(interactions[0].contentViewCount)} />
            <Detail
              label="Moderation"
              value={<ModerationDetails actions={interactions[0].contentModerationActions} />}
              wide
            />
          </>
        )}
      </dl>
      {interactions.length > 1 && (
        <section className="network-interactions" aria-labelledby="network-interactions-title">
          <h5 id="network-interactions-title">
            <span>Interactions</span>
            <strong>{number(interactions.length)}</strong>
          </h5>
          <ol>
            {interactions.map((interaction, index) => (
              <li key={interaction.id ?? index}>
                <header>
                  <strong>Interaction {index + 1}</strong>
                  <span>{simulatedTime(Number(interaction.timestamp)) || "—"}</span>
                </header>
                <dl className="network-detail-list">
                  <Detail label="Content quality" value={number(interaction.quality, 3)} />
                  <Detail label="Content appeal" value={number(interaction.appeal, 3)} />
                  <Detail label="Content views" value={number(interaction.contentViewCount)} />
                  <Detail
                    label="Moderation"
                    value={<ModerationDetails actions={interaction.contentModerationActions} />}
                    wide
                  />
                </dl>
              </li>
            ))}
          </ol>
        </section>
      )}
    </NetworkModalFrame>
  );
}

function NetworkModalFrame({ titleId, onClose, onBackdrop, children }) {
  return (
    <div className="network-modal-backdrop" onMouseDown={onBackdrop}>
      <section
        className="network-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <button className="network-modal-close" type="button" onClick={onClose} aria-label="Close details">
          ×
        </button>
        {children}
      </section>
    </div>
  );
}

function InspectorHeader({ color, kicker, title, badge }) {
  return (
    <header className="network-inspector-header">
      <i style={{ background: color }} />
      <div><p>{kicker}</p><h4 id="network-detail-title">{title}</h4></div>
      {badge && <span>{badge}</span>}
    </header>
  );
}

function Detail({ label, value, wide = false }) {
  return (
    <div className={wide ? "network-detail-wide" : ""}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function ModerationDetails({ actions }) {
  const entries = Array.isArray(actions)
    ? actions
    : actions && typeof actions === "object"
      ? [actions]
      : [];
  if (!entries.length) return "None";
  return (
    <ul className="moderation-details">
      {entries.map((action, index) => (
        <li key={index}>{moderationActionSummary(action)}</li>
      ))}
    </ul>
  );
}

function moderationActionSummary(action) {
  if (!action || typeof action !== "object") return String(action ?? "None");
  const details = [];
  if (action.intervention_type) details.push(formatMetadataText(action.intervention_type));
  if (action.violation_type) details.push(formatMetadataText(action.violation_type));
  if (action.policy_reviewed === true) details.push("Reviewed");
  if (action.policy_reviewed === false) details.push("Not reviewed");

  const recognized = new Set(["intervention_type", "violation_type", "policy_reviewed"]);
  Object.entries(action).forEach(([key, value]) => {
    if (recognized.has(key)) return;
    if (
      key === "rule"
      && formatMetadataText(value) === formatMetadataText(action.violation_type)
    ) return;
    const displayValue = value && typeof value === "object"
      ? JSON.stringify(value)
      : formatMetadataText(value);
    details.push(`${formatMetadataText(key)}: ${displayValue}`);
  });
  return details.length ? details.join(" · ") : "No moderation details";
}

function formatMetadataText(value) {
  return String(value ?? "").replace(/[_-]+/g, " ");
}

function SimulatedClock({ timestamp, isRunning }) {
  const previousTimestampRef = useRef(null);
  const value = Number(timestamp);
  const hasTime = timestamp !== null
    && timestamp !== undefined
    && Number.isFinite(value)
    && value >= 0;
  const shouldAnimate = hasTime
    && previousTimestampRef.current !== null
    && value >= previousTimestampRef.current;
  const totalSeconds = hasTime ? value / 1000 : 0;
  const minuteRotation = totalSeconds * 0.1;
  const hourRotation = totalSeconds / 120;

  useEffect(() => {
    previousTimestampRef.current = hasTime ? value : null;
  }, [hasTime, value]);

  return (
    <section
      className={`simulated-clock${shouldAnimate ? " is-advancing" : ""}`}
      style={{ "--clock-transition-duration": `${REFRESH_INTERVAL_MS}ms` }}
      aria-live="polite"
    >
      <div className="simulated-clock-readout">
        <div className="simulated-clock-label">
          <span className={`simulated-clock-dot${isRunning ? " is-running" : ""}`} />
          <span>Simulated time</span>
        </div>
        <time>{hasTime ? simulatedClockTime(value) : "--:--:--"}</time>
      </div>
      <svg className="simulated-clock-face" viewBox="0 0 100 100" aria-hidden="true">
        <circle className="simulated-clock-ring" cx="50" cy="50" r="43" />
        {Array.from({ length: 12 }, (_, index) => (
          <line
            key={index}
            className={`simulated-clock-tick${index % 3 === 0 ? " is-hour" : ""}`}
            x1="50"
            y1="10"
            x2="50"
            y2={index % 3 === 0 ? "18" : "15"}
            transform={`rotate(${index * 30} 50 50)`}
          />
        ))}
        <line className="simulated-clock-hand is-hour" x1="50" y1="50" x2="50" y2="31" style={{ transform: `rotate(${hourRotation}deg)` }} />
        <line className="simulated-clock-hand is-minute" x1="50" y1="50" x2="50" y2="23" style={{ transform: `rotate(${minuteRotation}deg)` }} />
        <circle className="simulated-clock-pin" cx="50" cy="50" r="3.5" />
      </svg>
    </section>
  );
}

function QualityTimeline({ series }) {
  const [hoveredPointIndex, setHoveredPointIndex] = useState(null);
  const width = 760;
  const height = 260;
  const plotLeft = 48;
  const plotRight = 22;
  const plotTop = 22;
  const plotBottom = 22;
  const values = series.map((point) => {
    if (point.avgActionQuality === null || point.avgActionQuality === undefined) return null;
    const value = Number(point.avgActionQuality);
    return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : null;
  });
  const observedValues = values.filter((value) => value !== null);
  const minimum = observedValues.length ? Math.min(...observedValues) : 0;
  const maximum = observedValues.length ? Math.max(...observedValues) : 1;
  const valuePadding = Math.max((maximum - minimum) * 0.12, 0.002);
  const lowerBound = observedValues.length ? Math.max(0, minimum - valuePadding) : 0;
  const upperBound = observedValues.length ? Math.min(1, maximum + valuePadding) : 1;
  const valueRange = upperBound - lowerBound || 0.01;
  const plotHeight = height - plotTop - plotBottom;
  const valueToY = (value) => height - plotBottom - ((value - lowerBound) / valueRange) * plotHeight;
  const qualityTicks = [upperBound, (upperBound + lowerBound) / 2, lowerBound];
  const simulatedTimestamps = series.map((point) => Number(point.simulatorTimestampMs));
  const firstSimulatedTimestamp = simulatedTimestamps[0];
  const latestSimulatedTimestamp = simulatedTimestamps.at(-1);
  const hasSimulatedTimeRange = Number.isFinite(firstSimulatedTimestamp)
    && Number.isFinite(latestSimulatedTimestamp)
    && latestSimulatedTimestamp > firstSimulatedTimestamp;
  const points = values.flatMap((value, index) => {
    if (value === null) return [];
    const x = series.length === 1
      ? (plotLeft + width - plotRight) / 2
      : hasSimulatedTimeRange && Number.isFinite(simulatedTimestamps[index])
        ? plotLeft + ((simulatedTimestamps[index] - firstSimulatedTimestamp) / (latestSimulatedTimestamp - firstSimulatedTimestamp)) * (width - plotLeft - plotRight)
        : plotLeft + (index / (series.length - 1)) * (width - plotLeft - plotRight);
    const y = valueToY(value);
    return [{ x, y, value, simulatedTimestamp: simulatedTimestamps[index] }];
  });
  const line = points.map(({ x, y }) => `${x},${y}`).join(" ");
  const area = points.length ? `${points[0].x},${height - plotBottom} ${line} ${points.at(-1).x},${height - plotBottom}` : "";
  const hoveredPoint = points[hoveredPointIndex] || null;
  const tooltipWidth = 400;
  const tooltipHeight = 84;
  const tooltipX = hoveredPoint
    ? Math.max(plotLeft, Math.min(width - plotRight - tooltipWidth, hoveredPoint.x - tooltipWidth / 2))
    : 0;
  const tooltipY = hoveredPoint
    ? hoveredPoint.y - tooltipHeight - 14 >= plotTop
      ? hoveredPoint.y - tooltipHeight - 14
      : hoveredPoint.y + 14
    : 0;

  return (
    <div className="quality-timeline">
      <div className="chart-summary">
        <strong>{number(values.at(-1), 3)}</strong>
        <span>Average content quality</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Average content quality over simulated time">
        <defs>
          <linearGradient id="qualityAreaFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#0072b2" stopOpacity="0.3" />
            <stop offset="100%" stopColor="#0072b2" stopOpacity="0" />
          </linearGradient>
        </defs>
        {qualityTicks.map((value) => (
          <g key={value}>
            <line x1={plotLeft} x2={width - plotRight} y1={valueToY(value)} y2={valueToY(value)} className="grid-line" />
            <text x={plotLeft - 9} y={valueToY(value) + 4} textAnchor="end" className="quality-axis-label">
              {number(value, 3)}
            </text>
          </g>
        ))}
        {points.length > 0 && <polygon points={area} fill="url(#qualityAreaFill)" />}
        <polyline points={line} className="quality-line" />
        {points.map(({ x, y, value, simulatedTimestamp }, index) => (
          <g
            key={index}
            className="quality-point"
            role="img"
            aria-label={`${simulatedTime(simulatedTimestamp)}, average content quality ${number(value, 3)}`}
            tabIndex="0"
            onMouseEnter={() => setHoveredPointIndex(index)}
            onMouseLeave={() => setHoveredPointIndex(null)}
            onFocus={() => setHoveredPointIndex(index)}
            onBlur={() => setHoveredPointIndex(null)}
          >
            <circle cx={x} cy={y} r="12" className="quality-dot-target" />
            <circle cx={x} cy={y} r="4" className="quality-dot" />
          </g>
        ))}
        {hoveredPoint && (
          <g className="quality-tooltip" pointerEvents="none">
            <rect x={tooltipX} y={tooltipY} width={tooltipWidth} height={tooltipHeight} rx="8" />
            <text x={tooltipX + 16} y={tooltipY + 32}>Simulated time: {simulatedTime(hoveredPoint.simulatedTimestamp)}</text>
            <text x={tooltipX + 16} y={tooltipY + 66}>Average content quality: {number(hoveredPoint.value, 3)}</text>
          </g>
        )}
      </svg>
      <div className="chart-axis">
        <span>{simulatedTime(firstSimulatedTimestamp)}</span>
        <strong>Simulated time</strong>
        <span>{simulatedTime(latestSimulatedTimestamp)}</span>
      </div>
    </div>
  );
}

function simulatedTime(value) {
  if (!Number.isFinite(value) || value < 0) return "";
  const totalSeconds = Math.floor(value / 1000);
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (days) return `${days}d ${hours % 24}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

function simulatedClockTime(value) {
  const totalSeconds = Math.max(0, Math.floor(value / 1000));
  const days = Math.floor(totalSeconds / 86400);
  const hours = Math.floor((totalSeconds % 86400) / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const clock = [hours, minutes, seconds]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return days ? `${days}d ${clock}` : clock;
}

function SignalRow({ label, value }) {
  const numeric = value === null || value === undefined ? null : Number(value);
  return (
    <div className="signal-row">
      <div><span>{label}</span><strong>{percent(numeric)}</strong></div>
      <div className="signal-track"><i style={{ width: `${Math.max(0, Math.min(1, numeric || 0)) * 100}%` }} /></div>
    </div>
  );
}

function StatList({ rows }) {
  return <dl className="stat-list">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

function ActionDonut({ values }) {
  // Use the network legend colors so each action type has one visual identity
  // throughout the dashboard, while text labels carry the same information.
  const entries = Object.entries(values).map(([label, value]) => ({
    label,
    value: Number(value),
    color: NETWORK_ACTION_COLORS[label] || "#8d9aaa",
  })).filter((entry) => Number.isFinite(entry.value));
  let cursor = 0;
  const stops = entries.map((entry) => {
    const start = cursor;
    cursor += entry.value * 100;
    return `${entry.color} ${start}% ${cursor}%`;
  });

  if (!entries.length) {
    return <p className="muted-copy">Interaction types will appear after the first analytics snapshot.</p>;
  }

  return (
    <div className="donut-layout">
      <div className="donut" style={{ background: `conic-gradient(${stops.join(", ")})` }}>
        <div><strong>{entries.length}</strong><span>interaction types</span></div>
      </div>
      <div className="legend">
        {entries.map((entry) => (
          <div key={entry.label}>
            <i style={{ backgroundColor: entry.color }} />
            <span>{entry.label}</span>
            <strong>{percent(entry.value)}</strong>
          </div>
        ))}
      </div>
    </div>
  );
}

export default App;
