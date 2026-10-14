// The user's events (PLAN §2, §7, M6): every buy, sale, gift and movement, entered by hand, with its
// choice of lots. Amounts are typed as BTC and USD text and never pass through floats; moments are
// entered in UTC. React renders every value as text (T-104); refusals show the app's fixed messages.
import { useCallback, useEffect, useState } from "react";

import {
  type Accounts,
  addEvent,
  btc,
  type Choice,
  type EventInput,
  type EventKind,
  getAccounts,
  getEvents,
  identifyLots,
  removeEvent,
  satsOf,
  type StoredEvent,
} from "../api/client";

const KINDS: { kind: EventKind; label: string }[] = [
  { kind: "buy", label: "Buy (exchange)" },
  { kind: "p2p_buy", label: "Buy (peer to peer)" },
  { kind: "income", label: "Income" },
  { kind: "gift_in", label: "Gift received" },
  { kind: "inherit", label: "Inheritance" },
  { kind: "sell", label: "Sell" },
  { kind: "spend", label: "Spend" },
  { kind: "gift_out", label: "Gift given" },
  { kind: "deposit", label: "Deposit to an exchange" },
  { kind: "withdrawal", label: "Withdrawal from an exchange" },
  { kind: "self_transfer", label: "Transfer between wallets" },
];
const MOVEMENTS: EventKind[] = ["deposit", "withdrawal", "self_transfer"];
const NO_USD: EventKind[] = [...MOVEMENTS, "gift_in", "gift_out"]; // a gift given's proceeds are 0.00
const FEE: EventKind[] = [...MOVEMENTS, "buy"]; // a BTC fee on a sale, spend or gift isn't supported yet
const CHOOSES: EventKind[] = [...MOVEMENTS, "sell", "spend", "gift_out"];

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

const label = (kind: EventKind) => KINDS.find((k) => k.kind === kind)?.label ?? kind;

/** A choice of lots typed one per line as `lot BTC`; null if a line isn't one. */
function choicesOf(text: string): Choice[] | null {
  const lines = text.split("\n").map((l) => l.trim()).filter((l) => l !== "");
  const out: Choice[] = [];
  for (const line of lines) {
    const [lot, amount, extra] = line.split(/\s+/);
    const sats = amount === undefined ? null : satsOf(amount);
    if (lot === undefined || sats === null || extra !== undefined) return null;
    out.push({ lot, sats });
  }
  return out;
}

export function Events({ session }: { session: string }) {
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [events, setEvents] = useState<StoredEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<EventKind>("buy");
  const [account, setAccount] = useState("");
  const [to, setTo] = useState("");
  const [at, setAt] = useState("");
  const [amount, setAmount] = useState("");
  const [usd, setUsd] = useState("");
  const [fee, setFee] = useState("");
  const [note, setNote] = useState("");
  const [fmv, setFmv] = useState("");
  const [donorDate, setDonorDate] = useState("");
  const [choosing, setChoosing] = useState<number | null>(null);
  const [lots, setLots] = useState("");

  const load = useCallback(async () => {
    try {
      const [acc, ev] = await Promise.all([getAccounts(session), getEvents(session)]);
      setAccounts(acc);
      setEvents(ev.events);
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await load();
      return true;
    } catch (e) {
      setError(message(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function onAdd() {
    const sats = satsOf(amount);
    const feeSats = !FEE.includes(kind) || fee.trim() === "" ? 0 : satsOf(fee);
    if (sats === null || feeSats === null) return setError("Enter BTC amounts such as 0.015.");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(at)) return setError("Enter when it happened, in UTC.");
    const event: EventInput = {
      kind,
      account: Number(account),
      at: `${at}:00Z`,
      sats,
      usd: kind === "gift_out" ? "0.00" : NO_USD.includes(kind) || usd.trim() === "" ? null : usd.trim(),
      to: MOVEMENTS.includes(kind) ? Number(to) : null,
      fee_sats: feeSats,
      note: note.trim() === "" ? null : note.trim(),
      ...(kind === "gift_in" ? { fmv: fmv.trim(), donor_acquired: donorDate } : {}),
    };
    if (await run(() => addEvent(session, event))) {
      setAmount("");
      setUsd("");
      setFee("");
      setNote("");
      setFmv("");
      setDonorDate("");
    }
  }

  async function onChoose(id: number) {
    const choices = choicesOf(lots);
    if (choices === null) return setError("Enter one lot per line, as its id and BTC amount.");
    if (await run(() => identifyLots(session, id, choices))) setChoosing(null);
  }

  if (!accounts) return <section id="events">{error ? <p>{error}</p> : <p>Loading…</p>}</section>;
  const name = (id: number | null) => accounts.tax_accounts.find((t) => t.id === id)?.name ?? "";
  const options = accounts.tax_accounts.map((t) => (
    <option key={t.id} value={t.id}>
      {t.name}
    </option>
  ));

  return (
    <section id="events">
      <h2>Events</h2>
      <button id="events-refresh" type="button" disabled={busy} onClick={() => void load()}>
        Refresh
      </button>
      {error && <p id="events-error">{error}</p>}
      <fieldset id="new-event">
        <legend>Add an event</legend>
        <select id="event-kind" value={kind} onChange={(e) => setKind(e.target.value as EventKind)}>
          {KINDS.map((k) => (
            <option key={k.kind} value={k.kind}>
              {k.label}
            </option>
          ))}
        </select>
        <select id="event-account" aria-label="Account" value={account} onChange={(e) => setAccount(e.target.value)}>
          <option value="">Account…</option>
          {options}
        </select>
        {MOVEMENTS.includes(kind) && (
          <select id="event-to" aria-label="To account" value={to} onChange={(e) => setTo(e.target.value)}>
            <option value="">To account…</option>
            {options}
          </select>
        )}
        <input id="event-at" type="datetime-local" aria-label="When (UTC)" value={at} onChange={(e) => setAt(e.target.value)} />
        <input id="event-btc" aria-label="BTC" placeholder="BTC" value={amount} onChange={(e) => setAmount(e.target.value)} />
        {!NO_USD.includes(kind) && (
          <input id="event-usd" aria-label="USD" placeholder="USD" value={usd} onChange={(e) => setUsd(e.target.value)} />
        )}
        {kind === "gift_in" && (
          <>
            <input id="event-fmv" aria-label="USD value at the gift" placeholder="USD value at the gift" value={fmv} onChange={(e) => setFmv(e.target.value)} />
            <input id="event-donor-date" type="date" aria-label="When the donor acquired it" value={donorDate} onChange={(e) => setDonorDate(e.target.value)} />
          </>
        )}
        {FEE.includes(kind) && (
          <input id="event-fee" aria-label="Fee (BTC)" placeholder="Fee (BTC)" value={fee} onChange={(e) => setFee(e.target.value)} />
        )}
        <input id="event-note" aria-label="Note" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
        <button id="event-add" type="button" disabled={busy || account === ""} onClick={() => void onAdd()}>
          Add
        </button>
      </fieldset>

      <table id="events-list">
        <thead>
          <tr>
            <th>#</th>
            <th>When (UTC)</th>
            <th>Kind</th>
            <th>Account</th>
            <th>BTC</th>
            <th>USD</th>
            <th>Lots chosen</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td className="id">{e.id}</td>
              <td>{e.at}</td>
              <td className="kind">{label(e.kind)}</td>
              <td>
                {name(e.account)}
                {e.to !== null ? ` → ${name(e.to)}` : ""}
              </td>
              <td className="btc">{btc(e.sats)}</td>
              <td className="usd">{e.usd ?? ""}</td>
              <td className="choices">
                {e.choices.map((c) => `${c.lot} ${btc(c.sats)}`).join(", ")}
                {e.identified_at !== null ? ` (chosen ${e.identified_at})` : ""}
              </td>
              <td>
                {CHOOSES.includes(e.kind) && (
                  <button type="button" className="choose" disabled={busy} onClick={() => setChoosing(e.id)}>
                    Choose lots
                  </button>
                )}
                <button type="button" className="remove" disabled={busy} onClick={() => void run(() => removeEvent(session, e.id))}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {choosing !== null && (
        <section id="choose-lots">
          <h3>Choose lots for event {choosing}</h3>
          <p>One lot per line: its id (the event that bought it) and the BTC taken from it. Leave it empty to use the account's standing method.</p>
          <textarea id="choose-lots-text" value={lots} onChange={(e) => setLots(e.target.value)} />
          <button id="choose-lots-save" type="button" disabled={busy} onClick={() => void onChoose(choosing)}>
            Save choice
          </button>
        </section>
      )}
    </section>
  );
}
