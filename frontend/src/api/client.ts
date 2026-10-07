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
