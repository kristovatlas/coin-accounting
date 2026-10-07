// The app shell (M0.3): claims the session, shows the node status and offers Quit. React renders
// all text, so chain data and error messages are always escaped (T-104); no inline styles (CSP).
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import { claimSession, getStatus, quit, SessionError, takeBootstrapToken, type NodeStatus } from "./api/client";

// Before anything renders: the token leaves the address bar at once (T-110), and the claim runs
// exactly once, however often React runs the effect below (StrictMode runs it twice in development).
const session = claimSession(takeBootstrapToken());

type State =
  | { kind: "starting" }
  | { kind: "ready"; session: string; status: NodeStatus }
  | { kind: "stopped" }
  | { kind: "error"; message: string };

function describe(status: NodeStatus): string {
  return status.online
    ? `Connected to Bitcoin Core (${status.chain}).`
    : `Offline mode: ${status.reasons.join("; ")}`;
}

function App() {
  const [state, setState] = useState<State>({ kind: "starting" });
  const [quitting, setQuitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const token = await session;
        const status = await getStatus(token);
        if (!cancelled) setState({ kind: "ready", session: token, status });
      } catch (error) {
        if (!cancelled) setState({ kind: "error", message: (error as Error).message });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  async function onQuit(token: string) {
    setQuitting(true);
    try {
      await quit(token);
      setState({ kind: "stopped" });
    } catch (error) {
      // A refusal, or no answer at all: the app may still be running, so never say it stopped.
      const message = error instanceof SessionError ? error.message : "The app did not answer. Close it from the terminal.";
      setState({ kind: "error", message });
    }
  }

  return (
    <main>
      <h1>Coin Accounting</h1>
      {state.kind === "starting" && <p id="status">Starting…</p>}
      {state.kind === "error" && <p id="status">{state.message}</p>}
      {state.kind === "stopped" && <p id="status">Coin Accounting has stopped. You can close this tab.</p>}
      {state.kind === "ready" && (
        <>
          <p id="status">{describe(state.status)}</p>
          <button id="quit" type="button" disabled={quitting} onClick={() => onQuit(state.session)}>
            Quit
          </button>
        </>
      )}
    </main>
  );
}

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
