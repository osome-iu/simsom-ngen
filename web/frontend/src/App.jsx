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
  const [logTail, setLogTail] = useState({
    available: true,
    path: "logs/simsom.log",
    lines: [],
    modifiedAt: null,
    sizeBytes: 0,
  });
  const [error, setError] = useState("");
  const [launchError, setLaunchError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [nextRun, nextResults, nextLogTail] = await Promise.all([
        api("/api/runs/current"),
        api("/api/results"),
        api("/api/logs/tail?lines=200"),
      ]);
      setRun(nextRun);
      const setupInProgress = (
        nextRun.status === "running" || nextRun.status === "stopping"
      ) && (!nextRun.setup?.available || nextRun.setup?.status === "running");
      setResults(setupInProgress ? { available: true, series: [] } : nextResults);
      setLogTail(nextLogTail);
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setLoading(false);
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
      if (advancedOpen && advancedConfig) body.config = advancedConfig;
      const nextRun = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setRun(nextRun);
      setLaunchError("");
      setResults({ available: true, series: [] });
      setNetworkGraph({ available: true, nodes: [], edges: [], meta: {} });
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
  const timing = properties.timing?.inter_activity_ms || {};
  const actionMix = properties.action_type_ratios || {};
  const avgActionsPerUser = properties.avg_actions_per_user ?? (
    latest?.userCount ? latest.actionCount / latest.userCount : null
  );
  const isRunning = run.status === "running" || run.status === "stopping";

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

  return (
    <main className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <header className="topbar">
        <a className="brand" href="#top" aria-label="SimSoM home">
          <span className="brand-mark">S</span>
          <span>SimSoM <i>Control Room</i></span>
        </a>
        <span className={`status status-${run.status}`}>
          <span className="status-dot" />{statusCopy}
        </span>
      </header>

      <section className="hero" id="top">
        <div className="hero-copy">
          <p className="eyebrow">Social systems, made observable</p>
          <h1>Social Media Simulator</h1>
          <p className="hero-description">
            Launch a distributed SimSoM run and follow behavior, quality, policy,
            and network signals as they emerge.
          </p>
        </div>

        <div className="launch-card">
          <div>
            <p className="card-kicker">New simulation</p>
          </div>
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
          <div className="button-row">
            <button className="button button-primary" onClick={startRun} disabled={isRunning}>
              <span className="play-icon">▶</span>
              {isRunning ? "Running…" : "Run simulator"}
            </button>
            {isRunning && (
              <button className="button button-quiet" onClick={stopRun}>Stop</button>
            )}
          </div>
          <p className="launch-note">
            {advancedOpen ? "A validated run-specific config will be generated from these settings." : "Uses the active simulator config and local PostgreSQL database."}
          </p>
        </div>
      </section>

      {advancedOpen && (
        <AdvancedConfig
          config={advancedConfig}
          defaults={defaultAdvancedConfig}
          disabled={isRunning}
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
      {!results.available && !launchError && !error && (
        <div className="alert"><strong>Waiting for analytics</strong><span>{results.message}</span></div>
      )}

      <section className="dashboard-section">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Latest state</p>
            <h2>Simulation pulse</h2>
          </div>
          <div className="section-heading-actions">
            <span className="last-updated">
              {latest ? `Snapshot ${series.length} · ${new Date(latest.recordedAt).toLocaleTimeString()}` : "No snapshots yet"}
            </span>
            <ExportMenu
              run={run}
              results={results}
              network={networkGraph}
              configuration={run.config || advancedConfig || defaultAdvancedConfig}
              logTail={logTail}
            />
          </div>
        </div>

        <div className="metric-grid">
          <MetricCard label="Users" value={number(latest?.userCount)} accent="mint" />
          <MetricCard label="Actions" value={number(latest?.actionCount)} accent="gold" />
          <MetricCard label="Average quality" value={number(latest?.avgActionQuality, 3)} accent="blue" />
          <MetricCard label="Actions per user" value={number(avgActionsPerUser, 2)} accent="rose" />
        </div>

        {series.length === 0 && !loading && !isRunning ? (
          <EmptyState onRun={startRun} />
        ) : (
          <div className="dashboard-grid">
            <Panel className="network-panel" kicker="Interactive topology" title="User interaction network">
              <NetworkVisualization graph={networkGraph} loading={networkLoading} setup={run.setup} runStatus={run.status} />
            </Panel>

            <Panel className="trend-panel" kicker="Across snapshots" title="Action growth">
              <LineChart series={series} />
            </Panel>

            <Panel kicker="Behavior mix" title="What people did">
              <ActionDonut values={actionMix} />
            </Panel>

            <Panel kicker="Model signals" title="Quality & policy">
              <SignalRow label="Average action quality" value={latest?.avgActionQuality} />
              <SignalRow label="Policy coverage" value={policy.policy_coverage} />
              <SignalRow label="Intervention ratio" value={policy.policy_intervention_ratio} />
              <div className="convergence-row">
                <span className={`convergence-mark ${latest?.converged ? "is-converged" : ""}`} />
                <div>
                  <strong>{latest?.converged ? "Converged" : "Still evolving"}</strong>
                  <p>{latest?.convergenceReason || results.lifecycle?.analyticsStopReason || "No stop signal published."}</p>
                </div>
              </div>
            </Panel>

            <Panel kicker="Scale & time" title="Run context">
              <StatList rows={[
                ["Software runtime", duration(runtime.software_runtime_seconds)],
                ["Simulated runtime", duration(runtime.simulated_runtime_seconds)],
                ["Avg. friends / user", number(network.avg_friends_per_user, 2)],
                ["Avg. followers / user", number(network.avg_followers_per_user, 2)],
              ]} />
            </Panel>

            <Panel className="timing-panel" kicker="Simulated milliseconds" title="Inter-activity timing">
              <TimingBars values={timing} />
            </Panel>

            <Panel className="console-panel" kicker="Process output" title="Run console">
              <Console lines={run.output || []} />
            </Panel>

            <Panel className="log-panel" kicker={logTail.path || "logs/simsom.log"} title="Simulator log tail">
              <LogTail log={logTail} />
            </Panel>
          </div>
        )}
      </section>
    </main>
  );
}

function MetricCard({ label, value, accent }) {
  return (
    <article className={`metric-card metric-${accent}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <i />
    </article>
  );
}

function ExportMenu({ run, results, network, configuration, logTail }) {
  const menuRef = useRef(null);
  const hasAnalytics = Boolean(results?.series?.length);
  const hasNodes = Boolean(network?.nodes?.length);
  const hasEdges = Boolean(network?.edges?.length);

  function download(kind) {
    const bundle = buildExportBundle({ run, results, network, configuration, logTail });
    const baseName = exportBaseName(bundle);
    const exports = {
      html: [`${baseName}-report.html`, buildHtmlReport(bundle), "text/html"],
      analytics: [`${baseName}-analytics.csv`, analyticsCsv(results.series), "text/csv"],
      nodes: [`${baseName}-nodes.csv`, nodesCsv(network.nodes), "text/csv"],
      edges: [`${baseName}-edges.csv`, edgesCsv(network.edges), "text/csv"],
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
          title="Network nodes CSV"
          description="User attributes for statistical analysis"
          disabled={!hasNodes}
          onClick={() => download("nodes")}
        />
        <ExportOption
          title="Network edges CSV"
          description="Interaction and post attributes"
          disabled={!hasEdges}
          onClick={() => download("edges")}
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

function ExportOption({ title, description, disabled = false, onClick }) {
  return (
    <button type="button" disabled={disabled} onClick={onClick}>
      <strong>{title}</strong>
      <span>{description}</span>
    </button>
  );
}

function AdvancedConfig({ config, defaults, disabled, onChange, onReset }) {
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

  function setCriterionEnabled(criterion, checked) {
    const criteria = { ...termination.criteria_functions };
    if (checked) {
      criteria[criterion] = clone(defaults.termination.criteria_functions[criterion]);
    } else {
      delete criteria[criterion];
    }
    onChange(["termination", "criteria_functions"], criteria);
  }

  const criteria = termination.criteria_functions;
  const maxActions = criteria.max_avg_actions_per_user;
  const qualityEma = criteria.exponential_moving_average_action_quality;

  return (
    <section className="advanced-section">
      <header className="advanced-header">
        <div>
          <p className="eyebrow">Run-specific overrides</p>
          <h2>Advanced configuration</h2>
          <p>Adjust the model without editing the JSONC file. Unexposed infrastructure settings keep their defaults.</p>
        </div>
        <button className="button button-quiet reset-button" onClick={onReset} disabled={disabled}>Reset defaults</button>
      </header>

      <div className="config-grid">
        <ConfigGroup title="Population" subtitle="Who participates in the network">
          <div className="field-pair">
            <ConfigField label="Normal users" value={normal.num_nodes} min={0} max={1000000} step={100} onChange={(value) => onChange(["setup_db", "user_types", "normal", "num_nodes"], value)} disabled={disabled} />
            <ConfigField label="Adversarial users" value={bad.num_nodes} min={0} max={1000000} step={10} onChange={(value) => onChange(["setup_db", "user_types", "bad", "num_nodes"], value)} disabled={disabled} />
          </div>
          <div className="field-pair">
            <ConfigField label="Normal lurker ratio" value={normal.lurker_ratio} min={0} max={0.99} step={0.01} onChange={(value) => onChange(["setup_db", "user_types", "normal", "lurker_ratio"], value)} disabled={disabled} />
            <ConfigField label="Adversarial lurker ratio" value={bad.lurker_ratio} min={0} max={0.99} step={0.01} onChange={(value) => onChange(["setup_db", "user_types", "bad", "lurker_ratio"], value)} disabled={disabled} />
          </div>
          <div className="field-pair">
            <ConfigField label="Normal quality weight" value={normal.user_quality_distribution.w} min={0} max={1} step={0.05} onChange={(value) => onChange(["setup_db", "user_types", "normal", "user_quality_distribution", "w"], value)} disabled={disabled} />
            <ConfigField label="Adversarial quality weight" value={bad.user_quality_distribution.w} min={0} max={1} step={0.05} onChange={(value) => onChange(["setup_db", "user_types", "bad", "user_quality_distribution", "w"], value)} disabled={disabled} />
          </div>
          <details className="sub-settings">
            <summary>Distribution shape</summary>
            <div className="field-pair">
              <ConfigField label="Normal lambda slope" value={normal.user_quality_distribution.lambda_slope} min={0.001} step={0.001} onChange={(value) => onChange(["setup_db", "user_types", "normal", "user_quality_distribution", "lambda_slope"], value)} disabled={disabled} />
              <ConfigField label="Adversarial lambda slope" value={bad.user_quality_distribution.lambda_slope} min={0.001} step={0.001} onChange={(value) => onChange(["setup_db", "user_types", "bad", "user_quality_distribution", "lambda_slope"], value)} disabled={disabled} />
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
            <Toggle label="Stop at an actions-per-user limit" description="Use the maximum-average-actions criterion." checked={Boolean(maxActions)} onChange={(checked) => setCriterionEnabled("max_avg_actions_per_user", checked)} disabled={disabled || (Boolean(maxActions) && !qualityEma)} />
            {maxActions && (
              <ConfigField label="Actions per user threshold" value={maxActions.threshold} min={0} step="any" onChange={(value) => onChange(["termination", "criteria_functions", "max_avg_actions_per_user", "threshold"], value)} disabled={disabled} />
            )}
            <Toggle label="Stop when quality converges" description="Use the quality EMA convergence criterion." checked={Boolean(qualityEma)} onChange={(checked) => setCriterionEnabled("exponential_moving_average_action_quality", checked)} disabled={disabled || (Boolean(qualityEma) && !maxActions)} />
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
        <p>{kicker}</p>
        <h3>{title}</h3>
      </header>
      {children}
    </article>
  );
}

function NetworkVisualization({ graph, loading, setup, runStatus }) {
  const meta = graph.meta || {};
  const [selection, setSelection] = useState(null);
  const isLiveRun = runStatus === "running" || runStatus === "stopping";

  useEffect(() => setSelection(null), [graph]);

  if (!graph.available) {
    return <div className="network-visualization"><NetworkProgress setup={setup} runStatus={runStatus} /><div className="network-empty"><strong>Network unavailable</strong><p>{graph.message}</p></div></div>;
  }
  if (!graph.nodes?.length) {
    return <div className="network-visualization"><NetworkProgress setup={setup} runStatus={runStatus} /><div className="network-empty"><strong>No network rows yet</strong><p>Users and post interactions will appear as the simulation initializes.</p></div></div>;
  }

  return (
    <div className="network-visualization">
      <div className="network-meta">
        <div><strong>{number(meta.userCount)}</strong><span>users / nodes</span></div>
        <div><strong>{number(meta.returnedNodeCount ?? meta.userCount)}</strong><span>shown users / nodes</span></div>
        <div><strong>{number(meta.returnedEdgeCount)}</strong><span>shown interactions / edges</span></div>
        <div><strong>{number(meta.originalPostCount)}</strong><span>root posts</span></div>
        {loading && <em>Refreshing…</em>}
      </div>
      {meta.truncated && (
        <div className="network-sample-notice">
          {isLiveRun
            ? <>Live network snapshot. K-core filtering is currently applied at level k={number(meta.kCoreLevel)}. Showing {number(meta.returnedNodeCount)} users and {number(meta.returnedEdgeCount)} interactions. Final totals are unavailable until the simulation finishes.</>
            : <>Network generated successfully. K-core filtering was applied at level k={number(meta.kCoreLevel)}. Showing {number(meta.returnedNodeCount)} of {number(meta.userCount)} users and {number(meta.returnedEdgeCount)} of {number(meta.interactionCount)} interactions.</>}
        </div>
      )}
      <div className="network-workspace">
        <NetworkGraph graph={graph} onSelectionChange={setSelection} />
        <NetworkInspector graph={graph} selection={selection} />
      </div>
      <div className="network-footnote">
        <span>Drag to rotate · shift-drag to pan · scroll to zoom · select a node or edge for details</span>
        <span>
          Directed edges connect an acting user to the parent post author.
          {meta.truncated ? " Displaying the k-core-filtered network." : ""}
        </span>
      </div>
    </div>
  );
}

function NetworkProgress({ setup, runStatus }) {
  const isActive = runStatus === "running" || runStatus === "stopping";
  const current = Number(setup?.progress_current);
  const total = Number(setup?.progress_total);
  const hasProgress = Number.isFinite(current) && Number.isFinite(total) && total >= 0;
  const percentValue = hasProgress
    ? Math.max(0, Math.min(100, Number(setup.progress_percent ?? (total ? current * 100 / total : 100))))
    : 0;
  const stage = setup?.stage === "network_generation" ? "Generating network" : "Preparing network";
  const phase = setup?.phase ? ` · ${setup.phase.replaceAll("_", " ")}` : "";
  const detail = hasProgress
    ? `${number(current)} of ${number(total)} ${setup.progress_unit || "items"}${setup.network_total_users ? ` · ${number(setup.network_total_users)} total users` : ""}`
    : (isActive ? "Starting network generation…" : "Network generation is idle");

  if (!isActive && !hasProgress) return null;
  return (
    <div className="network-progress" aria-live="polite">
      <div className="network-progress-heading">
        <span><strong>{stage}</strong>{phase}</span>
        <span>{hasProgress ? `${number(percentValue, 1)}%` : "Working…"}</span>
      </div>
      <div className={`network-progress-track${hasProgress ? "" : " is-indeterminate"}`} role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={hasProgress ? percentValue : undefined}>
        <div className="network-progress-fill" style={{ width: hasProgress ? `${percentValue}%` : undefined }} />
      </div>
      <div className="network-progress-detail">{detail}{setup?.total_edges !== undefined ? ` · ${number(setup.total_edges)} edges generated` : ""}</div>
    </div>
  );
}

function NetworkInspector({ graph, selection }) {
  if (!selection) {
    return (
      <aside className="network-inspector network-inspector-empty">
        <p>Selection details</p>
        <strong>Choose a user or interaction</strong>
        <span>Click a node or edge in the network to inspect its stored attributes.</span>
      </aside>
    );
  }

  if (selection.kind === "node") {
    const node = selection.item;
    const outgoing = graph.edges.filter((edge) => edge.source === node.id);
    const incoming = graph.edges.filter((edge) => edge.target === node.id);
    const neighbors = new Set([
      ...outgoing.map((edge) => edge.target),
      ...incoming.map((edge) => edge.source),
    ]);
    neighbors.delete(node.id);
    return (
      <aside className="network-inspector">
        <InspectorHeader
          color={NETWORK_USER_COLORS[node.userType] || "#56b4e9"}
          kicker="Selected user"
          title={node.label || `User ${node.id}`}
          badge={node.userType === "bad" ? "adversarial" : node.userType}
        />
        <dl className="network-detail-list">
          <Detail label="User ID" value={number(node.id)} />
          <Detail label="Activity level" value={number(node.activityLevel, 3)} />
          <Detail label="User quality" value={number(node.quality, 3)} />
          <Detail label="Outgoing actions" value={number(outgoing.length)} />
          <Detail label="Incoming actions" value={number(incoming.length)} />
          <Detail label="Visible neighbors" value={number(neighbors.size)} />
        </dl>
        {graph.meta?.truncated && (
          <small>Connection counts reflect the interactions currently loaded into the graph.</small>
        )}
      </aside>
    );
  }

  const edge = selection.item;
  const actor = graph.nodes.find((node) => node.id === edge.source);
  const parentAuthor = graph.nodes.find((node) => node.id === edge.target);
  const moderation = moderationSummary(edge.contentModerationActions);
  return (
    <aside className="network-inspector">
      <InspectorHeader
        color={NETWORK_ACTION_COLORS[edge.actionType] || "#b7c4ce"}
        kicker="Selected interaction"
        title={`${edge.actionType} #${edge.id}`}
        badge={edge.actionType}
      />
      <dl className="network-detail-list">
        <Detail label="Actor" value={actor?.label || `User ${edge.source}`} />
        <Detail label="Parent author" value={parentAuthor?.label || `User ${edge.target}`} />
        <Detail label="Parent action" value={edge.parentActionId == null ? "—" : `#${edge.parentActionId}`} />
        <Detail label="Content action" value={edge.contentActionId == null ? "—" : `#${edge.contentActionId}`} />
        <Detail label="Simulation tick" value={number(edge.timestamp)} />
        <Detail label="Content quality" value={number(edge.quality, 3)} />
        <Detail label="Content appeal" value={number(edge.appeal, 3)} />
        <Detail label="Content views" value={number(edge.contentViewCount)} />
        <Detail label="Repeat actors" value={number(edge.contentRepeatActorCount)} />
        <Detail label="Embedding dimensions" value={number(edge.contentEmbeddingDimensions)} />
        <Detail label="Moderation" value={moderation} wide />
        <Detail label="Session" value={edge.sessionId || "—"} wide />
      </dl>
    </aside>
  );
}

function InspectorHeader({ color, kicker, title, badge }) {
  return (
    <header className="network-inspector-header">
      <i style={{ background: color }} />
      <div><p>{kicker}</p><h4>{title}</h4></div>
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

function moderationSummary(actions) {
  if (!actions || typeof actions !== "object") return "None";
  const intervention = actions.intervention_type;
  const violation = actions.violation_type;
  if (intervention) return violation ? `${intervention} · ${violation}` : intervention;
  if (actions.policy_reviewed === true) return "Reviewed · no intervention";
  if (actions.policy_reviewed === false) return "Not reviewed";
  return Object.keys(actions).length ? "Metadata available" : "None";
}

function EmptyState({ onRun }) {
  return (
    <div className="empty-state">
      <div className="empty-orbit"><span /><i /><b /></div>
      <h3>Your first run will appear here</h3>
      <p>Start the simulator to populate live analytics and a final summary.</p>
      <button className="button button-primary" onClick={onRun}>Run simulator</button>
    </div>
  );
}

function LineChart({ series }) {
  const width = 760;
  const height = 260;
  const inset = 22;
  const values = series.map((point) => Number(point.actionCount) || 0);
  const max = Math.max(...values, 1);
  const points = values.map((value, index) => {
    const x = series.length === 1 ? width / 2 : inset + (index / (series.length - 1)) * (width - inset * 2);
    const y = height - inset - (value / max) * (height - inset * 2);
    return [x, y];
  });
  const line = points.map(([x, y]) => `${x},${y}`).join(" ");
  const area = points.length ? `${points[0][0]},${height - inset} ${line} ${points.at(-1)[0]},${height - inset}` : "";

  return (
    <div className="line-chart">
      <div className="chart-summary">
        <strong>{number(values.at(-1))}</strong>
        <span>total actions</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Action count over analytics snapshots">
        <defs>
          <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#56b4e9" stopOpacity="0.28" />
            <stop offset="100%" stopColor="#56b4e9" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75, 1].map((ratio) => (
          <line key={ratio} x1={inset} x2={width - inset} y1={height * ratio - 4} y2={height * ratio - 4} className="grid-line" />
        ))}
        {points.length > 0 && <polygon points={area} fill="url(#areaFill)" />}
        <polyline points={line} className="trend-line" />
        {points.map(([x, y], index) => <circle key={index} cx={x} cy={y} r="4" className="trend-dot" />)}
      </svg>
      <div className="chart-axis"><span>First snapshot</span><span>Latest</span></div>
    </div>
  );
}

function ActionDonut({ values }) {
  const entries = Object.entries(values).map(([label, value]) => ({
    label,
    value: Number(value),
    color: NETWORK_ACTION_COLORS[label] || "#56b4e9",
  })).filter((entry) => Number.isFinite(entry.value));
  let cursor = 0;
  const stops = entries.map((entry) => {
    const start = cursor;
    cursor += entry.value * 100;
    return `${entry.color} ${start}% ${cursor}%`;
  });

  if (!entries.length) return <p className="muted-copy">Action mix will appear after the first analytics snapshot.</p>;
  return (
    <div className="donut-layout">
      <div className="donut" style={{ background: `conic-gradient(${stops.join(", ")})` }}>
        <div><strong>{entries.length}</strong><span>action types</span></div>
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

function TimingBars({ values }) {
  const entries = ["min", "p50", "mean", "p90", "p99", "max"].map((key) => [key, values[key]]);
  const max = Math.max(...entries.map(([, value]) => Number(value) || 0), 1);
  return (
    <div className="timing-bars">
      {entries.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <i><b style={{ width: `${((Number(value) || 0) / max) * 100}%` }} /></i>
          <strong>{value === null || value === undefined ? "—" : `${number(value, 1)} ms`}</strong>
        </div>
      ))}
    </div>
  );
}

function LogTail({ log }) {
  const updatedAt = log.modifiedAt ? new Date(log.modifiedAt).toLocaleTimeString() : null;
  return (
    <div className="log-tail">
      <div className="log-tail-meta">
        <span>{log.available === false ? "Log unavailable" : `${number(log.lines?.length || 0)} latest lines`}</span>
        <span>{updatedAt ? `Updated ${updatedAt}` : "Waiting for log output"}</span>
      </div>
      <Console
        lines={log.lines || []}
        emptyText={log.message || "$ Waiting for logs/simsom.log…"}
        className="log-tail-output"
      />
    </div>
  );
}

function Console({ lines, emptyText = "$ Waiting for a simulation to start…", className = "" }) {
  const outputRef = useRef(null);
  const shouldFollowOutputRef = useRef(true);

  useEffect(() => {
    const output = outputRef.current;
    if (output && shouldFollowOutputRef.current) {
      output.scrollTop = output.scrollHeight;
    }
  }, [lines]);

  function updateOutputFollowing(event) {
    const output = event.currentTarget;
    const distanceFromBottom = output.scrollHeight - output.scrollTop - output.clientHeight;
    shouldFollowOutputRef.current = distanceFromBottom <= 24;
  }

  return (
    <pre
      ref={outputRef}
      className={`console-output ${className}`.trim()}
      onScroll={updateOutputFollowing}
    >
      {lines.length ? lines.join("\n") : emptyText}
    </pre>
  );
}

export default App;
