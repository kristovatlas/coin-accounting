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
const NO_USD: EventKind[] = [...MOVEMENTS, "gift_out"]; // a gift given's proceeds are 0.00
const USD = /^-?\d{1,15}(\.\d{1,2})?$/; // the API's USD text: exact cents
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
  const [alwaysLong, setAlwaysLong] = useState(false);
  const [missingBasis, setMissingBasis] = useState("");
  const [missingDate, setMissingDate] = useState("");
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
    const feeSats = !FEE.includes(kind) || fee.trim() === "" || /^0*\.?0*$/.test(fee.trim()) ? 0 : satsOf(fee);
    if (sats === null || feeSats === null) return setError("Enter BTC amounts such as 0.015.");
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(at)) return setError("Enter when it happened, in UTC.");
    if (kind === "withdrawal" && (missingBasis.trim() !== "" || missingDate !== "")) {
      if (!USD.test(missingBasis.trim()) || missingDate === "")
        return setError("For sats the account has no lots for, enter both their USD basis and when they were acquired.");
    }
    if (MOVEMENTS.includes(kind) && to === "") return setError("Choose the account it went to.");
    if (!NO_USD.includes(kind) && usd.trim() !== "" && !USD.test(usd.trim()))
      return setError("Enter USD amounts such as 1250.50, without $ or commas.");
    if (!NO_USD.includes(kind) && kind !== "gift_in" && usd.trim() === "") return setError("Enter the USD amount.");
    if (kind === "gift_in" && (!USD.test(fmv.trim()) || donorDate === ""))
      return setError("Enter the gift's USD value and when the donor acquired it.");
    const event: EventInput = {
      kind,
      account: Number(account),
      at: `${at.length === 16 ? `${at}:00` : at}Z`, // seconds kept when given
      sats,
      usd: kind === "gift_out" ? "0.00" : NO_USD.includes(kind) || usd.trim() === "" ? null : usd.trim(),
      to: MOVEMENTS.includes(kind) ? Number(to) : null,
      fee_sats: feeSats,
      note: note.trim() === "" ? null : note.trim(),
      // a gift received's USD amount is the donor's basis; empty means unknown, which blocks reports
      ...(kind === "gift_in" ? { fmv: fmv.trim(), donor_acquired: donorDate, donor_always_long: alwaysLong } : {}),
      // a withdrawal of sats its account has no lots for names their basis and date (PLAN §7)
      ...(kind === "withdrawal" && missingBasis.trim() !== ""
        ? { missing_basis: missingBasis.trim(), missing_acquired: missingDate }
        : {}),
    };
    if (await run(() => addEvent(session, event))) {
      setAmount("");
      setUsd("");
      setFee("");
      setNote("");
      setFmv("");
      setDonorDate("");
      setAlwaysLong(false);
      setMissingBasis("");
      setMissingDate("");
    }
  }

  /** Open the choice panel on `e`, filled with its current choice, so saving unchanged changes nothing. */
  function openChoice(e: StoredEvent) {
    setChoosing(e.id);
    setLots(e.choices.map((c) => `${c.lot} ${btc(c.sats)}`).join("\n"));
  }

  const current = events.find((e) => e.id === choosing);
  const currentText = current ? current.choices.map((c) => `${c.lot} ${btc(c.sats)}`).join("\n") : "";

  async function onChoose(id: number) {
    const choices = choicesOf(lots);
    if (choices === null) return setError("Enter one lot per line, as its id and BTC amount.");
    if (await run(() => identifyLots(session, id, choices))) setChoosing(null);
  }

  if (!accounts)
    return (
      <section id="events">
        {error ? <p id="events-error">{error}</p> : <p>Loading…</p>}
        {error && (
          <button id="events-refresh" type="button" onClick={() => void load()}>
            Refresh
          </button>
        )}
      </section>
    );
  const name = (id: number | null) => accounts.tax_accounts.find((t) => t.id === id)?.name ?? "";
  // an account whose standing method selects its lots (ADR 0021 §1) shows no lot picker
  const selectsItself = (id: number) => {
    const a = accounts.tax_accounts.find((t) => t.id === id);
    return a !== undefined && a.automatic !== false && (a.standing_method ?? "fifo") === "fifo";
  };
  const custodial = (id: number) => accounts.tax_accounts.find((t) => t.id === id)?.kind === "custodial";
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
        <input autoComplete="off" id="event-at" type="datetime-local" step={1} aria-label="When (UTC)" value={at} onChange={(e) => setAt(e.target.value)} />
        <input autoComplete="off" id="event-btc" aria-label="BTC" placeholder="BTC" value={amount} onChange={(e) => setAmount(e.target.value)} />
        {!NO_USD.includes(kind) && (
          <input
            autoComplete="off"
            id="event-usd"
            aria-label={kind === "gift_in" ? "Donor's basis (USD), empty if unknown" : "USD"}
            placeholder={kind === "gift_in" ? "Donor's basis (USD), empty if unknown" : "USD"}
            value={usd}
            onChange={(e) => setUsd(e.target.value)}
          />
        )}
        {kind === "gift_in" && (
          <>
            <input autoComplete="off" id="event-fmv" aria-label="USD value at the gift" placeholder="USD value at the gift" value={fmv} onChange={(e) => setFmv(e.target.value)} />
            <input autoComplete="off" id="event-donor-date" type="date" aria-label="When the donor acquired it" value={donorDate} onChange={(e) => setDonorDate(e.target.value)} />
            <label>
              <input id="event-always-long" type="checkbox" checked={alwaysLong} onChange={(e) => setAlwaysLong(e.target.checked)} />
              The donor's lot was always long-term
            </label>
          </>
        )}
        {kind === "withdrawal" && (
          <>
            <input
              autoComplete="off"
              id="event-missing-basis"
              aria-label="USD basis of sats the account has no lots for (optional)"
              placeholder="Basis of sats without lots (USD, optional)"
              value={missingBasis}
              onChange={(e) => setMissingBasis(e.target.value)}
            />
            <input
              autoComplete="off"
              id="event-missing-date"
              type="date"
              aria-label="When those sats were acquired"
              value={missingDate}
              onChange={(e) => setMissingDate(e.target.value)}
            />
          </>
        )}
        {FEE.includes(kind) && (
          <input autoComplete="off" id="event-fee" aria-label="Fee (BTC)" placeholder="Fee (BTC)" value={fee} onChange={(e) => setFee(e.target.value)} />
        )}
        <input autoComplete="off" id="event-note" aria-label="Note" placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} />
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
                {CHOOSES.includes(e.kind) && !selectsItself(e.account) && (
                  <button type="button" className="choose" disabled={busy} onClick={() => openChoice(e)}>
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

      {current !== undefined && (
        <section id="choose-lots">
          <h3>Choose lots for event {current.id}</h3>
          <p>
            One lot per line: its id (the event that bought it) and the BTC taken from it. Leave it empty to use the
            account's standing method. The app records when you save a change: one made after the event may count
            as late.
          </p>
          {custodial(current.account) && current.at >= "2027" && (
            <p id="choose-lots-broker">
              From 2027 your broker must have this identification by the time of the sale: tell them, not only this
              app (ADR 0008).
            </p>
          )}
          <textarea autoComplete="off" id="choose-lots-text" value={lots} onChange={(e) => setLots(e.target.value)} />
          <button
            id="choose-lots-save"
            type="button"
            disabled={busy || lots.trim() === currentText}
            onClick={() => void onChoose(current.id)}
          >
            Save choice
          </button>
          <button id="choose-lots-cancel" type="button" onClick={() => setChoosing(null)}>
            Cancel
          </button>
        </section>
      )}
    </section>
  );
}
