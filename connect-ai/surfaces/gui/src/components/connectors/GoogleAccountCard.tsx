import { useCallback, useEffect, useRef, useState } from "react";
import {
  GOOGLE_CONNECTORS,
  getGoogleState,
  googleActivate,
  googleConnectAll,
  googleSignOut,
  openGoogleWizard,
  startGoogleLogin,
  type Connector,
  type GoogleAccount,
  type GoogleState,
} from "../../api";
import { ConnectorIcon } from "../../connectors/ConnectorIcon";
import { CHIP_OFF, CHIP_OK, FOOT, GRP, GRP_H, PILL_ACCENT, PILL_QUIET, ROW } from "./ui";

// Google is ONE account, not three connectors (connect-AI): a single local
// sign-in drives Gmail, Google Calendar and Google Drive together, so the
// Connectors list opens with this card instead of asking for the same Google
// login three times. Sign in → all three connect. Sign out → the grant is
// revoked at Google and all three disconnect. The per-service detail pages stay
// where they are for tools and privacy filters; credentials only live here.

const SERVICE_ORDER = GOOGLE_CONNECTORS; // gmail, google_calendar, google_drive

// Last-resort labels: the card names all three services even before the sidecar
// has anything to say about one of them.
const SERVICE_TITLES: Record<string, string> = {
  gmail: "Gmail",
  google_calendar: "Calendar",
  google_drive: "Drive",
};

export function GoogleAccountCard({
  connectors,
  onChanged,
}: {
  connectors: Connector[];
  onChanged: () => void;
}) {
  const [state, setState] = useState<GoogleState | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // action label while running
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState(false); // consent tab open in the browser

  // The parent re-renders on its own 5s connector poll, so keep `onChanged` in a
  // ref: the sign-in poll below must not be torn down and restarted each time.
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;

  // A sign-in is done when the account COUNT grows — "any account exists" would
  // finish instantly while adding a second one. Give up after 3 minutes so an
  // abandoned consent tab doesn't leave the buttons stuck.
  const waitFrom = useRef({ count: 0, since: 0 });

  const load = useCallback(async () => setState(await getGoogleState()), []);

  // Poll the helper: slowly to stay honest when a sign-in happens on another
  // surface (a service page's Add account), fast while our own consent tab is
  // open — that sign-in lands out-of-band, so we watch for the account and then
  // refresh the connector list behind us.
  useEffect(() => {
    void load();
    const t = setInterval(async () => {
      const next = await getGoogleState();
      setState(next);
      if (!waiting) return;
      if (next.accounts.length > waitFrom.current.count) {
        setWaiting(false);
        onChangedRef.current();
      } else if (Date.now() - waitFrom.current.since > 180_000) {
        setWaiting(false);
      }
    }, waiting ? 2000 : 10000);
    return () => clearInterval(t);
  }, [waiting, load]);

  // The helper (launcher, port 8766) owns the Google tokens. Without it there is
  // no sign-in to show — stay quiet rather than render a card that can't work.
  if (!state || (!state.reachable && !state.accounts.length)) return null;

  const accounts = state.accounts;
  const services = SERVICE_ORDER.map((name) => {
    const c = connectors.find((x) => x.name === name);
    return {
      name,
      title: SERVICE_TITLES[name] || state.services[name]?.title || c?.title || name,
      brand_color: c?.brand_color,
      connected: state.services[name]?.connected ?? !!c?.connected,
      accounts: state.services[name]?.accounts ?? [],
    };
  });
  const live = services.filter((s) => s.connected).length;
  const incomplete = accounts.length > 0 && live < services.length;

  const run = async (label: string, fn: () => Promise<{ ok: boolean; error?: string }>) => {
    setBusy(label);
    setError(null);
    const res = await fn();
    setBusy(null);
    if (!res.ok) setError(res.error || "that didn't work");
    await load();
    onChangedRef.current();
  };

  const signIn = () => {
    setError(null);
    if (!state.has_client) {
      // Nothing to consent to yet — startGoogleLogin would just open the setup
      // page. Send them there without arming a poll that can never finish.
      openGoogleWizard();
      return;
    }
    waitFrom.current = { count: accounts.length, since: Date.now() };
    setWaiting(true);
    void startGoogleLogin();
  };

  return (
    <div data-testid="google-account-card">
      <div className={GRP_H + " !mt-0"}>Google</div>
      <div className={GRP}>
        {accounts.length === 0 ? (
          <div className={ROW}>
            <span className="min-w-0 flex-1">
              <span className="font-medium text-[13.5px]">
                {state.has_client ? "Sign in with Google" : "Set up Google sign-in"}
              </span>
              <span className="block text-[12px] text-muted">
                {state.has_client
                  ? "One sign-in connects Gmail, Calendar and Drive together."
                  : /* No OAuth client saved yet: the button opens the local setup
                       page, not a Google consent tab — say so before it happens. */
                    "One-time setup first: save your own Google OAuth client (~3 min), then sign-in is one click."}
              </span>
            </span>
            <button
              className={PILL_ACCENT}
              data-testid="google-card-signin"
              onClick={signIn}
              disabled={waiting}
            >
              {!state.has_client
                ? "Set up Google"
                : waiting
                  ? "Finish in your browser…"
                  : "Sign in with Google"}
            </button>
          </div>
        ) : (
          accounts.map((a) => (
            <AccountRow
              key={a.name}
              a={a}
              services={services}
              busy={busy}
              onActivate={() => run("activate", () => googleActivate(a.name))}
              onSignOut={() => {
                if (
                  !window.confirm(
                    `Sign ${a.email || a.name} out of Gmail, Calendar and Drive? ` +
                      "Access is revoked at Google too.",
                  )
                )
                  return;
                void run("signout", () => googleSignOut(a.name));
              }}
            />
          ))
        )}

        {accounts.length > 0 && (
          <div className={ROW}>
            <span className="min-w-0 flex-1 text-[12.5px] text-muted">
              {incomplete
                ? `${live} of ${services.length} Google services connected`
                : "Gmail, Calendar and Drive all run on this sign-in."}
            </span>
            {incomplete && (
              <button
                className={PILL_QUIET}
                data-testid="google-card-reconnect"
                onClick={() => void run("reconnect", googleConnectAll)}
                disabled={busy !== null}
              >
                {busy === "reconnect" ? "Reconnecting…" : "Reconnect all"}
              </button>
            )}
            <button
              className={PILL_QUIET}
              data-testid="google-card-add"
              onClick={signIn}
              disabled={waiting}
            >
              {waiting ? "Check your browser…" : "＋ Add account"}
            </button>
          </div>
        )}
      </div>

      {error && <div className="text-[12.5px] text-danger px-4 pt-1.5">{error}</div>}
      <div className={FOOT}>
        Tokens stay on this computer and refresh themselves — no cloud account.{" "}
        <button className="text-muted hover:text-ink underline" onClick={openGoogleWizard}>
          Google setup
        </button>
      </div>
    </div>
  );
}

function AccountRow({
  a,
  services,
  busy,
  onActivate,
  onSignOut,
}: {
  a: GoogleAccount;
  services: {
    name: string;
    title: string;
    brand_color?: string;
    connected: boolean;
    accounts: string[];
  }[];
  busy: string | null;
  onActivate: () => void;
  onSignOut: () => void;
}) {
  const email = (a.email || a.name).toLowerCase();
  return (
    <div className={ROW + " !items-start !py-3"} data-testid={`google-account-${email}`}>
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-2">
          <span className="text-[13.5px] font-medium truncate">{a.email || a.name}</span>
          {a.is_active && <span className={CHIP_OK}>Default</span>}
          {!a.has_refresh_token && <span className={CHIP_OFF}>Sign in again</span>}
        </span>
        {/* One row of service chips: what this single login is actually driving. */}
        <span className="flex flex-wrap items-center gap-1.5 mt-1.5">
          {services.map((s) => {
            const on = s.connected && (!s.accounts.length || s.accounts.includes(email));
            return (
              <span
                key={s.name}
                className={
                  "inline-flex items-center gap-1 text-[11.5px] px-2 py-0.5 rounded-full border " +
                  (on
                    ? "border-okLine bg-okSoft text-ok"
                    : "border-line bg-paper text-faint")
                }
                data-testid={`google-service-${s.name}`}
                title={on ? `${s.title} is connected` : `${s.title} is not connected`}
              >
                <ConnectorIcon
                  connector={{
                    logo: s.name,
                    title: s.title,
                    // Disconnected services read as gray, matching the chip.
                    brand_color: on ? s.brand_color : "#6b7280",
                  }}
                  size={12}
                />
                {s.title}
              </span>
            );
          })}
        </span>
      </span>
      {!a.is_active && (
        <button
          className="text-[12px] text-muted hover:text-ink shrink-0"
          data-testid={`google-make-default-${email}`}
          onClick={onActivate}
          disabled={busy !== null}
          title="Use this account by default across Gmail, Calendar and Drive"
        >
          Make default
        </button>
      )}
      <button
        className="text-[12px] text-muted hover:text-danger shrink-0"
        data-testid={`google-signout-${email}`}
        onClick={onSignOut}
        disabled={busy !== null}
      >
        {busy === "signout" ? "Signing out…" : "Sign out"}
      </button>
    </div>
  );
}
