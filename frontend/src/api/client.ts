// The SPA's only way to the backend (architecture §4). Same origin only: the CSP's connect-src is
// 'self'. The session token lives in sessionStorage, scoped to this origin and tab; no cookies.

const SESSION_KEY = "session";
// The launch file sends the browser here with the one-time token in the fragment (T-110).
const BOOTSTRAP = /^#bootstrap=([A-Za-z0-9_-]{1,200})$/;

export type NodeStatus = { online: boolean; chain: string | null; reasons: string[] };

export class SessionError extends Error {}

/** Removes a launch token from the address bar and history, and returns it. Called once, before
 * the first render (T-110). Any `#bootstrap` fragment is removed, even a malformed one. */
export function takeBootstrapToken(): string | null {
  const hash = window.location.hash;
  if (!hash.startsWith("#bootstrap")) return null;
  window.history.replaceState(null, "", window.location.pathname);
  return BOOTSTRAP.exec(hash)?.[1] ?? null;
}

/** Claims the session with the launch token, if there is one, then returns the session token. */
export async function claimSession(bootstrap: string | null): Promise<string> {
  if (bootstrap) {
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bootstrap }),
    });
    const data: { session?: string; error?: string } = await response.json().catch(() => ({}));
    if (!response.ok || !data.session) {
      throw new SessionError(data.error ?? "The session could not be started. Restart the app.");
    }
    window.sessionStorage.setItem(SESSION_KEY, data.session);
  }
  const session = window.sessionStorage.getItem(SESSION_KEY);
  if (!session) {
    throw new SessionError("No session. Restart the app.");
  }
  return session;
}

function auth(session: string): Record<string, string> {
  return { Authorization: `Bearer ${session}` };
}

export async function getStatus(session: string): Promise<NodeStatus> {
  const response = await fetch("/api/status", { headers: auth(session) });
  if (!response.ok) {
    throw new SessionError("The session was not accepted. Restart the app.");
  }
  return (await response.json()) as NodeStatus;
}

/** Asks the app to stop. Resolves only once the app has accepted (202); a refusal throws, so the
 * page never says the app stopped while it is still running. */
export async function quit(session: string): Promise<void> {
  const response = await fetch("/api/quit", {
    method: "POST",
    headers: { ...auth(session), "Content-Type": "application/json" },
    body: "{}",
  });
  if (!response.ok) {
    throw new SessionError(`The app refused to quit (${response.status}). Close it from the terminal.`);
  }
}

// --- Accounts and imports (PLAN §3; the routes in backend/coinacct/api/app.py) ----------------------

export type Entity = { id: number; name: string; kind: string; knows_identity: boolean };
export type TaxAccount = { id: number; name: string; kind: "self_custody" | "custodial"; entity_id: number | null };
export type WalletClient = { id: number; name: string; kind: string };
export type Accounts = { online: boolean; entities: Entity[]; tax_accounts: TaxAccount[]; clients: WalletClient[] };

export type Known = { script: string; address: string | null; entity_id: number; tax_account_id: number | null };
export type AddressPreview = {
  new: { script: string; address: string }[];
  known: Known[];
  repeated: number;
  invalid_lines: number[];
};
export type DescriptorPreview = {
  descriptor: string;
  ranged: boolean;
  gap_limit: number;
  derived: { index: number; script: string; address: string }[];
  already_imported: boolean;
  known: Known[];
};
export type Owner = {
  entity_id: number;
  tax_account_id: number | null;
  label?: string;
  start_height?: number;
  client_ids?: number[];
};

/** A refusal from the API: its message is the server's fixed text, which never repeats the input. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(session: string, method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? auth(session) : { ...auth(session), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) throw new SessionError("The session was not accepted. Restart the app.");
  if (response.status === 413) throw new ApiError(413, "That upload is too large. Import the list in parts.");
  const data: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = (data as { error?: unknown }).error;
    throw new ApiError(response.status, typeof error === "string" ? error : `The app refused (${response.status}).`);
  }
  return data as T;
}

export const getAccounts = (session: string) => call<Accounts>(session, "GET", "/api/accounts");

export const addTaxAccount = (session: string, name: string, kind: TaxAccount["kind"], entityId?: number) =>
  call<{ id: number }>(session, "POST", "/api/tax-accounts", { name, kind, entity_id: entityId ?? null });

export const addClient = (session: string, name: string, kind: string) =>
  call<{ id: number }>(session, "POST", "/api/clients", { name, kind });

export const addEntity = (session: string, name: string, kind: string) =>
  call<{ id: number }>(session, "POST", "/api/entities", { name, kind });

export const previewAddresses = (session: string, text: string) =>
  call<AddressPreview>(session, "POST", "/api/imports/addresses/preview", { text });

export const importAddresses = (session: string, text: string, owner: Owner) =>
  call<{ added: string[]; conflicts: string[] }>(session, "POST", "/api/imports/addresses", { text, ...owner });

export const previewDescriptor = (session: string, text: string, gapLimit: number) =>
  call<DescriptorPreview>(session, "POST", "/api/imports/descriptor/preview", { text, gap_limit: gapLimit });

export const importDescriptor = (session: string, text: string, gapLimit: number, owner: Owner) =>
  call<{ id: number }>(session, "POST", "/api/imports/descriptor", { text, gap_limit: gapLimit, ...owner });

// --- History (the routes of services/history; PLAN §3) ---------------------------------------------

export type Tip = { blockhash: string; height: number };
export type AddressSummary = {
  script: string;
  address: string | null;
  entity_id: number;
  tax_account_id: number | null;
  label: string;
  balance: number;
  utxos: number;
  received: number;
  transactions: number;
  last_height: number | null;
  scanned_to: number | null;
};
export type Addresses = { as_of: Tip | null; catching_up: boolean; addresses: AddressSummary[] };
export type HistoryEvent = {
  kind: "receive" | "spend";
  txid: string;
  n: number;
  sats: number;
  height: number;
  blockhash: string;
  prevout: { txid: string; vout: number } | null;
};
export type Utxo = {
  txid: string;
  vout: number;
  sats: number;
  script: string;
  address: string | null;
  height: number;
  complete: boolean;
};

export const getAddresses = (session: string) => call<Addresses>(session, "GET", "/api/addresses");

/** One address's events. A POST, so its script never appears in a URL (T-105). */
export const getAddressEvents = (session: string, script: string) =>
  call<{ events: HistoryEvent[] }>(session, "POST", "/api/addresses/events", { script });

export const getUtxos = (session: string) => call<{ utxos: Utxo[] }>(session, "GET", "/api/utxos");

/** Sats as BTC with eight decimals, by integer arithmetic only (no floats near amounts). */
export function btc(sats: number): string {
  const negative = sats < 0;
  const digits = String(Math.abs(Math.trunc(sats))).padStart(9, "0");
  return `${negative ? "-" : ""}${digits.slice(0, -8)}.${digits.slice(-8)}`;
}
