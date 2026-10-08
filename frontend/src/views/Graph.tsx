// The transaction graph (PLAN §4, M3): open a transaction, expand backward to the transaction that
// created an input and forward to the one that spent an output, and tag from the side panel: whose
// an address is (owner, wallet, label, wallet apps) and whether a transaction is a mix. Every txid
// and script goes in a POST body (T-105). React renders all text, and Cytoscape draws labels as text
// with a stylesheet of constants (T-104, ADR 0037). Each element is also listed as a button beside the
// canvas, so the graph can be used without a pointer.
import cytoscape from "cytoscape";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  type Accounts,
  btc,
  type Change,
  getAccounts,
  getGraphTx,
  getSpender,
  getTagHistory,
  setMixing,
  type Spender,
  tagAddress,
} from "../api/client";
import {
  addTx,
  emptyGraph,
  type Graph as GraphModel,
  label,
  layout,
  type Node,
  type OutputNode,
  ownerClass,
  setMixingFlag,
  setOwner,
  txId,
  type TxNode,
} from "../graph/model";
import { STYLESHEET } from "../graph/style";

const ME = 1; // the user entity, seeded by the schema
const HASH = /^[0-9a-f]{64}$/;

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

function classes(node: Node): string {
  if (node.kind === "tx") return node.mixing ? "tx mixing" : "tx";
  return `output ${ownerClass(node)}`;
}

const SPENDER_TEXT: Record<Spender["state"], string> = {
  unspendable: "Unspendable: it can never be spent.",
  unspent: "Unspent",
  spent_unconfirmed: "Spent by an unconfirmed transaction",
  spent: "Spent",
};

function History({ changes }: { changes: Change[] }) {
  if (changes.length === 0) return <p>No changes recorded.</p>;
  return (
    <ol id="graph-history">
      {changes.map((c, i) => (
        <li key={i}>
          {c.at}: {c.before === null ? "set" : "changed"} {JSON.stringify(c.after)}
        </li>
      ))}
    </ol>
  );
}

function TxPanel(props: {
  node: TxNode;
  session: string;
  onMixing: (txid: string, mixing: boolean) => void;
}) {
  const { node, session, onMixing } = props;
  const [mixing, setMixingChoice] = useState(node.mixing ?? false);
  const [result, setResult] = useState<string | null>(null);
  const [changes, setChanges] = useState<Change[] | null>(null);

  async function save() {
    setResult(null);
    try {
      await setMixing(session, node.txid, mixing);
      onMixing(node.txid, mixing);
      setResult(mixing ? "Marked as a mixing transaction." : "Marked as not a mixing transaction.");
    } catch (error) {
      setResult(message(error));
    }
  }

  async function showHistory() {
    try {
      setChanges((await getTagHistory(session, "tx_flag", node.txid)).changes);
    } catch (error) {
      setResult(message(error));
    }
  }

  return (
    <>
      <h3>Transaction</h3>
      <p className="txid" id="graph-panel-txid">
        {node.txid}
      </p>
      <p>{node.blockhash === null ? "Unconfirmed" : "Confirmed"}</p>
      <label>
        <input
          id="graph-mixing"
          type="checkbox"
          checked={mixing}
          onChange={(e) => setMixingChoice(e.target.checked)}
        />{" "}
        A mixing transaction (CoinJoin, PayJoin)
      </label>{" "}
      <button id="graph-mixing-save" type="button" onClick={save}>
        Save
      </button>
      {result && <p id="graph-mixing-result">{result}</p>}
      <button id="graph-show-history" type="button" onClick={showHistory}>
        Change history
      </button>
      {changes && <History changes={changes} />}
    </>
  );
}

function OutputPanel(props: {
  node: OutputNode;
  session: string;
  accounts: Accounts | null;
  loaded: boolean; // whether the transaction that created it is in the graph
  onOpen: (txid: string, blockhash: string | null) => Promise<void>;
  onTagged: (node: OutputNode, entityId: number, accountId: number | null, labelText: string) => void;
}) {
  const { node, session, accounts, loaded, onOpen, onTagged } = props;
  const [spender, setSpender] = useState<Spender | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [owner, setOwnerChoice] = useState<number>(node.owner?.entity_id ?? ME);
  const [account, setAccount] = useState<number | null>(node.owner?.tax_account_id ?? null);
  const [labelText, setLabelText] = useState(node.owner?.label ?? "");
  const [clients, setClients] = useState<number[]>([]);
  const [result, setResult] = useState<string | null>(null);
  const [changes, setChanges] = useState<Change[] | null>(null);

  const wallets = (accounts?.tax_accounts ?? []).filter((a) => a.kind === "self_custody");
  const ownerName = (id: number) =>
    id === ME ? "You" : accounts?.entities.find((e) => e.id === id)?.name ?? `owner ${id}`;

  async function findSpender() {
    setError(null);
    try {
      const found = await getSpender(session, node.txid, node.blockhash, node.n);
      setSpender(found);
      if (found.spending_txid !== null) await onOpen(found.spending_txid, found.blockhash);
    } catch (e) {
      setError(message(e));
    }
  }

  async function save() {
    if (node.script === null) return;
    setResult(null);
    const accountId = owner === ME ? (account ?? wallets[0]?.id ?? null) : null;
    try {
      const saved = await tagAddress(session, {
        script: node.script,
        address: node.address,
        entity_id: owner,
        tax_account_id: accountId,
        label: labelText,
        client_ids: clients,
      });
      onTagged(node, owner, accountId, labelText);
      setResult(saved.new ? "Tagged. The app is scanning this address's history." : "Tag saved.");
    } catch (e) {
      setResult(message(e));
    }
  }

  async function showHistory() {
    if (node.script === null) return;
    try {
      setChanges((await getTagHistory(session, "address_tag", node.script)).changes);
    } catch (e) {
      setError(message(e));
    }
  }

  return (
    <>
      <h3>Output</h3>
      <p className="txid" id="graph-panel-outpoint">
        {node.txid}:{node.n}
      </p>
      <p>
        {node.sats === null ? "Amount unknown" : `${btc(node.sats)} BTC`}
        {node.address && <> to {node.address}</>}
      </p>
      <p id="graph-panel-owner">
        {node.unspendable ? "Unspendable" : node.owner ? `Owner: ${ownerName(node.owner.entity_id)}` : "Not tagged"}
        {node.owner?.label ? ` (${node.owner.label})` : ""}
      </p>
      {!loaded && (
        <button id="graph-funding" type="button" onClick={() => onOpen(node.txid, node.blockhash)}>
          Show the transaction that created it
        </button>
      )}
      {!node.unspendable && (
        <button id="graph-spender" type="button" onClick={findSpender}>
          Find what spent it
        </button>
      )}
      {spender && <p id="graph-spender-state">{SPENDER_TEXT[spender.state]}</p>}
      {error && <p id="graph-panel-error">{error}</p>}

      {!node.unspendable && node.script !== null && (
        <fieldset id="tag-form">
          <legend>Whose address is this?</legend>
          <label>
            Owner{" "}
            <select id="tag-owner" value={owner} onChange={(e) => setOwnerChoice(Number(e.target.value))}>
              <option value={ME}>You</option>
              {(accounts?.entities ?? [])
                .filter((e) => e.id !== ME)
                .map((e) => (
                  <option key={e.id} value={e.id}>
                    {e.name}
                  </option>
                ))}
            </select>
          </label>{" "}
          {owner === ME && (
            <label>
              Wallet{" "}
              <select
                id="tag-account"
                value={account ?? wallets[0]?.id ?? ""}
                onChange={(e) => setAccount(Number(e.target.value))}
              >
                {wallets.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </select>
            </label>
          )}{" "}
          <label>
            Label <input id="tag-label" value={labelText} maxLength={200} onChange={(e) => setLabelText(e.target.value)} />
          </label>
          {(accounts?.clients ?? []).map((c) => (
            <label key={c.id}>
              <input
                type="checkbox"
                className="tag-client"
                checked={clients.includes(c.id)}
                onChange={(e) =>
                  setClients(e.target.checked ? [...clients, c.id] : clients.filter((x) => x !== c.id))
                }
              />{" "}
              {c.name}
            </label>
          ))}{" "}
          <button id="tag-save" type="button" onClick={save}>
            Save tag
          </button>
          {result && <p id="tag-result">{result}</p>}
          <button id="tag-history" type="button" onClick={showHistory}>
            Change history
          </button>
          {changes && <History changes={changes} />}
        </fieldset>
      )}
    </>
  );
}

export function Graph({ session }: { session: string }) {
  const container = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<cytoscape.Core | null>(null);
  const [graph, setGraph] = useState<GraphModel>(emptyGraph);
  const [selected, setSelected] = useState<string | null>(null);
  const [txidText, setTxidText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<Accounts | null>(null);

  useEffect(() => {
    getAccounts(session).then(setAccounts, () => setAccounts(null));
  }, [session]);

  // One Cytoscape instance for the view's life; its elements follow `graph` below.
  useEffect(() => {
    if (!container.current) return;
    const cy = cytoscape({
      container: container.current,
      style: STYLESHEET as unknown as cytoscape.StylesheetJson,
      layout: { name: "preset" },
      boxSelectionEnabled: false,
      autoungrabify: true,
      wheelSensitivity: 0.3,
    });
    cy.on("tap", "node", (event) => setSelected(event.target.id() as string));
    cyRef.current = cy;
    return () => {
      cy.destroy();
      cyRef.current = null;
    };
  }, []);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    const positions = layout(graph);
    cy.batch(() => {
      cy.elements().remove();
      // Element data is the id, the edges' ends and the text label only: nothing a style could load.
      cy.add([
        ...[...graph.nodes.values()].map((n) => ({
          group: "nodes" as const,
          data: { id: n.id, label: label(n) },
          classes: classes(n),
          position: positions.get(n.id) ?? { x: 0, y: 0 },
        })),
        ...[...graph.edges.values()].map((e) => ({
          group: "edges" as const,
          data: { id: e.id, source: e.source, target: e.target },
        })),
      ]);
    });
    cy.fit(undefined, 30);
    if (selected) cy.$id(selected).select();
  }, [graph, selected]);

  const open = useCallback(
    async (txid: string, blockhash: string | null) => {
      setError(null);
      try {
        const tx = await getGraphTx(session, txid, blockhash);
        setGraph((g) => addTx(g, tx));
        setSelected(txId(tx.txid));
      } catch (e) {
        setError(message(e));
      }
    },
    [session],
  );

  async function openTyped() {
    const txid = txidText.trim().toLowerCase();
    if (!HASH.test(txid)) {
      setError("A transaction id is 64 hex digits.");
      return;
    }
    await open(txid, null);
  }

  const node = selected ? graph.nodes.get(selected) : undefined;

  return (
    <section id="graph-view">
      <h2>Transaction graph</h2>
      <p>
        Open a transaction, then follow its coins: back to the transaction that created an input, forward
        to the one that spent an output. Links shown are the ones the app knows; they don't make coins
        private.
      </p>
      <label>
        Transaction id{" "}
        <input id="graph-txid" value={txidText} spellCheck={false} onChange={(e) => setTxidText(e.target.value)} />
      </label>{" "}
      <button id="graph-open" type="button" onClick={openTyped}>
        Open
      </button>
      {graph.nodes.size > 0 && (
        <button id="graph-clear" type="button" onClick={() => (setGraph(emptyGraph()), setSelected(null))}>
          Clear
        </button>
      )}
      {error && <p id="graph-error">{error}</p>}
      <div className="graph-layout">
        <div id="graph" className="graph-canvas" ref={container} />
        <aside id="graph-panel" className="graph-panel">
          {node === undefined && <p>Select a transaction or an output.</p>}
          {node?.kind === "tx" && (
            <TxPanel
              key={node.id}
              node={node}
              session={session}
              onMixing={(txid, mixing) => setGraph((g) => setMixingFlag(g, txid, mixing))}
            />
          )}
          {node?.kind === "output" && (
            <OutputPanel
              key={node.id}
              node={node}
              session={session}
              accounts={accounts}
              loaded={graph.nodes.has(txId(node.txid))}
              onOpen={open}
              onTagged={(n, entityId, accountId, labelText) =>
                n.script !== null &&
                setGraph((g) =>
                  setOwner(g, n.script as string, { entity_id: entityId, tax_account_id: accountId, label: labelText }, n.address),
                )
              }
            />
          )}
        </aside>
      </div>
      <ul id="graph-nodes" className="graph-nodes">
        {[...graph.nodes.values()].map((n) => (
          <li key={n.id}>
            <button
              type="button"
              className={n.id === selected ? "graph-node selected" : "graph-node"}
              data-node={n.id}
              onClick={() => setSelected(n.id)}
            >
              {n.kind === "tx" ? `Transaction ${n.txid}` : `Output ${n.txid}:${n.n}`}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
