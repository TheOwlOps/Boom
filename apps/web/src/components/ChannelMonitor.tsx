import React from "react";

export interface ChannelMessage {
  id: string;
  channel: string;
  sender: string;
  text: string;
  timestamp: string;
}

export const ChannelMonitor: React.FC<{ messages: ChannelMessage[] }> = ({ messages }) => {
  return (
    <div className="p-4 rounded-xl border border-slate-800 bg-slate-900/60 shadow-lg">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold tracking-wide text-slate-400 uppercase">
          Multi-Channel Stream
        </h3>
        <span className="flex items-center gap-1.5 text-xs text-emerald-400 font-mono">
          <span className="w-2 h-2 rounded-full bg-emerald-500 animate-ping"></span>
          LIVE
        </span>
      </div>
      <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
        {messages.map((m) => (
          <div
            key={m.id}
            className="p-2.5 rounded-lg border border-slate-800/80 bg-slate-950/40 text-xs font-mono"
          >
            <div className="flex justify-between items-center text-slate-500 mb-1">
              <span className="text-indigo-400 font-semibold">[{m.channel}] {m.sender}</span>
              <span>{m.timestamp}</span>
            </div>
            <p className="text-slate-200">{m.text}</p>
          </div>
        ))}
      </div>
    </div>
  );
};
