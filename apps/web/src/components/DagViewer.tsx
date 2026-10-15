import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow,
  Background,
  Controls,
  type Edge,
  type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { getDag } from '../lib/api';
import type { DagNode } from '../lib/types';
import './DagViewer.css';

const POLL_MS = 3000;
const COL_GAP = 260;
const ROW_GAP = 84;

const STATUS_CLASS: Record<DagNode['status'], string> = {
  idle: 'dag-node-idle',
  running: 'dag-node-running',
  completed: 'dag-node-completed',
  failed: 'dag-node-failed',
  blocked: 'dag-node-blocked',
};

/**
 * Topological-layer layout: x = layer index (dependencies before dependents),
 * y = index within the layer. Cycle-safe; unknown dependencies are ignored.
 */
function buildGraph(dag: DagNode[]): { rfNodes: Node[]; rfEdges: Edge[] } {
  const byId = new Map(dag.map((n) => [n.id, n]));
  const levelOf = new Map<string, number>();

  const level = (id: string, seen: Set<string>): number => {
    const cached = levelOf.get(id);
    if (cached !== undefined) return cached;
    if (seen.has(id)) return 0; // cycle guard
    seen.add(id);
    const node = byId.get(id);
    const deps = (node?.dependencies ?? []).filter((d) => byId.has(d));
    const lv = deps.length > 0 ? Math.max(...deps.map((d) => level(d, seen))) + 1 : 0;
    seen.delete(id);
    levelOf.set(id, lv);
    return lv;
  };
  dag.forEach((n) => level(n.id, new Set()));

  const layers = new Map<number, DagNode[]>();
  for (const node of dag) {
    const lv = levelOf.get(node.id) ?? 0;
    const bucket = layers.get(lv) ?? [];
    bucket.push(node);
    layers.set(lv, bucket);
  }
  const counters = new Map<number, number>();
  const rfNodes: Node[] = dag.map((node) => {
    const lv = levelOf.get(node.id) ?? 0;
    const row = counters.get(lv) ?? 0;
    counters.set(lv, row + 1);
    return {
      id: node.id,
      position: { x: lv * COL_GAP, y: row * ROW_GAP },
      data: { label: node.name },
      title: node.id,
      className: `dag-node ${STATUS_CLASS[node.status]}`,
    };
  });

  const rfEdges: Edge[] = dag.flatMap((node) =>
    node.dependencies
      .filter((dep) => byId.has(dep))
      .map((dep) => ({
        id: `e-${dep}-${node.id}`,
        source: dep,
        target: node.id,
        animated: byId.get(node.id)?.status === 'running',
        className: 'dag-edge',
      })),
  );
  return { rfNodes, rfEdges };
}

function normalize(payload: unknown): DagNode[] {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === 'object' && 'nodes' in payload) {
    const nodes = (payload as { nodes: unknown }).nodes;
    return Array.isArray(nodes) ? (nodes as DagNode[]) : [];
  }
  return [];
}

export default function DagViewer() {
  const [nodes, setNodes] = useState<DagNode[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hasLoaded, setHasLoaded] = useState(false);
  const timer = useRef<number | null>(null);

  const fetchDag = useCallback(async () => {
    try {
      const payload = await getDag();
      setNodes(normalize(payload));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load DAG');
    } finally {
      setHasLoaded(true);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => {
      if (alive) void fetchDag();
    };
    const start = () => {
      tick();
      timer.current = window.setInterval(tick, POLL_MS);
    };
    const stop = () => {
      if (timer.current !== null) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
    };
    const onVisibility = () => (document.hidden ? stop() : start());

    start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      alive = false;
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [fetchDag]);

  const { rfNodes, rfEdges } = useMemo(() => buildGraph(nodes), [nodes]);

  return (
    <div className="dag-viewer" data-testid="dag-viewer">
      {error && (
        <div className="dag-error" role="alert">
          <span className="dag-error-dot" aria-hidden="true" />
          <span className="dag-error-text">{error}</span>
          <button type="button" className="dag-retry" onClick={() => void fetchDag()}>
            Retry
          </button>
        </div>
      )}

      {!hasLoaded ? (
        <div className="dag-loading" aria-busy="true">
          <div className="dag-skeleton" />
        </div>
      ) : nodes.length === 0 ? (
        <div className="dag-empty">
          <svg
            className="dag-empty-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            aria-hidden="true"
          >
            <circle cx="6" cy="6" r="2.5" />
            <circle cx="18" cy="6" r="2.5" />
            <circle cx="12" cy="18" r="2.5" />
            <path d="M8 7.5 10.5 15.5M16 7.5 13.5 15.5M8.5 6h7" />
          </svg>
          <p className="dag-empty-text">No DAG run yet — ask something complex</p>
        </div>
      ) : (
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          fitView
          fitViewOptions={{ padding: 0.2 }}
          proOptions={{ hideAttribution: true }}
          minZoom={0.4}
          maxZoom={1.5}
          colorMode="dark"
          className="dag-canvas"
        >
          <Background gap={24} size={1} />
          <Controls
            showInteractive={false}
            showFitView={false}
            position="bottom-right"
          />
        </ReactFlow>
      )}
    </div>
  );
}
