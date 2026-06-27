import { useEffect, useRef, useState } from "react";
import { Bug, X, Loader2, Copy, Check } from "lucide-react";
import { buildApiUrl } from "@/config/api";
import { copyText } from "@/lib/clipboard";

interface LogLine {
  id: number;
  ts: number;
  level: string;
  logger: string;
  message: string;
}

const levelColor = (level: string) => {
  switch (level) {
    case "ERROR":
    case "CRITICAL":
      return "text-red-400";
    case "WARNING":
      return "text-amber-400";
    case "DEBUG":
      return "text-muted-foreground";
    default:
      return "text-emerald-300";
  }
};

/**
 * In-app backend debug panel. A small toggle in the header opens a centered
 * floating box that live-tails recent backend log lines (GET /settings/debug-logs).
 */
const DebugPanel = () => {
  const [open, setOpen] = useState(false);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // "Stuck to bottom" when within a small threshold of the end.
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  };

  const handleCopyAll = async () => {
    const text = logs
      .map((l) => `${l.level}\t${new Date(l.ts * 1000).toLocaleTimeString()}\t${l.message}`)
      .join("\n");
    if (await copyText(text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  useEffect(() => {
    if (!open) return;
    let active = true;
    const fetchLogs = async () => {
      try {
        setLoading(true);
        const res = await fetch(buildApiUrl("/settings/debug-logs?limit=300"), { credentials: "include" });
        if (!res.ok) return;
        const data = await res.json();
        if (active && Array.isArray(data.logs)) setLogs(data.logs);
      } catch {
        /* ignore transient errors */
      } finally {
        if (active) setLoading(false);
      }
    };
    fetchLogs();
    const timer = window.setInterval(fetchLogs, 2000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [open]);

  // On open, start pinned to the bottom.
  useEffect(() => {
    if (open) atBottomRef.current = true;
  }, [open]);

  // Auto-tail ONLY while the user is at the bottom. If they scrolled up to read,
  // leave their position alone (no more jumping down); auto-tail resumes once
  // they scroll back to the bottom.
  useEffect(() => {
    if (open && scrollRef.current && atBottomRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [logs, open]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-sm transition-colors ${
          open
            ? "bg-primary/15 border-primary/40 text-foreground"
            : "bg-card border-border text-muted-foreground hover:text-foreground hover:bg-muted"
        }`}
        title="Show backend debug log"
        data-testid="debug-panel-toggle"
      >
        <Bug className="w-4 h-4" />
        Debug
      </button>

      {open && (
        <div className="absolute left-1/2 -translate-x-1/2 top-full mt-2 z-50 w-[min(760px,92vw)] rounded-lg border border-border bg-card shadow-2xl">
          <div className="flex items-center justify-between px-3 py-2 border-b border-border">
            <div className="flex items-center gap-2 text-sm font-medium text-foreground">
              <Bug className="w-4 h-4 text-primary" />
              Backend Debug
              {loading && <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />}
            </div>
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={handleCopyAll}
                disabled={logs.length === 0}
                className="flex items-center gap-1 text-muted-foreground hover:text-foreground disabled:opacity-40"
                title="Copy all messages"
                data-testid="debug-copy-all"
              >
                {copied ? (
                  <Check className="w-4 h-4 text-emerald-400" />
                ) : (
                  <Copy className="w-4 h-4" />
                )}
              </button>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="text-muted-foreground hover:text-foreground"
                aria-label="Close debug panel"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>
          <div ref={scrollRef} onScroll={handleScroll} className="max-h-[340px] overflow-auto p-2 font-mono text-[11px] leading-relaxed">
            {logs.length === 0 ? (
              <div className="text-muted-foreground px-1 py-2">No recent log lines.</div>
            ) : (
              logs.map((line) => (
                <div key={line.id} className="whitespace-pre-wrap break-words">
                  <span className={levelColor(line.level)}>{line.level}</span>{" "}
                  <span className="text-muted-foreground">
                    {new Date(line.ts * 1000).toLocaleTimeString()}
                  </span>{" "}
                  <span className="text-foreground/90">{line.message}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default DebugPanel;
