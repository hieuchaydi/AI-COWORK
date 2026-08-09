import { useEffect, useState } from "react";
import {
  connectConnector,
  connectMcpBacked,
  getConnectors,
  GOOGLE_CONNECTORS,
  startGoogleLogin,
  type Connector,
} from "../../api";
import { ConnectorBadge } from "../../connectors/ConnectorIcon";
import { ConnectSetup } from "../ManageTabs";
import { PILL_ACCENT, PILL_LINE, TAG_ACCENT } from "./ui";

// The ONE place a connection gets added (UX-DECISIONS §21): the detail page's header
// button (or the list's Connect pill) opens this sheet. Google connectors hand off to
// the local Google wizard (helper on :8766); MCP-backed connectors get a local OAuth
// one-click; everything else renders its manual ConnectSetup (token paste).

const INPUT =
  "w-full px-3 py-2 rounded-lg border border-line bg-paper text-[13px] text-ink outline-none focus:border-accent";

export function AddConnectionModal({
  c,
  title,
  onClose,
  onChanged,
}: {
  c: Connector;
  title?: string; // e.g. "Add a workspace" — defaults to "Connect {title}"
  onClose: () => void;
  onChanged: () => void;
}) {
  // MCP-backed one-click (§42): local OAuth against the vendor's hosted MCP server —
  // with manual fields alongside (jira, asana) it's a second mode; alone (monday)
  // it IS the connect flow.
  const mcpBacked = !!c.mcp;
  const isGoogle = GOOGLE_CONNECTORS.includes(c.name);
  const twoModes = mcpBacked && c.fields.length > 0;
  const [pane, setPane] = useState<"one" | "manual">("one");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-40" data-testid="add-connection-modal">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div
        className="absolute left-1/2 top-[14%] -translate-x-1/2 w-[480px] max-w-[calc(100vw-2rem)] bg-panel rounded-2xl border border-line shadow-2xl"
        role="dialog"
        aria-label={title || `Connect ${c.title}`}
      >
        <div className="flex items-center gap-3 px-5 pt-5">
          <ConnectorBadge connector={c} size={34} title={c.title} />
          <div className="flex-1 font-semibold text-[16px] tracking-tight">
            {title || `Connect ${c.title}`}
          </div>
          <button className="text-faint hover:text-ink text-[18px] leading-none" onClick={onClose} title="Close">
            ×
          </button>
        </div>

        {isGoogle ? (
          <GoogleConnect c={c} onConnected={() => { onChanged(); onClose(); }} />
        ) : twoModes ? (
          <>
            <div className="px-5 pt-4">
              <div className="inline-flex rounded-full p-0.5 bg-paper text-[12.5px] font-medium">
                {(["one", "manual"] as const).map((p) => (
                  <button
                    key={p}
                    data-testid={`modal-pane-${p}`}
                    className={
                      "px-3.5 py-1 rounded-full " +
                      (pane === p ? "bg-panel shadow-sm text-ink border border-line" : "text-muted")
                    }
                    onClick={() => setPane(p)}
                  >
                    {p === "one" ? "One click" : "Manual"}
                  </button>
                ))}
              </div>
            </div>
            {pane === "one" ? (
              <McpOneClick c={c} onConnected={() => { onChanged(); onClose(); }} />
            ) : (
              <div className="px-1.5 pb-2">
                <ConnectSetup c={c} onConnected={() => { onChanged(); onClose(); }} manualOnly />
              </div>
            )}
          </>
        ) : mcpBacked ? (
          /* MCP-backed with no manual fields (monday): one-click IS the flow. */
          <McpOneClick c={c} onConnected={() => { onChanged(); onClose(); }} />
        ) : c.name === "slack" ? (
          <SlackManual onConnected={() => { onChanged(); onClose(); }} />
        ) : (
          <div className="px-1.5 pb-2">
            {/* Manual token/key setup for everything else. */}
            <ConnectSetup c={c} onConnected={() => { onChanged(); onClose(); }} />
          </div>
        )}
      </div>
    </div>
  );
}

// Google connect pane: opens the LOCAL Google wizard (launcher helper on :8766) in a
// new tab — sign in with Google there; ONE sign-in connects Gmail, Calendar and
// Drive at once. Tokens land in
// google-tokens.json on this computer and are pushed into the sidecar. Poll until
// the connector flips to connected, then close.
function GoogleConnect({ c, onConnected }: { c: Connector; onConnected: () => void }) {
  const [waiting, setWaiting] = useState(false);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(async () => {
      try {
        const list = await getConnectors();
        if (list.find((x) => x.name === c.name)?.connected) onConnected();
      } catch {
        /* keep polling */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [waiting, c.name, onConnected]);
  return (
    <div className="px-5 py-4 space-y-3">
      <p className="text-[13px] text-muted">
        Sign in with Google in your browser — the same sign-in connects Gmail, Calendar
        and Drive. Tokens are stored only on this computer.
      </p>
      <button
        className={PILL_ACCENT + " w-full !py-2"}
        data-testid="modal-google-connect"
        onClick={() => {
          void startGoogleLogin();
          setWaiting(true);
        }}
      >
        {waiting ? "Finish in your browser… (this closes itself)" : "Sign in with Google"}
      </button>
      <p className="text-[12px] text-faint text-center flex items-center justify-center gap-1.5">
        <span className={TAG_ACCENT}>Local</span> one Google sign-in covers Gmail, Calendar
        &amp; Drive · no cloud account
      </p>
    </div>
  );
}

// One-click pane for MCP-BACKED connectors (monday, asana, jira — §42): the sidecar
// runs a fully LOCAL OAuth flow against the vendor's hosted MCP server (DCR — no
// client secret, no broker, no cloud sign-in). Poll until the card flips to
// connected, then close.
function McpOneClick({ c, onConnected }: { c: Connector; onConnected: () => void }) {
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!waiting) return;
    const t = setInterval(async () => {
      try {
        const list = await getConnectors();
        if (list.find((x) => x.name === c.name)?.connected) onConnected();
      } catch {
        /* keep polling */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [waiting, c.name, onConnected]);
  const go = async () => {
    setError(null);
    const res = await connectMcpBacked(c.name);
    if (res.ok) setWaiting(true);
    else setError(res.error || "could not start the connect");
  };
  return (
    <div className="px-5 py-4 space-y-3">
      <p className="text-[13px] text-muted">
        Opens {c.title} in your browser — sign in and approve access there. No tokens
        typed: the sign-in runs entirely on this computer.
      </p>
      <button
        className={PILL_ACCENT + " w-full !py-2"}
        data-testid="modal-mcp-one-click"
        onClick={go}
        disabled={waiting}
      >
        {waiting ? "Check your browser…" : `Connect ${c.title}`}
      </button>
      {error && <div className="text-[12.5px] text-danger">{error}</div>}
      <p className="text-[12px] text-faint text-center flex items-center justify-center gap-1.5">
        <span className={TAG_ACCENT}>Recommended</span> agents get a curated set of{" "}
        {c.title} tools · tokens stay on this computer
      </p>
    </div>
  );
}

function SlackManual({ onConnected }: { onConnected: () => void }) {
  const [bot, setBot] = useState("");
  const [app, setApp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setError(null);
    const res = await connectConnector("slack", { bot_token: bot.trim(), app_token: app.trim() });
    setBusy(false);
    if (res.ok) onConnected();
    else setError(res.error || "could not connect");
  };
  return (
    <div className="px-5 py-4 space-y-3">
      <ol className="list-decimal pl-4 text-[13px] text-muted space-y-1">
        <li>Create an app at api.slack.com/apps</li>
        <li>Enable Socket Mode, add bot scopes, install it to your workspace</li>
        <li>Paste both tokens</li>
      </ol>
      <input className={INPUT} type="password" placeholder="Bot token · xoxb-…" value={bot} spellCheck={false} onChange={(e) => setBot(e.target.value)} />
      <input className={INPUT} type="password" placeholder="App token · xapp-…" value={app} spellCheck={false} onChange={(e) => setApp(e.target.value)} />
      <button className={PILL_LINE + " w-full !py-2"} onClick={submit} disabled={busy || !bot.trim() || !app.trim()}>
        {busy ? "Validating…" : "Connect"}
      </button>
      {error && <div className="text-[12.5px] text-danger">{error}</div>}
    </div>
  );
}
