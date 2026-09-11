import { useEffect, useState } from "react";
import { cancelSessionWake, getSessionWakes, setSessionWakeAuto, type SessionWake } from "../api";
import { Icon } from "./Icon";
import { Toggle } from "./Toggle";

export function WakeControls({ sessionId }: { sessionId: string }) {
  const [wakes, setWakes] = useState<SessionWake[]>([]);
  const [auto, setAuto] = useState(true);
  const refresh = () => getSessionWakes(sessionId).then((x) => {
    setWakes(Array.isArray(x.wakes) ? x.wakes : []);
    setAuto(typeof x.auto === "boolean" ? x.auto : true);
  }).catch(() => {});

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [sessionId]);

  if (wakes.length === 0) return null;
  return (
    <div className="mx-4 mb-2 flex items-center gap-2 border border-line bg-panel px-3 py-2 text-[12.5px]" data-testid="wake-controls">
      <Icon name="clock" size={14} className="text-accent shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{wakes.length} wake đang chờ</div>
        <div className="truncate text-faint">
          {wakes[0].note || wakes[0].event_key || wakes[0].job_id || "Tiếp tục công việc"}
          {wakes[0].fire_at ? ` · ${new Date(wakes[0].fire_at).toLocaleString()}` : ""}
        </div>
      </div>
      <span className="text-faint">Auto</span>
      <Toggle checked={auto} onChange={async (next) => {
        setAuto(next);
        await setSessionWakeAuto(sessionId, next);
      }} title="Tự động tiếp tục khi wake đến hạn" />
      <button
        className="text-danger/80 hover:text-danger"
        title="Hủy wake này"
        aria-label="Hủy wake"
        onClick={async () => {
          await cancelSessionWake(sessionId, wakes[0].id);
          refresh();
        }}
      >
        <Icon name="trash" size={14} />
      </button>
    </div>
  );
}
