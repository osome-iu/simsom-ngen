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
  like: "#e69f0080", // orange, reduced alpha because likes are especially dense
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
const DEFAULT_NODE_SIZE_SCALE = 1;
const DEFAULT_EDGE_WIDTH_SCALE = 0.24;
const FILTERED_OUT_NODE_OPACITY = 0.22;
const EDGE_FILTERED_NODE_OPACITY = 0.3;
const DEFAULT_EDGE_OPACITY_SCALE = 0.3;
const CONNECTED_NETWORK_FRAME_COVERAGE = 0.85;
const CONNECTED_NETWORK_FRAME_PADDING = 0.02;
const CAMERA_ZOOM_FACTOR = 1.4;

function NetworkGraph({ graph, selection, onSelectionChange }) {
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
  const hiddenEdgeTypesRef = useRef(new Set());
  const filteredNodeIndexesRef = useRef(new Set());
  const [renderError, setRenderError] = useState("");
  const [ready, setReady] = useState(false);
  const [linkDistance, setLinkDistance] = useState(DEFAULT_LINK_DISTANCE);
  const [nodeSizeScale, setNodeSizeScale] = useState(DEFAULT_NODE_SIZE_SCALE);
  const [edgeWidthScale, setEdgeWidthScale] = useState(DEFAULT_EDGE_WIDTH_SCALE);
  const [activeNodeTypes, setActiveNodeTypes] = useState([]);
  const [hiddenEdgeTypes, setHiddenEdgeTypes] = useState([]);

  latestGraphRef.current = graph;

  useEffect(() => {
    if (selection) return;
    const helios = heliosRef.current;
    helios?.getBehavior("selection")?.clearSelection();
    helios?.getBehavior("hover")?.clearNodeHover({ silent: true, force: true });
    helios?.getBehavior("hover")?.clearEdgeHover({ silent: true, force: true });
  }, [selection]);

  function updateExistingGraph(nextGraph) {
    const helios = heliosRef.current;
    const network = networkRef.current;
    const previousGraph = graphRef.current;
    if (!helios || !network || !previousGraph) return false;

    const nextNodes = nextGraph.nodes || [];
    const nextEdges = nextGraph.edges || [];
    const nextNodesById = new Map(nextNodes.map((node) => [node.id, node]));
    const nextEdgesById = new Map(nextEdges.map((edge) => [edge.id, edge]));
    // The API returns a rolling edge window. Keep the existing Helios graph
    // while that window grows; a later full rebuild handles window eviction.
    if (
      previousGraph.nodes.some((node) => !nextNodesById.has(node.id))
      || previousGraph.edges.some((edge) => !nextEdgesById.has(edge.id))
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
      (node) => nextNodesById.get(node.id) ?? node,
    );
    const currentEdges = allEdges.map(
      (edge) => nextEdgesById.get(edge.id) ?? edge,
    );
    writeGraphAttributes(
      network,
      currentNodes,
      currentEdges,
      nodeIndexesRef.current,
      edgeIndexesRef.current,
      hiddenEdgeTypesRef.current,
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
    configureVisibilityMappers(helios);
    filteredNodeIndexesRef.current = updateFilteredNodeStates(
      helios,
      currentNodes,
      currentEdges,
      nodeIndexesRef.current,
      hiddenEdgeTypesRef.current,
      filteredNodeIndexesRef.current,
    );
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
      filteredNodeIndexesRef.current.clear();
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
      hiddenEdgeTypesRef.current = new Set();
      filteredNodeIndexesRef.current = new Set();
      setHiddenEdgeTypes([]);
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
          hiddenEdgeTypesRef.current,
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

        const actionCategories = {
          domain: ACTION_TYPE_DOMAIN,
          range: Object.values(ACTION_TYPE_COLORS),
        };
        // Do not spread a large node array into Math.max. With a capped
        // 50,000-node sample this can still exceed JavaScript's
        // function-argument limit.
        const maxActivity = graph.nodes.reduce(
          (maximum, node) => Math.max(maximum, Number(node.activityLevel) || 0),
          1,
        );

        helios.behavior.mappers.setChannelConfig("node", "size", {
          type: "linear",
          attributes: "activityLevel",
          domain: [0, maxActivity],
          range: [2, 9],
          defaultValue: 3,
        });
        configureVisibilityMappers(helios);
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
          edgeOpacityScale: DEFAULT_EDGE_OPACITY_SCALE,
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
        helios.nodeStateStyle(
          "FILTERED",
          unhighlightedNodeStyle(EDGE_FILTERED_NODE_OPACITY),
        );
        // Legend highlighting should emphasize edges without replacing the
        // action-type color supplied by the categorical edge mapper.
        helios.edgeStateStyle("HIGHLIGHTED", {
          widthMul: 1.35,
          opacityMul: 2.2,
          discard: false,
          forceMaxAlpha: false,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0, 0, 0, 0],
        });
        helios.edgeStateStyle("SELECTED", {
          widthMul: 1.5,
          opacityMul: 2.5,
          discard: false,
          forceMaxAlpha: false,
          colorMul: [1, 1, 1, 1],
          colorAdd: [0, 0, 0, 0],
        });
        helios.edgeHoverStyle({
          widthMul: 1.35,
          opacityMul: 2.2,
          discard: false,
          forceMaxAlpha: false,
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
          hoverEnabled: true,
          clickMoveTolerancePx: 8,
          suppressClickAfterWheelMs: 80,
        };
        helios.enableNodePicking(pickingOptions);
        helios.enableEdgePicking(pickingOptions);
        eventUnsubscribers.push(
          helios.on(EVENTS.NODE_CLICK, ({ detail }) => {
            const index = Number(detail?.index);
            const node = nodesByIndexRef.current.get(index);
            if (node && !filteredNodeIndexesRef.current.has(index)) {
              onSelectionChange?.({ kind: "node", item: node });
            } else {
              helios.getBehavior("selection")?.clearSelection();
              onSelectionChange?.(null);
            }
          }),
          helios.on(EVENTS.EDGE_CLICK, ({ detail }) => {
            const edge = edgesByIndexRef.current.get(Number(detail?.index));
            if (edge && !hiddenEdgeTypesRef.current.has(edge.actionType)) {
              onSelectionChange?.({ kind: "edge", item: edge });
            } else {
              helios.getBehavior("selection")?.clearSelection();
              onSelectionChange?.(null);
            }
          }),
          helios.on(EVENTS.NODE_HOVER, ({ detail }) => {
            if (
              detail?.state === "in"
              && filteredNodeIndexesRef.current.has(Number(detail.index))
            ) {
              helios.getBehavior("hover")?.clearNodeHover({ silent: true, force: true });
            }
          }),
          helios.on(EVENTS.EDGE_HOVER, ({ detail }) => {
            const edge = edgesByIndexRef.current.get(Number(detail?.index));
            if (
              detail?.state === "in"
              && edge
              && hiddenEdgeTypesRef.current.has(edge.actionType)
            ) {
              helios.getBehavior("hover")?.clearEdgeHover({ silent: true, force: true });
            }
          }),
          helios.on(EVENTS.GRAPH_CLICK, ({ detail }) => {
            if (detail?.kind !== "node" && detail?.kind !== "edge") {
              onSelectionChange?.(null);
            }
          }),
        );
        // Isolated nodes can drift far from the useful topology and force the
        // camera to zoom out. Fit to nodes participating in at least one edge
        // so the connected network remains the visual focus.
        const connectedFrameNodeIndexes = connectedNodeIndexes(
          visibleEdges,
          nodeIndexesRef.current,
        );
        const connectedFrameOptions = connectedFrameNodeIndexes.length
          ? {
              nodeIndices: connectedFrameNodeIndexes,
              // Focus on the central 85% of connected positions rather than
              // letting a few long connected tails dictate the camera scale.
              coverage: CONNECTED_NETWORK_FRAME_COVERAGE,
              paddingRatio: CONNECTED_NETWORK_FRAME_PADDING,
            }
          : {};
        helios.frameNetwork({ ...connectedFrameOptions, animate: false });
        frameTimer = window.setTimeout(
          () => {
            if (heliosRef.current === helios) {
              helios.frameNetwork({ ...connectedFrameOptions, animate: true });
            }
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

  function zoomNetwork(direction) {
    const helios = heliosRef.current;
    const pose = helios?.cameraPose?.();
    if (!pose) return;

    const scale = direction === "in" ? CAMERA_ZOOM_FACTOR : 1 / CAMERA_ZOOM_FACTOR;
    const nextPose = pose.mode === "3d"
      ? { distance: pose.distance / scale }
      : { zoom: pose.zoom * scale };
    helios.setCameraPose(nextPose, {
      source: "ui",
      manual: true,
      reason: `dashboard-zoom-${direction}`,
    });
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

  function toggleNodeLegendCategory(type, additive) {
    const active = activeNodeTypes;
    const next = additive
      ? active.includes(type)
        ? active.filter((entry) => entry !== type)
        : [...active, type]
      : active.length === 1 && active[0] === type
        ? []
        : [type];
    setActiveNodeTypes(next);

    const helios = heliosRef.current;
    helios?._clearHighlightSource?.("dashboard:legend-hover");
    helios?.getBehavior("hover")?.update({
      otherHighlightNodeStyle: unhighlightedNodeStyle(
        next.length ? FILTERED_OUT_NODE_OPACITY : 1,
      ),
    });
    applyLegendSelection(helios, "nodes", next);
  }

  function toggleEdgeType(type) {
    const next = hiddenEdgeTypes.includes(type)
      ? hiddenEdgeTypes.filter((entry) => entry !== type)
      : [...hiddenEdgeTypes, type];
    const hiddenTypes = new Set(next);
    hiddenEdgeTypesRef.current = hiddenTypes;
    setHiddenEdgeTypes(next);

    const helios = heliosRef.current;
    const network = networkRef.current;
    const currentGraph = graphRef.current;
    helios?._clearHighlightSource?.("dashboard:legend-hover");
    setActiveNodeTypes([]);
    applyLegendSelection(helios, "nodes", []);
    helios?.getBehavior("hover")?.update({
      otherHighlightNodeStyle: unhighlightedNodeStyle(1),
    });
    helios?.getBehavior("selection")?.clearSelection();
    helios?.getBehavior("hover")?.clearNodeHover({ silent: true, force: true });
    helios?.getBehavior("hover")?.clearEdgeHover({ silent: true, force: true });
    onSelectionChange?.(null);
    if (!helios || !network || !currentGraph) return;
    writeFilterAttributes(
      network,
      currentGraph.edges,
      edgeIndexesRef.current,
      hiddenTypes,
    );
    configureVisibilityMappers(helios);
    filteredNodeIndexesRef.current = updateFilteredNodeStates(
      helios,
      currentGraph.nodes,
      currentGraph.edges,
      nodeIndexesRef.current,
      hiddenTypes,
      filteredNodeIndexesRef.current,
    );
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
              onToggle={toggleNodeLegendCategory}
              formatLabel={(type) => type === "bad" ? "bad actor" : type}
            />
            <EdgeFilterGroup
              title="Interaction type"
              types={actionTypes}
              colors={NETWORK_ACTION_COLORS}
              fallback="#b7c4ce"
              hiddenTypes={hiddenEdgeTypes}
              onEnter={(type) => highlightLegendCategory("edges", type)}
              onLeave={clearLegendHover}
              onToggle={toggleEdgeType}
            />
          </div>
          <div className="network-controls">
            <label>
              <span>Link distance</span>
              <input
                type="range"
                min="1"
                max="50"
                step="1"
                value={linkDistance}
                onChange={updateLinkDistance}
              />
              <output>{linkDistance.toFixed(0)}</output>
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
          <div className="network-zoom-controls" aria-label="Network zoom controls">
            <button
              type="button"
              aria-label="Zoom in"
              title="Zoom in"
              onClick={() => zoomNetwork("in")}
            >
              +
            </button>
            <button
              type="button"
              aria-label="Zoom out"
              title="Zoom out"
              onClick={() => zoomNetwork("out")}
            >
              −
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function connectedNodeIndexes(edges, nodeIndexes) {
  const connected = new Set();
  edges.forEach((edge) => {
    const sourceIndex = nodeIndexes.get(edge.source);
    const targetIndex = nodeIndexes.get(edge.target);
    if (sourceIndex !== undefined) connected.add(sourceIndex);
    if (targetIndex !== undefined) connected.add(targetIndex);
  });
  return [...connected];
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

function EdgeFilterGroup({
  title,
  types,
  colors,
  fallback,
  hiddenTypes,
  onEnter,
  onLeave,
  onToggle,
}) {
  return (
    <div className="network-frame-legend-group">
      <strong>{title}</strong>
      {types.map((type) => {
        const visible = !hiddenTypes.includes(type);
        const color = colors[type] || fallback;
        return (
          <label
            key={type}
            className={`network-edge-filter${visible ? "" : " is-filtered"}`}
            title={visible ? "Uncheck to hide this interaction type" : "Check to show this interaction type"}
            onPointerDown={(event) => event.stopPropagation()}
            onPointerUp={(event) => event.stopPropagation()}
            onWheel={(event) => event.stopPropagation()}
            onPointerEnter={() => { if (visible) onEnter(type); }}
            onPointerLeave={onLeave}
          >
            <input
              type="checkbox"
              checked={visible}
              onChange={() => onToggle(type)}
              onClick={(event) => event.stopPropagation()}
            />
            <i style={{ background: color }} />
            <span>{type}</span>
          </label>
        );
      })}
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

function writeGraphAttributes(
  network,
  nodes,
  edges,
  nodeIndexes,
  edgeIndexes,
  hiddenEdgeTypes = new Set(),
) {
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
  writeFilterAttributes(
    network,
    edges,
    edgeIndexes,
    hiddenEdgeTypes,
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

function writeFilterAttributes(
  network,
  edges,
  edgeIndexes,
  hiddenEdgeTypes,
) {
  const hiddenTypes = hiddenEdgeTypes instanceof Set
    ? hiddenEdgeTypes
    : new Set(hiddenEdgeTypes || []);
  const edgesByIndex = itemsByNetworkIndex(edges, edgeIndexes);
  if (!edges.length) return;
  network.edgeAttribute("filterOpacity", (current, index) => {
    const edge = edgesByIndex.get(index);
    return edge && hiddenTypes.has(edge.actionType) ? 0 : 1;
  });
}

function configureVisibilityMappers(helios) {
  helios?.behavior.mappers.setChannelConfig("node", "color", {
    type: "categorical",
    attributes: "userTypeCode",
    domain: USER_TYPE_DOMAIN,
    range: Object.values(NETWORK_USER_COLORS).map(toRgbaHex),
    defaultValue: "#b7c4ceff",
  });
  helios?.behavior.mappers.setChannelConfig("edge", "opacity", {
    type: "linear",
    attributes: "filterOpacity",
    domain: [0, 1],
    range: [0, 1],
    defaultValue: 1,
  });
}

function updateFilteredNodeStates(
  helios,
  nodes,
  edges,
  nodeIndexes,
  hiddenEdgeTypes,
  previousFilteredIndexes = new Set(),
) {
  const hiddenTypes = hiddenEdgeTypes instanceof Set
    ? hiddenEdgeTypes
    : new Set(hiddenEdgeTypes || []);
  const nextFilteredIndexes = new Set();

  if (hiddenTypes.size) {
    const connectedNodeIds = new Set();
    const visiblyConnectedNodeIds = new Set();
    for (const edge of edges) {
      connectedNodeIds.add(edge.source);
      connectedNodeIds.add(edge.target);
      if (!hiddenTypes.has(edge.actionType)) {
        visiblyConnectedNodeIds.add(edge.source);
        visiblyConnectedNodeIds.add(edge.target);
      }
    }
    for (const node of nodes) {
      if (connectedNodeIds.has(node.id) && !visiblyConnectedNodeIds.has(node.id)) {
        const index = Number(nodeIndexes.get(node.id));
        if (Number.isInteger(index) && index >= 0) nextFilteredIndexes.add(index);
      }
    }
  }

  const restoredIndexes = [...previousFilteredIndexes].filter(
    (index) => !nextFilteredIndexes.has(index),
  );
  const newlyFilteredIndexes = [...nextFilteredIndexes].filter(
    (index) => !previousFilteredIndexes.has(index),
  );
  if (restoredIndexes.length) {
    helios?.nodeState(restoredIndexes, "FILTERED", { mode: "remove" });
  }
  if (newlyFilteredIndexes.length) {
    helios?.nodeState(newlyFilteredIndexes, "FILTERED", { mode: "add" });
  }
  return nextFilteredIndexes;
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
