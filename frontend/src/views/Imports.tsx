// Importing addresses and public descriptors (PLAN §3; THREAT_MODEL T-203, T-701, T-703).
// Two steps, as the API requires: a preview of exactly what the upload holds, then the import of that
// same upload once the user confirms it. React renders every value as text (T-104), and nothing is
// kept beyond this view: the upload stays in this component's state only.
import { useEffect, useRef, useState } from "react";

import {
  type Accounts,
  addClient,
  addTaxAccount,
  type AddressPreview,
  type DescriptorPreview,
  getAccounts,
  importAddresses,
  importDescriptor,
  type Known,
  type Owner,
  previewAddresses,
  previewDescriptor,
} from "../api/client";

const ME = 1; // the user entity, seeded by the schema
const SHOWN = 20; // previews list this many entries, then a count

type Kind = "addresses" | "descriptor";
// What was previewed is what gets imported: the upload, its kind and its gap limit are kept with the
// result, and Import sends these, never the form's current values (T-701).
type Upload = { text: string; gapLimit: number };
type Preview =
  | ({ kind: "addresses"; result: AddressPreview } & Upload)
  | ({ kind: "descriptor"; result: DescriptorPreview } & Upload);

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

function More({ total }: { total: number }) {
  return total > SHOWN ? <p>…and {total - SHOWN} more.</p> : null;
}

type Target = { entity_id: number; tax_account_id: number | null };

function KnownList({ known, accounts, target }: { known: Known[]; accounts: Accounts; target: Target }) {
  if (known.length === 0) return null;
  const where = (k: Known) => {
    if (k.entity_id !== ME) return accounts.entities.find((e) => e.id === k.entity_id)?.name ?? "another owner";
    return accounts.tax_accounts.find((a) => a.id === k.tax_account_id)?.name ?? "one of your accounts";
  };
  const isConflict = (k: Known) => k.entity_id !== target.entity_id || k.tax_account_id !== target.tax_account_id;
  const conflicts = known.filter(isConflict).length;
  return (
    <section id="preview-known">
      <h3>Already known ({known.length})</h3>
      <p>
        An import never moves these.
        {conflicts > 0 && ` ${conflicts} belong to another owner or account and will stay there.`}
      </p>
      <ul>
        {known.slice(0, SHOWN).map((k) => (
          <li key={k.script} className={isConflict(k) ? "conflict" : undefined}>
            {k.address ?? k.script} — {isConflict(k) ? `stays with ${where(k)}` : `already in ${where(k)}`}
          </li>
        ))}
      </ul>
      <More total={known.length} />
    </section>
  );
}

function PreviewView({ preview, accounts, target }: { preview: Preview; accounts: Accounts; target: Target }) {
  if (preview.kind === "addresses") {
    const p = preview.result;
    return (
      <section id="preview">
        <h3>New addresses ({p.new.length})</h3>
        <ul id="preview-new">
          {p.new.slice(0, SHOWN).map((a) => (
            <li key={a.script}>{a.address}</li>
          ))}
        </ul>
        <More total={p.new.length} />
        <KnownList known={p.known} accounts={accounts} target={target} />
        {p.repeated > 0 && <p>{p.repeated} repeated lines are counted once.</p>}
        {p.invalid_lines.length > 0 && (
          <p id="preview-invalid">
            Not addresses of this chain, so skipped: line{p.invalid_lines.length > 1 ? "s" : ""}{" "}
            {p.invalid_lines.slice(0, SHOWN).join(", ")}
            {p.invalid_lines.length > SHOWN ? ", …" : ""}.
          </p>
        )}
      </section>
    );
  }
  const p = preview.result;
  return (
    <section id="preview">
      {p.already_imported && (
        <p id="preview-already">This descriptor is already imported; importing it again only adds the chosen wallet clients.</p>
      )}
      <p>
        {p.ranged ? `Ranged: the first ${p.gap_limit} addresses are scanned, and more as they are used.` : "One address."}
      </p>
      <ul id="preview-new">
        {p.derived.slice(0, SHOWN).map((d) => (
          <li key={d.script}>
            {d.index}: {d.address}
          </li>
        ))}
      </ul>
      <More total={p.derived.length} />
      <KnownList known={p.known} accounts={accounts} target={target} />
    </section>
  );
}

function clampGap(value: string): number {
  const n = Math.trunc(Number(value));
  return Number.isFinite(n) ? Math.min(1000, Math.max(1, n)) : 20;
}

export function Imports({ session }: { session: string }) {
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [entity, setEntity] = useState(ME);
  const [account, setAccount] = useState<number | null>(null);
  const [newWallet, setNewWallet] = useState("");
  const [chosenClients, setChosenClients] = useState<number[]>([]);
  const [newClient, setNewClient] = useState("");
  const [newClientKind, setNewClientKind] = useState("hardware");
  const [label, setLabel] = useState("");
  const [startHeight, setStartHeight] = useState(0);
  const [kind, setKind] = useState<Kind>("addresses");
  const [text, setText] = useState("");
  const [gapLimit, setGapLimit] = useState(20);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Bumped by every change to the form: a preview that answers after one is dropped.
  const edition = useRef(0);

  function changed() {
    setPreview(null);
    edition.current += 1;
  }

  async function reload(select?: number) {
    const loaded = await getAccounts(session);
    setAccounts(loaded);
    const wallets = loaded.tax_accounts.filter((a) => a.kind === "self_custody");
    setAccount(select ?? wallets[0]?.id ?? null);
  }

  useEffect(() => {
    let cancelled = false;
    getAccounts(session)
      .then((loaded) => {
        if (cancelled) return;
        setAccounts(loaded);
        setAccount(loaded.tax_accounts.find((a) => a.kind === "self_custody")?.id ?? null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(message(e));
      });
    return () => {
      cancelled = true;
    };
  }, [session]);

  async function run(step: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await step();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  // Your own addresses go into one of your wallets; another owner's (a third party's) into none.
  const target: Target = { entity_id: entity, tax_account_id: entity === ME ? account : null };
  const owner = (): Owner => ({ ...target, label, start_height: startHeight, client_ids: chosenClients });

  const onPreview = () =>
    run(async () => {
      setResult(null);
      const asked = edition.current;
      const next: Preview =
        kind === "addresses"
          ? { kind, text, gapLimit, result: await previewAddresses(session, text) }
          : { kind, text, gapLimit, result: await previewDescriptor(session, text, gapLimit) };
      if (edition.current === asked) setPreview(next); // the form hasn't changed since the request
    });

  const onImport = (previewed: Preview) =>
    run(async () => {
      if (previewed.kind === "addresses") {
        const done = await importAddresses(session, previewed.text, owner());
        const total = previewed.result.new.length + previewed.result.known.length;
        const kept = Math.max(0, total - done.added.length - done.conflicts.length);
        const parts = [`Imported ${done.added.length} new address${done.added.length === 1 ? "" : "es"}.`];
        if (kept > 0) parts.push(`${kept} were already there.`);
        if (done.conflicts.length > 0) parts.push(`${done.conflicts.length} belong to another owner or account and were left as they are.`);
        if (done.added.length + kept > 0) parts.push("They are being scanned.");
        setResult(parts.join(" "));
      } else {
        await importDescriptor(session, previewed.text, previewed.gapLimit, owner());
        setResult("Imported the descriptor. Its addresses are being scanned.");
      }
      setPreview(null);
      setText("");
    });

  const onNewClient = () =>
    run(async () => {
      const { id } = await addClient(session, newClient, newClientKind);
      setNewClient("");
      setAccounts(await getAccounts(session));
      setChosenClients((chosen) => [...chosen, id]);
    });

  const onNewWallet = () =>
    run(async () => {
      const { id } = await addTaxAccount(session, newWallet, "self_custody");
      setNewWallet("");
      await reload(id);
    });

  if (!accounts) return <section id="imports">{error ? <p id="import-error">{error}</p> : <p>Loading…</p>}</section>;
  const wallets = accounts.tax_accounts.filter((a) => a.kind === "self_custody");

  return (
    <section id="imports">
      <h2>Import</h2>
      <fieldset>
        <legend>Into</legend>
        <label>
          Owner{" "}
          <select id="import-owner" value={entity} disabled={busy} onChange={(e) => setEntity(Number(e.target.value))}>
            {accounts.entities.map((e) => (
              <option key={e.id} value={e.id}>
                {e.id === ME ? "Me" : e.name}
              </option>
            ))}
          </select>
        </label>
        {entity !== ME ? (
          <p>Another owner's addresses are tagged to them, outside your wallets (for privacy and history).</p>
        ) : wallets.length > 0 ? (
          <select id="import-account" value={account ?? ""} onChange={(e) => setAccount(Number(e.target.value))}>
            {wallets.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
        ) : (
          <p>Add a wallet first: its addresses are yours, and its coins are counted together for tax.</p>
        )}
        <input
          id="new-wallet-name"
          autoComplete="off"
          placeholder="New wallet name"
          value={newWallet}
          maxLength={200}
          onChange={(e) => setNewWallet(e.target.value)}
        />
        <button id="new-wallet" type="button" disabled={busy || newWallet.trim() === ""} onClick={onNewWallet}>
          Add wallet
        </button>
      </fieldset>

      <fieldset>
        <legend>Wallet clients (the apps or devices that hold these)</legend>
        {accounts.clients.map((c) => (
          <label key={c.id}>
            <input
              type="checkbox"
              className="import-client"
              checked={chosenClients.includes(c.id)}
              disabled={busy}
              onChange={(e) =>
                setChosenClients((chosen) => (e.target.checked ? [...chosen, c.id] : chosen.filter((id) => id !== c.id)))
              }
            />
            {c.name}
          </label>
        ))}
        <input
          id="new-client-name"
          autoComplete="off"
          placeholder="New client name"
          value={newClient}
          maxLength={200}
          onChange={(e) => setNewClient(e.target.value)}
        />
        <select id="new-client-kind" value={newClientKind} onChange={(e) => setNewClientKind(e.target.value)}>
          {["hardware", "mobile", "desktop", "web", "paper", "other"].map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <button id="new-client" type="button" disabled={busy || newClient.trim() === ""} onClick={onNewClient}>
          Add client
        </button>
        <label>
          Label{" "}
          <input id="import-label" autoComplete="off" value={label} maxLength={200} disabled={busy} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label>
          Scan from block{" "}
          <input
            id="import-start"
            type="number"
            min={0}
            value={startHeight}
            disabled={busy}
            onChange={(e) => setStartHeight(Math.max(0, Math.trunc(Number(e.target.value)) || 0))}
          />
        </label>
      </fieldset>

      <fieldset>
        <legend>What</legend>
        <label>
          <input
            type="radio"
            name="import-kind"
            id="kind-addresses"
            checked={kind === "addresses"}
            disabled={busy}
            onChange={() => {
              setKind("addresses");
              changed();
            }}
          />
          Addresses, one per line
        </label>
        <label>
          <input
            type="radio"
            name="import-kind"
            id="kind-descriptor"
            checked={kind === "descriptor"}
            disabled={busy || !accounts.online}
            onChange={() => {
              setKind("descriptor");
              changed();
            }}
          />
          A public descriptor (xpub){accounts.online ? "" : " — needs the node; the app is offline"}
        </label>
        {kind === "descriptor" && (
          <label>
            Gap limit{" "}
            <input
              id="gap-limit"
              type="number"
              min={1}
              max={1000}
              value={gapLimit}
              disabled={busy}
              onChange={(e) => {
                setGapLimit(clampGap(e.target.value));
                changed();
              }}
            />
          </label>
        )}
      </fieldset>

      <p>Public data only: a private key (xprv, WIF) anywhere refuses the whole upload, and it is never stored.</p>
      <textarea
        id="import-text"
        rows={8}
        cols={80}
        spellCheck={false}
        autoComplete="off"
        value={text}
        disabled={busy}
        onChange={(e) => {
          setText(e.target.value);
          changed();
        }}
      />
      <div>
        <button id="preview-button" type="button" disabled={busy || text.trim() === ""} onClick={onPreview}>
          Preview
        </button>
        <button
          id="import-button"
          type="button"
          disabled={busy || preview === null || (entity === ME && account === null)}
          onClick={() => preview && onImport(preview)}
        >
          Import
        </button>
      </div>
      {error && <p id="import-error">{error}</p>}
      {result && <p id="import-result">{result}</p>}
      {preview && <PreviewView preview={preview} accounts={accounts} target={target} />}
    </section>
  );
}
