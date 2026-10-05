"""A minimal page served until the real SPA exists (M0.3; the frontend build comes with its
dependencies). It claims the session (architecture §4), shows the node status and offers Quit.

It lives in code, not files, because `api/` has no filesystem access (architecture §2). It obeys the
CSP: no inline script or style, and text goes in with `textContent` only (T-104).
"""

from __future__ import annotations

from typing import Final

INDEX_HTML: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<title>Coin Accounting</title>
<script src="/app.js" defer></script>
</head><body>
<h1>Coin Accounting</h1>
<p id="status">Starting…</p>
<button id="quit" type="button" hidden>Quit</button>
</body></html>
"""

APP_JS: Final = """"use strict";
const statusLine = document.getElementById("status");
const quitButton = document.getElementById("quit");

function show(text) {
  statusLine.textContent = text;
}

async function claimSession() {
  const match = /^#bootstrap=([A-Za-z0-9_-]{1,200})$/.exec(location.hash);
  if (match) {
    // T-110: drop the token from the address bar and history before anything else.
    history.replaceState(null, "", location.pathname);
    const response = await fetch("/api/session", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({bootstrap: match[1]}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(data.error || "The session could not be started. Restart the app.");
    }
    sessionStorage.setItem("session", data.session);
  }
  const session = sessionStorage.getItem("session");
  if (!session) {
    throw new Error("No session. Restart the app.");
  }
  return session;
}

async function main() {
  try {
    const session = await claimSession();
    const auth = {Authorization: "Bearer " + session};
    const response = await fetch("/api/status", {headers: auth});
    if (!response.ok) {
      throw new Error("The session was not accepted. Restart the app.");
    }
    const status = await response.json();
    show(status.online
      ? "Connected to Bitcoin Core (" + status.chain + ")."
      : "Offline mode: " + status.reasons.join("; "));
    quitButton.hidden = false;
    quitButton.addEventListener("click", async () => {
      quitButton.disabled = true;
      await fetch("/api/quit", {
        method: "POST",
        headers: {...auth, "Content-Type": "application/json"},
        body: "{}",
      }).catch(() => undefined);
      show("Coin Accounting has stopped. You can close this tab.");
      quitButton.hidden = true;
    });
  } catch (error) {
    show(error.message);
  }
}

main();
"""
