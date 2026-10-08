// Importing addresses and public descriptors (PLAN §3; THREAT_MODEL T-203, T-701, T-703).
// Two steps, as the API requires: a preview of exactly what the upload holds, then the import of that
// same upload once the user confirms it. React renders every value as text (T-104), and nothing is
// kept beyond this view: the upload stays in this component's state only.
import { useEffect, useState } from "react";

import {
  type Accounts,
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
type Preview = { kind: "addresses"; result: AddressPreview } | { kind: "descriptor"; result: DescriptorPreview };

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

function KnownList({ known, accounts }: { known: Known[]; accounts: Accounts }) {
  if (known.length === 0) return null;
  const owner = (k: Known) => {
    if (k.entity_id !== ME) return accounts.entities.find((e) => e.id === k.entity_id)?.name ?? "another owner";
    return accounts.tax_accounts.find((a) => a.id === k.tax_account_id)?.name ?? "one of your accounts";
  };
  return (
    <section id="preview-known">
      <h3>Already known ({known.length})</h3>
      <p>These stay with their current owner and account; an import never moves them.</p>
      <ul>
        {known.slice(0, SHOWN).map((k) => (
          <li key={k.script}>
            {k.address ?? k.script} — {owner(k)}
          </li>
        ))}
      </ul>
    </section>
  );
}

function PreviewView({ preview, accounts }: { preview: Preview; accounts: Accounts }) {
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
        {p.new.length > SHOWN && <p>…and {p.new.length - SHOWN} more.</p>}
        <KnownList known={p.known} accounts={accounts} />
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
      {p.already_imported && <p id="preview-already">This descriptor is already imported; importing it again only adds wallet links.</p>}
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
      <KnownList known={p.known} accounts={accounts} />
    </section>
  );
}

export function Imports({ session }: { session: string }) {
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [account, setAccount] = useState<number | null>(null);
  const [newWallet, setNewWallet] = useState("");
  const [kind, setKind] = useState<Kind>("addresses");
  const [text, setText] = useState("");
  const [gapLimit, setGapLimit] = useState(20);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  const owner = (): Owner => ({ entity_id: ME, tax_account_id: account });

  const onPreview = () =>
    run(async () => {
      setResult(null);
      setPreview(
        kind === "addresses"
          ? { kind, result: await previewAddresses(session, text) }
          : { kind, result: await previewDescriptor(session, text, gapLimit) },
      );
    });

  const onImport = () =>
    run(async () => {
      if (kind === "addresses") {
        const done = await importAddresses(session, text, owner());
        setResult(
          `Imported ${done.added.length} address${done.added.length === 1 ? "" : "es"}.` +
            (done.conflicts.length ? ` ${done.conflicts.length} belong to another owner or account and were left as they are.` : "") +
            " They are being scanned.",
        );
      } else {
        await importDescriptor(session, text, gapLimit, owner());
        setResult("Imported the descriptor. Its addresses are being scanned.");
      }
      setPreview(null);
      setText("");
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
        {wallets.length > 0 ? (
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
        <legend>What</legend>
        <label>
          <input
            type="radio"
            name="import-kind"
            id="kind-addresses"
            checked={kind === "addresses"}
            onChange={() => {
              setKind("addresses");
              setPreview(null);
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
            disabled={!accounts.online}
            onChange={() => {
              setKind("descriptor");
              setPreview(null);
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
              onChange={(e) => setGapLimit(Number(e.target.value))}
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
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
      />
      <div>
        <button id="preview-button" type="button" disabled={busy || text.trim() === ""} onClick={onPreview}>
          Preview
        </button>
        <button id="import-button" type="button" disabled={busy || preview === null || account === null} onClick={onImport}>
          Import
        </button>
      </div>
      {error && <p id="import-error">{error}</p>}
      {result && <p id="import-result">{result}</p>}
      {preview && <PreviewView preview={preview} accounts={accounts} />}
    </section>
  );
}
