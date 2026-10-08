// The graph's contents (PLAN §4): transactions and the outputs they create or spend, built from the
// API's answers, and their positions from dagre. Pure: no DOM and no network. Labels are plain text,
// which Cytoscape draws as text (T-104). Amounts stay integer sats (`btc` formats them).
import dagre from "@dagrejs/dagre";

import { btc, type GraphOwner, type GraphTx } from "../api/client";

const ME = 1; // the user entity, seeded by the schema

/** A transaction node, or an output node (`txid:n`). */
export type TxNode = { kind: "tx"; id: string; txid: string; blockhash: string | null; mixing: boolean | null };
export type OutputNode = {
  kind: "output";
  id: string;
  txid: string; // the transaction that created it
  n: number;
  blockhash: string | null; // the creating transaction's block, once that transaction is loaded
  sats: number | null; // null if the creating transaction isn't loaded and the input didn't say
  script: string | null;
  address: string | null;
  owner: GraphOwner;
  unspendable: boolean;
};
export type Node = TxNode | OutputNode;
export type Edge = { id: string; source: string; target: string };
export type Graph = { nodes: Map<string, Node>; edges: Map<string, Edge> };

export const txId = (txid: string) => `tx:${txid}`;
export const outputId = (txid: string, n: number) => `out:${txid}:${n}`;

export function emptyGraph(): Graph {
  return { nodes: new Map(), edges: new Map() };
}

function short(hex: string): string {
  return `${hex.slice(0, 8)}…${hex.slice(-4)}`;
}

/** The owner class a node is drawn with: a fixed set, never free text (ADR 0037). */
export function ownerClass(node: OutputNode): "mine" | "tagged" | "untagged" | "unspendable" {
  if (node.unspendable) return "unspendable";
  if (node.owner === null) return "untagged";
  return node.owner.entity_id === ME ? "mine" : "tagged";
}

export function label(node: Node): string {
  if (node.kind === "tx") return short(node.txid);
  const amount = node.sats === null ? "?" : btc(node.sats);
  const where = node.address
    ? short(node.address)
    : node.unspendable
      ? "unspendable"
      : node.script
        ? short(node.script)
        : "";
  return `${amount}\n${where}`;
}

function edge(graph: Graph, source: string, target: string): void {
  const id = `${source}->${target}`;
  graph.edges.set(id, { id, source, target });
}

/** Adds a transaction with its inputs' and outputs' nodes. What is already known about an output
 * is kept unless this answer knows more (an input's prevout gets its creating block later). */
export function addTx(graph: Graph, tx: GraphTx): Graph {
  const next: Graph = { nodes: new Map(graph.nodes), edges: new Map(graph.edges) };
  const id = txId(tx.txid);
  next.nodes.set(id, { kind: "tx", id, txid: tx.txid, blockhash: tx.blockhash, mixing: tx.mixing });
  for (const input of tx.inputs) {
    if (input.prevout === null) continue; // a coinbase input spends nothing
    const oid = outputId(input.prevout.txid, input.prevout.vout);
    const known = next.nodes.get(oid);
    if (known === undefined) {
      next.nodes.set(oid, {
        kind: "output",
        id: oid,
        txid: input.prevout.txid,
        n: input.prevout.vout,
        blockhash: null,
        sats: input.sats,
        script: input.script,
        address: input.address,
        owner: input.owner,
        unspendable: false,
      });
    }
    edge(next, oid, id);
  }
  for (const output of tx.outputs) {
    const oid = outputId(tx.txid, output.n);
    next.nodes.set(oid, {
      kind: "output",
      id: oid,
      txid: tx.txid,
      n: output.n,
      blockhash: tx.blockhash,
      sats: output.sats,
      script: output.script,
      address: output.address,
      owner: output.owner,
      unspendable: output.unspendable,
    });
    edge(next, id, oid);
  }
  return next;
}

/** Records a new owner for every output paying to `script` (after a tag is saved). */
export function setOwner(graph: Graph, script: string, owner: GraphOwner, address: string | null): Graph {
  const nodes = new Map(graph.nodes);
  for (const [id, node] of nodes) {
    if (node.kind === "output" && node.script === script) {
      nodes.set(id, { ...node, owner, address: address ?? node.address });
    }
  }
  return { nodes, edges: graph.edges };
}

export function setMixingFlag(graph: Graph, txid: string, mixing: boolean): Graph {
  const nodes = new Map(graph.nodes);
  const node = nodes.get(txId(txid));
  if (node?.kind === "tx") nodes.set(node.id, { ...node, mixing });
  return { nodes, edges: graph.edges };
}

/** Left-to-right positions: funding transactions to the left of what they fund. */
export function layout(graph: Graph): Map<string, { x: number; y: number }> {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 30, ranksep: 70 });
  g.setDefaultEdgeLabel(() => ({}));
  // wide enough for the two-line label under each node (style.ts: text-max-width 160px)
  for (const node of graph.nodes.values()) g.setNode(node.id, { width: 160, height: 56 });
  for (const e of graph.edges.values()) g.setEdge(e.source, e.target);
  dagre.layout(g);
  const positions = new Map<string, { x: number; y: number }>();
  for (const id of g.nodes()) {
    const n = g.node(id);
    positions.set(id, { x: n.x, y: n.y });
  }
  return positions;
}
