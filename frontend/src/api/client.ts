// The SPA's only way to the backend (architecture §4). Same origin only: the CSP's connect-src is
// 'self'. The session token lives in sessionStorage, scoped to this origin and tab; no cookies.

const SESSION_KEY = "session";
// The launch file sends the browser here with the one-time token in the fragment (T-110).
const BOOTSTRAP = /^#bootstrap=([A-Za-z0-9_-]{1,200})$/;

export type NodeStatus = { online: boolean; chain: string | null; reasons: string[] };

export class SessionError extends Error {}

/** Claims the session if the address carries a launch token, then returns the session token.
 * The token is dropped from the address bar and history before the request is made (T-110). */
export async function claimSession(): Promise<string> {
  const match = BOOTSTRAP.exec(window.location.hash);
  if (match) {
    window.history.replaceState(null, "", window.location.pathname);
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bootstrap: match[1] }),
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

export async function quit(session: string): Promise<void> {
  await fetch("/api/quit", {
    method: "POST",
    headers: { ...auth(session), "Content-Type": "application/json" },
    body: "{}",
  }).catch(() => undefined);
}
