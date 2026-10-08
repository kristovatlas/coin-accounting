// Holdings and history (PLAN §3: "address/UTXO/tx history views"), from the chain cache only, so it
// works offline. Every figure is as of the last finished sync (`as_of`); an address that isn't scanned
// that far yet is marked, never shown as a settled zero (T-207, T-210). React renders every value as
// text (T-104). An address's events are fetched by POST, so its script never appears in a URL (T-105).
import { useCallback, useEffect, useState } from "react";

import {
  type Accounts,
  type Addresses,
  type AddressSummary,
  btc,
  getAccounts,
  getAddressEvents,
  getAddresses,
  getUtxos,
  type HistoryEvent,
  type Utxo,
} from "../api/client";

const ME = 1; // the user entity, seeded by the schema

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

/** How far an address's history is known, in words. */
function scanState(a: AddressSummary, asOf: number | null): string {
  if (a.scanned_to === null) return "not scanned yet";
  if (asOf !== null && a.scanned_to >= asOf) return "scanned";
  return `scanned to block ${a.scanned_to}`;
}

function Events({ events }: { events: HistoryEvent[] }) {
  if (events.length === 0) return <p>No activity as of the last sync.</p>;
  return (
    <table id="address-events">
      <thead>
        <tr>
          <th>Block</th>
          <th>Kind</th>
          <th>Transaction</th>
          <th>BTC</th>
        </tr>
      </thead>
      <tbody>
        {events.map((e) => (
          <tr key={`${e.kind}:${e.txid}:${e.n}:${e.blockhash}`}>
            <td>{e.height}</td>
            <td>{e.kind === "receive" ? "received" : "spent"}</td>
            <td className="txid">
              {e.txid}:{e.n}
            </td>
            <td>{e.kind === "receive" ? btc(e.sats) : `-${btc(e.sats)}`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Holdings({ session }: { session: string }) {
  const [addresses, setAddresses] = useState<Addresses | null>(null);
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [utxos, setUtxos] = useState<Utxo[]>([]);
  const [selected, setSelected] = useState<string | null>(null); // a script
  const [events, setEvents] = useState<HistoryEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [a, acc, u] = await Promise.all([getAddresses(session), getAccounts(session), getUtxos(session)]);
      setAddresses(a);
      setAccounts(acc);
      setUtxos(u.utxos);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  async function show(script: string) {
    setSelected(script);
    setEvents(null);
    try {
      setEvents((await getAddressEvents(session, script)).events);
    } catch (e) {
      setError(message(e));
    }
  }

  if (!addresses || !accounts) return <section id="holdings">{error ? <p>{error}</p> : <p>Loading…</p>}</section>;

  const asOf = addresses.as_of?.height ?? null;
  const own = addresses.addresses.filter((a) => a.entity_id === ME);
  const others = addresses.addresses.filter((a) => a.entity_id !== ME);
  const complete = own.every((a) => a.scanned_to !== null && asOf !== null && a.scanned_to >= asOf);
  const total = own.reduce((sum, a) => sum + a.balance, 0);
  const accountName = (id: number | null) => accounts.tax_accounts.find((t) => t.id === id)?.name ?? "";
  const ownerName = (id: number) => accounts.entities.find((e) => e.id === id)?.name ?? "another owner";

  return (
    <section id="holdings">
      <h2>Holdings</h2>
      <p id="holdings-as-of">
        {asOf === null ? "No sync has finished yet." : `As of block ${asOf}.`}
        {addresses.catching_up ? " A sync is running; newer blocks aren't counted yet." : ""}
      </p>
      <p id="holdings-total">
        Total: {btc(total)} BTC{complete ? "" : " (some addresses aren't fully scanned yet, so this may be incomplete)"}
      </p>
      <button id="holdings-refresh" type="button" disabled={busy} onClick={() => void load()}>
        Refresh
      </button>
      {error && <p id="holdings-error">{error}</p>}

      <table id="holdings-addresses">
        <thead>
          <tr>
            <th>Address</th>
            <th>Wallet</th>
            <th>Label</th>
            <th>BTC</th>
            <th>Outputs</th>
            <th>Scan</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {own.map((a) => (
            <tr key={a.script}>
              <td className="address">{a.address ?? a.script}</td>
              <td>{accountName(a.tax_account_id)}</td>
              <td>{a.label}</td>
              <td className="balance">{btc(a.balance)}</td>
              <td>{a.utxos}</td>
              <td className="scan">{scanState(a, asOf)}</td>
              <td>
                <button type="button" className="show-history" onClick={() => void show(a.script)}>
                  History
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {selected !== null && (
        <section id="address-history">
          <h3>History of {addresses.addresses.find((a) => a.script === selected)?.address ?? selected}</h3>
          {events === null ? <p>Loading…</p> : <Events events={events} />}
        </section>
      )}

      <h3>Unspent outputs ({utxos.length})</h3>
      <table id="holdings-utxos">
        <tbody>
          {utxos.map((u) => (
            <tr key={`${u.txid}:${u.vout}`}>
              <td className="txid">
                {u.txid}:{u.vout}
              </td>
              <td>{btc(u.sats)}</td>
              <td>block {u.height}</td>
              <td>{u.complete ? "" : "may since be spent (not fully scanned)"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {others.length > 0 && (
        <>
          <h3>Other owners' addresses ({others.length})</h3>
          <p>Tagged to someone else: not your holdings.</p>
          <ul id="holdings-others">
            {others.map((a) => (
              <li key={a.script}>
                {a.address ?? a.script} — {ownerName(a.entity_id)}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
