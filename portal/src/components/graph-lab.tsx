// Dev-only graph lab (graph-lab.html): the run view with a saved run, without signing in.
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "../styles/index.css";
import { BrowserRouter } from "react-router-dom";
import { PipelineGraph, type Step } from "./PipelineGraph";
import { RunTables, TableLoads } from "./TableLoads";
import { ManagementOverview } from "../pages/ManagementOverview";

const q = new URLSearchParams(location.search);
document.documentElement.dataset.theme = q.get("theme") ?? "dark";

function Lab() {
  const [run, setRun] = useState<{ steps: Step[]; tables?: any[] } | null>(null);
  const [loads, setLoads] = useState<any>(null);
  useEffect(() => { void fetch("/.lab/run.json").then((r) => r.json()).then(setRun); void fetch("/.lab/loads.json").then((r) => r.json()).then(setLoads); }, []);
  const [mgmt, setMgmt] = useState<any>(null);
  useEffect(() => { void fetch("/.lab/mgmt.json").then((r) => r.json()).then(setMgmt); }, []);
  if (q.get("view") === "mgmt") return <BrowserRouter><div style={{ padding: 24, background: "var(--canvas)", minHeight: "100vh", maxWidth: 1260 }}>{mgmt && <ManagementOverview sample={mgmt} />}</div></BrowserRouter>;
  if (q.get("view") === "tables") return <BrowserRouter><div style={{ padding: 24, background: "var(--canvas)", minHeight: "100vh" }}>{loads && <TableLoads projectKey="csdw-adf" sample={loads} />}</div></BrowserRouter>;
  return (
    <div style={{ padding: 24, background: "var(--canvas)", minHeight: "100vh" }}>
      <div className="rounded-2xl bg-surface p-5 shadow-[inset_0_0_0_1px_var(--line)]" style={{ maxWidth: 1180 }}>
        <h2 className="mb-3 text-lg font-semibold text-ink">This run, step by step</h2>
        {run ? <><PipelineGraph steps={run.steps} /><RunTables tables={(run.tables ?? []).filter((t: any) => t.kind !== "files")} /></> : "loading…"}
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<StrictMode><Lab /></StrictMode>);
