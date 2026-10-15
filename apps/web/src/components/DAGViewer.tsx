import React from "react";

export interface TaskNodeData {
  id: string;
  name: string;
  status: "idle" | "running" | "completed" | "failed";
  dependencies: string[];
}

export const DAGViewer: React.FC<{ tasks: TaskNodeData[] }> = ({ tasks }) => {
  return (
    <div className="p-4 rounded-xl border border-slate-800 bg-slate-900/60 shadow-lg">
      <h3 className="text-sm font-semibold tracking-wide text-slate-400 mb-3 uppercase">
        DAG Task Graph
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        {tasks.map((task) => {
          const statusBg =
            task.status === "completed"
              ? "border-emerald-500/50 bg-emerald-950/20 text-emerald-400"
              : task.status === "running"
              ? "border-amber-500/50 bg-amber-950/20 text-amber-400 animate-pulse"
              : "border-slate-800 bg-slate-900 text-slate-400";

          return (
            <div
              key={task.id}
              className={`p-3 rounded-lg border ${statusBg} flex flex-col justify-between`}
            >
              <div>
                <span className="text-xs uppercase font-mono px-1.5 py-0.5 rounded bg-slate-800 text-slate-300">
                  {task.id}
                </span>
                <p className="font-medium text-sm mt-1">{task.name}</p>
              </div>
              <div className="text-[11px] text-slate-500 mt-2 font-mono">
                deps: [{task.dependencies.join(", ") || "none"}]
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
};
