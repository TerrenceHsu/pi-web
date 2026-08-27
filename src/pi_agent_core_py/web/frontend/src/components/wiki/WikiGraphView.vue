<script setup lang="ts">
import { computed } from "vue"

import { useWikiStore } from "../../stores/wikiStore"

const wikiStore = useWikiStore()
const width = 900
const height = 540

const layoutNodes = computed(() => {
  const nodes = wikiStore.graph?.nodes ?? []
  const radius = Math.min(width, height) * 0.37
  return nodes.map((node, index) => {
    const angle = nodes.length <= 1 ? 0 : (Math.PI * 2 * index) / nodes.length - Math.PI / 2
    return {
      ...node,
      x: width / 2 + Math.cos(angle) * radius,
      y: height / 2 + Math.sin(angle) * radius,
    }
  })
})

const positionedEdges = computed(() => {
  const byId = new Map(layoutNodes.value.map((node) => [node.id, node]))
  return (wikiStore.graph?.edges ?? []).flatMap((edge) => {
    const from = byId.get(edge.from_node_id)
    const to = byId.get(edge.to_node_id)
    return from && to ? [{ ...edge, from, to }] : []
  })
})

function shortLabel(value: string): string {
  return value.length > 22 ? `${value.slice(0, 20)}…` : value
}

async function openNode(nodeId: string, kind: "page" | "source"): Promise<void> {
  if (kind !== "page") return
  wikiStore.activeTab = "pages"
  await wikiStore.loadPages()
  await wikiStore.selectPage(nodeId)
}
</script>

<template>
  <section class="graph-view" data-testid="wiki-graph-view">
    <header class="graph-header">
      <div>
        <h2>Page knowledge graph</h2>
        <span>
          Published page relations and system-maintained source evidence · revision
          {{ wikiStore.graph?.graph_revision ?? "—" }}
        </span>
      </div>
      <button type="button" :disabled="wikiStore.loadingGraph" @click="wikiStore.loadGraph()">
        {{ wikiStore.loadingGraph ? "Loading…" : "Refresh graph" }}
      </button>
    </header>

    <div v-if="wikiStore.loadingGraph && !wikiStore.graph" class="graph-empty">Loading graph…</div>
    <div v-else-if="!wikiStore.graph || wikiStore.graph.nodes.length === 0" class="graph-empty">
      Approve Wiki pages to populate the graph.
    </div>
    <div v-else class="graph-layout">
      <div class="graph-canvas">
        <svg :viewBox="`0 0 ${width} ${height}`" role="img" aria-label="Wiki page and Source graph">
          <defs>
            <marker
              id="wiki-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#94a3b8" />
            </marker>
          </defs>
          <g v-for="edge in positionedEdges" :key="edge.id">
            <line
              :x1="edge.from.x"
              :y1="edge.from.y"
              :x2="edge.to.x"
              :y2="edge.to.y"
              :class="['graph-edge', { system: edge.system_managed }]"
              marker-end="url(#wiki-arrow)"
            />
          </g>
          <g
            v-for="node in layoutNodes"
            :key="node.id"
            :class="['graph-node', node.kind]"
            role="button"
            :aria-label="`${node.kind}: ${node.label}`"
            tabindex="0"
            @click="openNode(node.id, node.kind)"
            @keydown.enter="openNode(node.id, node.kind)"
          >
            <circle :cx="node.x" :cy="node.y" :r="node.kind === 'page' ? 34 : 27" />
            <text :x="node.x" :y="node.y + 3" text-anchor="middle">
              {{ shortLabel(node.label) }}
            </text>
          </g>
        </svg>
      </div>
      <aside class="edge-list">
        <h3>Relations · {{ wikiStore.graph.edges.length }}</h3>
        <div
          v-for="edge in wikiStore.graph.edges"
          :key="edge.id"
          class="edge-row"
          data-testid="wiki-graph-edge"
        >
          <strong>{{ edge.relation_type }}</strong>
          <span>{{ edge.from_node_id }} → {{ edge.to_node_id }}</span>
          <small v-if="edge.system_managed">system managed</small>
        </div>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.graph-view {
  display: flex;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  padding: 18px;
}
.graph-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
}
.graph-header h2 {
  margin: 0;
  font-size: 18px;
}
.graph-header span {
  color: var(--muted);
  font-size: 11px;
}
.graph-layout {
  display: grid;
  min-height: 0;
  flex: 1;
  grid-template-columns: minmax(0, 1fr) 290px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 11px;
  background: #fff;
}
.graph-canvas {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  background-image: radial-gradient(#cbd5e1 0.7px, transparent 0.7px);
  background-size: 18px 18px;
}
.graph-canvas svg {
  display: block;
  width: 100%;
  min-width: 620px;
  height: 100%;
  min-height: 500px;
}
.graph-edge {
  stroke: #94a3b8;
  stroke-width: 1.6;
}
.graph-edge.system {
  stroke: #a78bfa;
  stroke-dasharray: 5 4;
}
.graph-node {
  cursor: pointer;
}
.graph-node circle {
  stroke: #60a5fa;
  stroke-width: 2;
  fill: #eff6ff;
}
.graph-node.source circle {
  stroke: #a78bfa;
  fill: #f5f3ff;
}
.graph-node text {
  max-width: 55px;
  fill: #1e293b;
  font-size: 9px;
  pointer-events: none;
}
.edge-list {
  overflow-y: auto;
  padding: 14px;
  border-left: 1px solid var(--border);
}
.edge-list h3 {
  margin: 0 0 10px;
  font-size: 13px;
}
.edge-row {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin-bottom: 7px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 7px;
}
.edge-row strong {
  color: #1d4ed8;
  font-size: 11px;
}
.edge-row span,
.edge-row small {
  overflow-wrap: anywhere;
  color: var(--muted);
  font-size: 9px;
}
.graph-empty {
  display: grid;
  min-height: 300px;
  place-items: center;
  color: var(--muted);
  font-size: 12px;
}
@media (max-width: 850px) {
  .graph-layout {
    display: block;
    overflow-y: auto;
  }
  .graph-canvas {
    height: 480px;
  }
  .edge-list {
    border-top: 1px solid var(--border);
    border-left: 0;
  }
}
</style>
