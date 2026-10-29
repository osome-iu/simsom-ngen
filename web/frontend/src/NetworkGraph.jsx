import { useEffect, useRef, useState } from "react";

// Okabe-Ito-inspired colors chosen to stay distinguishable for common forms
// of color-vision deficiency. The legend labels remain visible so color is
// not the only cue for a user or interaction type.
export const NETWORK_USER_COLORS = {
  normal: "#56b4e9", // sky blue
  bad: "#e69f00", // orange
};

const USER_TYPE_CODES = Object.fromEntries(
  Object.keys(NETWORK_USER_COLORS).map((type, index) => [type, index]),
);
const USER_TYPE_DOMAIN = Object.values(USER_TYPE_CODES);

const ACTION_TYPE_COLORS = {
  like: "#e69f00ff", // orange
  post: "#56b4e9ff", // sky blue
  repost: "#0072b2ff", // blue
  reply: "#cc79a7ff", // purple
  quote: "#009e73ff", // bluish green
};

const ACTION_TYPE_CODES = Object.fromEntries(
  Object.keys(ACTION_TYPE_COLORS).map((type, index) => [type, index]),
);
const ACTION_TYPE_DOMAIN = Object.values(ACTION_TYPE_CODES);

export const NETWORK_ACTION_COLORS = Object.fromEntries(
  Object.entries(ACTION_TYPE_COLORS).map(([key, value]) => [key, value.slice(0, 7)]),
);

const DEFAULT_LINK_DISTANCE = 5;
const DEFAULT_NODE_SIZE_SCALE = 1.35;
const DEFAULT_EDGE_WIDTH_SCALE = 0.24;
const FILTERED_OUT_NODE_OPACITY = 0.22;

function NetworkGraph({ graph, onSelectionChange }) {
  const containerRef = useRef(null);
  const heliosRef = useRef(null);
  const networkRef = useRef(null);
  const graphRef = useRef(null);
  const nodeIndexesRef = useRef(new Map());
  const edgeIndexesRef = useRef(new Map());
  const nodesByIndexRef = useRef(new Map());
  const edgesByIndexRef = useRef(new Map());
  const latestGraphRef = useRef(graph);
  const legendIndexesRef = useRef({ nodes: new Map(), edges: new Map() });
  const linkDistanceRef = useRef(DEFAULT_LINK_DISTANCE);
  const nodeSizeScaleRef = useRef(DEFAULT_NODE_SIZE_SCALE);
  const edgeWidthScaleRef = useRef(DEFAULT_EDGE_WIDTH_SCALE);
  const [renderError, setRenderError] = useState("");
  const [ready, setReady] = useState(false);
  const [linkDistance, setLinkDistance] = useState(DEFAULT_LINK_DISTANCE);
  const [nodeSizeScale, setNodeSizeScale] = useState(DEFAULT_NODE_SIZE_SCALE);
  const [edgeWidthScale, setEdgeWidthScale] = useState(DEFAULT_EDGE_WIDTH_SCALE);
  const [activeNodeTypes, setActiveNodeTypes] = useState([]);
  const [activeEdgeTypes, setActiveEdgeTypes] = useState([]);

  latestGraphRef.current = graph;

  function updateExistingGraph(nextGraph) {
    const helios = heliosRef.current;
    const network = networkRef.current;
    const previousGraph = graphRef.current;
    if (!helios || !network || !previousGraph) return false;

    const nextNodes = nextGraph.nodes || [];
    const nextEdges = nextGraph.edges || [];
    // The API returns a rolling edge window. Keep the existing Helios graph
    // while that window grows; a later full rebuild handles window eviction.
    if (
      previousGraph.nodes.some((node) => !nextNodes.some((candidate) => candidate.id === node.id))
      || previousGraph.edges.some((edge) => !nextEdges.some((candidate) => candidate.id === edge.id))
    ) {
      return false;
    }

    const addedNodes = nextNodes.filter((node) => !nodeIndexesRef.current.has(node.id));
    if (addedNodes.length) {
      // Use Helios' public mutators so its visual attribute buffers are
      // initialized before the renderer sees the new topology.
      const indexes = helios.addNodes(addedNodes.length);
      addedNodes.forEach((node, index) => nodeIndexesRef.current.set(node.id, Number(indexes[index])));
    }

    const addedEdges = nextEdges.filter(
      (edge) => !edgeIndexesRef.current.has(edge.id)
        && nodeIndexesRef.current.has(edge.source)
        && nodeIndexesRef.current.has(edge.target),
    );
    if (addedEdges.length) {
      const edgeIndexes = helios.addEdges(
        addedEdges.map((edge) => [nodeIndexesRef.current.get(edge.source), nodeIndexesRef.current.get(edge.target)]),
      );
      addedEdges.forEach((edge, index) => edgeIndexesRef.current.set(edge.id, Number(edgeIndexes[index])));
    }

    const allNodes = [...previousGraph.nodes, ...addedNodes];
    const allEdges = [...previousGraph.edges, ...addedEdges];
    const currentNodes = allNodes.map(
      (node) => nextNodes.find((candidate) => candidate.id === node.id) || node,
    );
    const currentEdges = allEdges.map(
      (edge) => nextEdges.find((candidate) => candidate.id === edge.id) || edge,
    );
    writeGraphAttributes(
      network,
      currentNodes,
      currentEdges,
      nodeIndexesRef.current,
      edgeIndexesRef.current,
    );
    nodesByIndexRef.current = itemsByNetworkIndex(currentNodes, nodeIndexesRef.current);
    edgesByIndexRef.current = itemsByNetworkIndex(currentEdges, edgeIndexesRef.current);
    if (currentEdges.length) {
      helios.behavior.mappers.setChannelConfig("edge", "color", {
        type: "categorical",
        attributes: "actionTypeCode",
        domain: ACTION_TYPE_DOMAIN,
        range: Object.values(ACTION_TYPE_COLORS),
        defaultValue: "#b7c4ceff",
      });
      helios.behavior.mappers.setChannelConfig("edge", "width", {
        type: "linear",
        attributes: "quality",
        domain: [0, 1],
        range: [1.1, 3.2],
        defaultValue: 1.2,
      });
    }

    graphRef.current = {
      ...nextGraph,
      nodes: currentNodes,
      edges: currentEdges,
    };
    legendIndexesRef.current = {
      nodes: groupIndexes(
        currentNodes,
        currentNodes.map((node) => nodeIndexesRef.current.get(node.id)),
        "userType",
        network.nodeCount,
      ),
      edges: groupIndexes(
        currentEdges,
        currentEdges.map((edge) => edgeIndexesRef.current.get(edge.id)),
        "actionType",
        network.edgeCount,
      ),
    };
    helios._clearHighlightSource?.("dashboard:legend-hover");
    applyLegendSelection(helios, "nodes", activeNodeTypes);
    applyLegendSelection(helios, "edges", activeEdgeTypes);
    helios.behavior.layout.reheat("dashboard-network-update");
    return true;
  }

  useEffect(() => {
    let cancelled = false;
    let helios = null;
    let network = null;
    let frameTimer = null;
    const eventUnsubscribers = [];

    function disposeGraph() {
      for (const unsubscribe of eventUnsubscribers.splice(0)) unsubscribe?.();
      const activeHelios = helios || heliosRef.current;
      const activeNetwork = network || networkRef.current;
      if (activeHelios) {
        activeHelios._clearHighlightSource?.("dashboard:legend-hover");
        activeHelios._clearHighlightSource?.("dashboard:node-legend-click");
        activeHelios._clearHighlightSource?.("dashboard:edge-legend-click");
        activeHelios.destroy();
        helios = null;
        network = null;
        heliosRef.current = null;
        networkRef.current = null;
      } else if (activeNetwork) {
        activeNetwork.dispose();
        network = null;
      }
      nodeIndexesRef.current.clear();
      edgeIndexesRef.current.clear();
      nodesByIndexRef.current.clear();
      edgesByIndexRef.current.clear();
      legendIndexesRef.current = { nodes: new Map(), edges: new Map() };
      graphRef.current = null;
    }

    async function mountGraph() {
      if (updateExistingGraph(graph)) {
        setRenderError("");
        return;
      }
      if (heliosRef.current || networkRef.current) disposeGraph();
      setReady(false);
      setRenderError("");
      setActiveNodeTypes([]);
      setActiveEdgeTypes([]);
      onSelectionChange?.(null);
      const container = containerRef.current;
      if (!container || !graph?.nodes?.length) return;

      try {
        const [{ default: HeliosNetwork }, { Helios, EVENTS }] = await Promise.all([
          import("helios-network"),
          import("helios-web"),
        ]);
        if (cancelled) return;

        const nodeIndex = new Map(graph.nodes.map((node, index) => [node.id, index]));
        const visibleEdges = graph.edges.filter(
          (edge) => nodeIndex.has(edge.source) && nodeIndex.has(edge.target),
        );

        network = await HeliosNetwork.create({
          directed: true,
          initialNodes: graph.nodes.length,
          initialEdges: visibleEdges.length,
        });
        if (cancelled) {
          network.dispose();
          if (networkRef.current === network) networkRef.current = null;
          network = null;
          return;
        }
        networkRef.current = network;
        const edgeIds = visibleEdges.length
          ? network.addEdges(
              visibleEdges.map((edge) => [nodeIndex.get(edge.source), nodeIndex.get(edge.target)]),
            )
          : [];
        legendIndexesRef.current = {
          nodes: groupIndexes(graph.nodes, graph.nodes.map((_, index) => index), "userType", graph.nodes.length),
          edges: groupIndexes(visibleEdges, edgeIds, "actionType", visibleEdges.length),
        };
        nodeIndexesRef.current = new Map(graph.nodes.map((node, index) => [node.id, index]));
        edgeIndexesRef.current = new Map(
          visibleEdges.map((edge, index) => [edge.id, Number(edgeIds[index])]),
        );
        writeGraphAttributes(
          network,
          graph.nodes,
          visibleEdges,
          nodeIndexesRef.current,
          edgeIndexesRef.current,
        );
        nodesByIndexRef.current = itemsByNetworkIndex(graph.nodes, nodeIndexesRef.current);
        edgesByIndexRef.current = itemsByNetworkIndex(visibleEdges, edgeIndexesRef.current);

        if (cancelled) {
          network.dispose();
          network = null;
          return;
        }

        helios = new Helios(network, {
          container,
          mode: "3d",
          ui: false,
          quickControls: false,
          legends: { enabled: false },
          transparencyModeEdges: "alpha",
          clearColor: "#09101c",
          // Helios promotes selected/highlighted items in its render buffers by
          // default. Keeping the native order fixed prevents categorical edge
          // colors and picking IDs from drifting during legend interaction.
          interactionRenderOrder: false,
        });
        await helios.ready;
        if (cancelled) {
          helios.destroy();
          if (heliosRef.current === helios) heliosRef.current = null;
          if (networkRef.current === network) networkRef.current = null;
          network?.dispose?.();
          helios = null;
          network = null;
          return;
        }

        helios.behavior.layout.parameters({
          linkDistance: linkDistanceRef.current,
          kRepulsion: 2.2,
          kAttraction: 0.25,
          kGravity: 0.0003,
        });
        helios.behavior.layout.reheat("dashboard-initial-spacing");

        const userCategories = {
          domain: USER_TYPE_DOMAIN,
          range: Object.values(NETWORK_USER_COLORS).map(toRgbaHex),
        };
        const actionCategories = {
          domain: ACTION_TYPE_DOMAIN,
          range: Object.values(ACTION_TYPE_COLORS),
        };
        // Do not spread a large node array into Math.max. With a capped
        // 50,000-edge sample this can still be tens of thousands of nodes,
        // which exceeds JavaScript's function-argument limit.
        const maxActivity = graph.nodes.reduce(
          (maximum, node) => Math.max(maximum, Number(node.activityLevel) || 0),
          1,
        );

        helios.behavior.mappers.setChannelConfig("node", "color", {
          type: "categorical",
          attributes: "userTypeCode",
          domain: userCategories.domain,
          range: userCategories.range,
          defaultValue: "#b7c4ceff",
        });
        helios.behavior.mappers.setChannelConfig("node", "size", {
          type: "linear",
          attributes: "activityLevel",
          domain: [0, maxActivity],
          range: [2, 9],
          defaultValue: 3,
        });
        if (visibleEdges.length) {
          helios.behavior.mappers.setChannelConfig("edge", "color", {
            type: "categorical",
            attributes: "actionTypeCode",
            domain: actionCategories.domain,
            range: actionCategories.range,
            defaultValue: "#b7c4ceff",
          });
          helios.behavior.mappers.setChannelConfig("edge", "width", {
            type: "linear",
            attributes: "quality",
            domain: [0, 1],
            range: [1.1, 3.2],
            defaultValue: 1.2,
          });
        }
        helios.behavior.appearance.appearance({
          background: "#09101c",
          nodeSizeScale: nodeSizeScaleRef.current,
          nodeOpacityScale: 1,
          edgeWidthScale: edgeWidthScaleRef.current,
          edgeOpacityScale: 0.48,
          shadedEnabled: true,
          shadedNodes: true,
          shadedEdges: false,
        });
        helios.behavior.labels.labels({
          enabled: true,
          source: "label",
          selectionMode: "selected-only",
          maxVisible: 40,
          fill: "#eef4f1",
          outlineColor: "#09101c",
        });
        helios.useBehavior("selection", {
          nodeClick: true,
          edgeClick: true,
          selectedConnectedEdges: true,
          pickingResolutionScale: 1,
          pickingMaxFps: 30,
        });
        helios.nodeStateStyle("HIGHLIGHTED", {
          sizeMul: 1,
          opacityMul: 1,
          outlineMul: 1,
          discard: false,
          forceMaxAlpha: true,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0.14, 0.14, 0.14, 0],
        });
        // Legend highlighting should emphasize edges without replacing the
        // action-type color supplied by the categorical edge mapper.
        helios.edgeStateStyle("HIGHLIGHTED", {
          widthMul: 1.35,
          opacityMul: 1,
          discard: false,
          forceMaxAlpha: true,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0, 0, 0, 0],
        });
        helios.edgeStateStyle("SELECTED", {
          widthMul: 1.5,
          opacityMul: 1,
          discard: false,
          forceMaxAlpha: true,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0, 0, 0, 0],
        });
        helios.edgeHoverStyle({
          widthMul: 1.35,
          opacityMul: 1,
          discard: false,
          forceMaxAlpha: true,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0, 0, 0, 0],
        });
        helios.nodeStateStyle("SELECTED", {
          sizeMul: 1,
          opacityMul: 1,
          outlineMul: 1,
          discard: false,
          forceMaxAlpha: true,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0.18, 0.18, 0.18, 0],
        });
        helios.getBehavior("hover")?.update({
          otherHighlightNodeStyle: unhighlightedNodeStyle(1),
        });
        // SelectionBehavior intentionally uses a low-resolution picking buffer
        // by default. Use full resolution and tolerate a small amount of
        // pointer movement so dense nodes and thin edges remain clickable.
        const pickingOptions = {
          resolutionScale: 1,
          maxFps: 30,
          hoverEnabled: false,
          clickMoveTolerancePx: 8,
          suppressClickAfterWheelMs: 80,
        };
        helios.enableNodePicking(pickingOptions);
        helios.enableEdgePicking(pickingOptions);
        eventUnsubscribers.push(
          helios.on(EVENTS.NODE_CLICK, ({ detail }) => {
            const node = nodesByIndexRef.current.get(Number(detail?.index));
            if (node) onSelectionChange?.({ kind: "node", item: node });
          }),
          helios.on(EVENTS.EDGE_CLICK, ({ detail }) => {
            const edge = edgesByIndexRef.current.get(Number(detail?.index));
            if (edge) onSelectionChange?.({ kind: "edge", item: edge });
          }),
          helios.on(EVENTS.GRAPH_CLICK, ({ detail }) => {
            if (detail?.kind !== "node" && detail?.kind !== "edge") {
              onSelectionChange?.(null);
            }
          }),
        );
        helios.frameNetwork({ animate: false });
        frameTimer = window.setTimeout(
          () => {
            if (heliosRef.current === helios) helios.frameNetwork({ animate: true });
          },
          1800,
        );
        heliosRef.current = helios;
        graphRef.current = { ...graph, edges: visibleEdges };
        setReady(true);
      } catch (error) {
        disposeGraph();
        if (!cancelled) setRenderError(error.message || "Helios could not render this graph.");
      }
    }

    mountGraph();
    return () => {
      cancelled = true;
      if (latestGraphRef.current !== graph) return;
      if (frameTimer) window.clearTimeout(frameTimer);
      if (heliosRef.current === helios) heliosRef.current = null;
      disposeGraph();
    };
  }, [graph, onSelectionChange]);

  function updateLinkDistance(event) {
    const value = Number(event.target.value);
    linkDistanceRef.current = value;
    setLinkDistance(value);
    const helios = heliosRef.current;
    helios?.behavior.layout.parameter("linkDistance", value);
    helios?.behavior.layout.reheat("dashboard-spacing");
  }

  function updateNodeSize(event) {
    const value = Number(event.target.value);
    nodeSizeScaleRef.current = value;
    setNodeSizeScale(value);
    heliosRef.current?.behavior.appearance.appearance({ nodeSizeScale: value });
  }

  function updateEdgeWidth(event) {
    const value = Number(event.target.value);
    edgeWidthScaleRef.current = value;
    setEdgeWidthScale(value);
    heliosRef.current?.behavior.appearance.appearance({ edgeWidthScale: value });
  }

  function highlightLegendCategory(kind, type) {
    const helios = heliosRef.current;
    const ids = legendIndexesRef.current[kind]?.get(type) || [];
    if (!helios) return;
    if (!ids.length) {
      helios._clearHighlightSource?.("dashboard:legend-hover");
      return;
    }
    helios._setHighlightSource?.(
      "dashboard:legend-hover",
      kind === "nodes" ? { nodes: ids } : { edges: ids },
    );
  }

  function clearLegendHover() {
    heliosRef.current?._clearHighlightSource?.("dashboard:legend-hover");
  }

  function toggleLegendCategory(kind, type, additive) {
    const isNode = kind === "nodes";
    const active = isNode ? activeNodeTypes : activeEdgeTypes;
    const next = additive
      ? active.includes(type)
        ? active.filter((entry) => entry !== type)
        : [...active, type]
      : active.length === 1 && active[0] === type
        ? []
        : [type];
    if (isNode) setActiveNodeTypes(next);
    else setActiveEdgeTypes(next);

    const helios = heliosRef.current;
    helios?._clearHighlightSource?.("dashboard:legend-hover");
    if (isNode) {
      helios?.getBehavior("hover")?.update({
        otherHighlightNodeStyle: unhighlightedNodeStyle(
          next.length ? FILTERED_OUT_NODE_OPACITY : 1,
        ),
      });
    }
    applyLegendSelection(helios, kind, next);
  }

  function applyLegendSelection(helios, kind, types) {
    const isNode = kind === "nodes";
    const source = isNode
      ? "dashboard:node-legend-click"
      : "dashboard:edge-legend-click";
    const ids = types.flatMap(
      (entry) => legendIndexesRef.current[kind]?.get(entry) || [],
    );
    if (ids.length) {
      helios?._setHighlightSource?.(source, isNode ? { nodes: ids } : { edges: ids });
    } else {
      helios?._clearHighlightSource?.(source);
    }
  }

  const userTypes = [...new Set(graph.nodes.map((node) => node.userType))];
  const actionTypes = [...new Set(graph.edges.map((edge) => edge.actionType))];

  return (
    <div className="network-stage">
      <div ref={containerRef} className="helios-container" />
      {!ready && !renderError && <div className="network-overlay">Preparing Helios renderer…</div>}
      {renderError && <div className="network-overlay network-error">{renderError}</div>}
      {ready && (
        <>
          <div className="network-frame-legend">
            <LegendGroup
              title="User type"
              types={userTypes}
              colors={NETWORK_USER_COLORS}
              fallback="#56b4e9"
              activeTypes={activeNodeTypes}
              onEnter={(type) => highlightLegendCategory("nodes", type)}
              onLeave={clearLegendHover}
              onToggle={(type, additive) => toggleLegendCategory("nodes", type, additive)}
              formatLabel={(type) => type === "bad" ? "adversarial" : type}
            />
            <LegendGroup
              title="Interaction type"
              types={actionTypes}
              colors={NETWORK_ACTION_COLORS}
              fallback="#b7c4ce"
              activeTypes={activeEdgeTypes}
              onEnter={(type) => highlightLegendCategory("edges", type)}
              onLeave={clearLegendHover}
              onToggle={(type, additive) => toggleLegendCategory("edges", type, additive)}
            />
          </div>
          <div className="network-controls">
            <label>
              <span>Link distance</span>
              <input
                type="range"
                min="1"
                max="12"
                step="0.5"
                value={linkDistance}
                onChange={updateLinkDistance}
              />
              <output>{linkDistance.toFixed(1)}</output>
            </label>
            <label>
              <span>Node size</span>
              <input
                type="range"
                min="0.5"
                max="3"
                step="0.05"
                value={nodeSizeScale}
                onChange={updateNodeSize}
              />
              <output>{nodeSizeScale.toFixed(2)}</output>
            </label>
            <label>
              <span>Edge thickness</span>
              <input
                type="range"
                min="0.05"
                max="1"
                step="0.05"
                value={edgeWidthScale}
                onChange={updateEdgeWidth}
              />
              <output>{edgeWidthScale.toFixed(2)}</output>
            </label>
          </div>
        </>
      )}
    </div>
  );
}

function LegendGroup({
  title,
  types,
  colors,
  fallback,
  activeTypes,
  onEnter,
  onLeave,
  onToggle,
  formatLabel = (type) => type,
}) {
  return (
    <div className="network-frame-legend-group">
      <strong>{title}</strong>
      {types.map((type) => (
        <button
          type="button"
          key={type}
          className={activeTypes.includes(type) ? "is-active" : ""}
          aria-pressed={activeTypes.includes(type)}
          title="Click to highlight this type; Shift-click to combine types"
          onPointerDown={(event) => event.stopPropagation()}
          onPointerUp={(event) => event.stopPropagation()}
          onWheel={(event) => event.stopPropagation()}
          onPointerEnter={() => onEnter(type)}
          onPointerLeave={onLeave}
          onClick={(event) => {
            event.stopPropagation();
            onToggle(type, event.shiftKey);
          }}
        >
          <i style={{ background: colors[type] || fallback }} />
          <span>{formatLabel(type)}</span>
        </button>
      ))}
    </div>
  );
}

function groupIndexes(items, indexes, attribute, count) {
  const groups = new Map();
  items.forEach((item, position) => {
    const type = item[attribute];
    const index = Number(indexes[position]);
    if (!Number.isInteger(index) || index < 0 || (Number.isInteger(count) && index >= count)) return;
    const group = groups.get(type) || [];
    group.push(index);
    groups.set(type, group);
  });
  return groups;
}

function writeGraphAttributes(network, nodes, edges, nodeIndexes, edgeIndexes) {
  const nodesByIndex = itemsByNetworkIndex(nodes, nodeIndexes);
  const edgesByIndex = itemsByNetworkIndex(edges, edgeIndexes);

  network.nodeAttribute("label", (current, index) => nodesByIndex.get(index)?.label ?? current ?? "");
  network.nodeAttribute("userId", (current, index) => nodesByIndex.get(index)?.id ?? current ?? 0);
  network.nodeAttribute("userType", (current, index) => nodesByIndex.get(index)?.userType ?? current ?? "unknown");
  network.nodeAttribute(
    "userTypeCode",
    (current, index) => {
      const node = nodesByIndex.get(index);
      return node ? USER_TYPE_CODES[node.userType] ?? -1 : current ?? -1;
    },
  );
  network.nodeAttribute(
    "activityLevel",
    (current, index) => finiteNumber(nodesByIndex.get(index)?.activityLevel, current),
  );
  network.nodeAttribute(
    "quality",
    (current, index) => finiteNumber(nodesByIndex.get(index)?.quality, current),
  );

  if (!edges.length) return;
  network.edgeAttribute("label", (current, index) => {
    const edge = edgesByIndex.get(index);
    return edge ? edgeLabel(edge) : current ?? "";
  });
  network.edgeAttribute("actionId", (current, index) => edgesByIndex.get(index)?.id ?? current ?? 0);
  network.edgeAttribute(
    "actionType",
    (current, index) => edgesByIndex.get(index)?.actionType ?? current ?? "unknown",
  );
  network.edgeAttribute(
    "actionTypeCode",
    (current, index) => {
      const edge = edgesByIndex.get(index);
      return edge ? ACTION_TYPE_CODES[edge.actionType] ?? -1 : current ?? -1;
    },
  );
  network.edgeAttribute(
    "quality",
    (current, index) => finiteNumber(edgesByIndex.get(index)?.quality, current),
  );
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  if (Number.isFinite(number)) return number;
  const fallbackNumber = Number(fallback);
  return Number.isFinite(fallbackNumber) ? fallbackNumber : 0;
}

function itemsByNetworkIndex(items, indexes) {
  const indexed = new Map();
  for (const item of items) {
    const index = Number(indexes.get(item.id));
    if (Number.isInteger(index) && index >= 0) indexed.set(index, item);
  }
  return indexed;
}

function unhighlightedNodeStyle(opacityMul) {
  return {
    sizeMul: 1,
    opacityMul,
    outlineMul: 1,
    discard: false,
    forceMaxAlpha: false,
    colorMul: [1, 1, 1, 1],
    colorAdd: [0, 0, 0, 0],
  };
}

function edgeLabel(edge) {
  return `${edge.actionType} #${edge.id} · User ${edge.source} → User ${edge.target}`;
}

function toRgbaHex(color) {
  return color.length === 7 ? `${color}ff` : color;
}

export default NetworkGraph;
