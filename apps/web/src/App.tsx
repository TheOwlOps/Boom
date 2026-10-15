import React, { useState } from "react";
import { DAGViewer, TaskNodeData } from "./components/DAGViewer";
import { ChannelMonitor, ChannelMessage } from "./components/ChannelMonitor";

export default function App() {
  const [tasks] = useState<TaskNodeData[]>([
    { id: "plan", name: "Decompose User Goal", status: "completed", dependencies: [] },
    { id: "worker_a", name: "AST Code Inspection", status: "completed", dependencies: ["plan"] },
    { id: "worker_b", name: "Run Sandbox Tests", status: "running", dependencies: ["plan"] },
    { id: "review", name: "Security & Tirith Gate", status: "idle", dependencies: ["worker_a", "worker_b"] },
  ]);

  const [messages] = useState<ChannelMessage[]>([
    { id: "1", channel: "telegram", sender: "Ryan", text: "Refactor auth middleware to async", timestamp: "12:00:01" },
    { id: "2", channel: "zalo", sender: "Bridge", text: "Incoming event routed to Core", timestamp: "12:00:02" },
    { id: "3", channel: "discord", sender: "Bot", text: "DAG Execution dispatched (4 workers)", timestamp: "12:00:03" },
  ]);

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      <header className="flex justify-between items-center border-b border-slate-800 pb-4">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2">
            Harness Multi-Channel Dashboard
          </h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Rust Core Engine + Python MASS Brain + ReAct & DAG
          </p>
        </div>
        <div className="flex gap-2">
          <span className="px-2.5 py-1 rounded bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 text-xs font-mono">
            IPC: 127.0.0.1:51234
          </span>
          <span className="px-2.5 py-1 rounded bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs font-mono">
            ACP: Ready
          </span>
        </div>
      </header>

      <DAGViewer tasks={tasks} />
      <ChannelMonitor messages={messages} />
    </div>
  );
}
