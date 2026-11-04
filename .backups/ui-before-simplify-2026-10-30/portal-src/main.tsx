import { SearchX } from "lucide-react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, Route, Routes, useParams } from "react-router-dom";
import "./styles/index.css";
import { Layout, useMe, useProject } from "@/app/Layout";
import { BotHome } from "@/bot/BotHome";
import { EmptyState } from "@/components/ui/primitives";
import { TooltipProvider } from "@/components/ui/menu";
import { AuthProvider, useAuth } from "@/lib/auth";
import { Alerts } from "@/pages/Alerts";
import { Home } from "@/pages/Home";
import { MyCharts } from "@/components/charts/MyCharts";
import { Databases } from "@/pages/Databases";
import { Lineage } from "@/pages/Lineage";
import { AiUsage } from "@/pages/AiUsage";
import { PipelineReport } from "@/pages/PipelineReport";
import { Compute } from "@/pages/Compute";
import { Warehouse } from "@/pages/Warehouse";
import { AllPipelines } from "@/pages/AllPipelines";
import { AskBot } from "@/pages/AskBot";
import { IncidentDetail } from "@/pages/IncidentDetail";
import { Incidents } from "@/pages/Incidents";
import { ManagementOverview } from "@/pages/ManagementOverview";
import { MorningReport } from "@/pages/MorningReport";
import { Overview } from "@/pages/Overview";
import { Runs } from "@/pages/Runs";
import { SignIn } from "@/pages/SignIn";
import { Tables } from "@/pages/Tables";

function Dashboard() {
  const { me } = useMe();
  if (!me.projects.length) return <Navigate to="/" replace />;
  return <Navigate to={`/p/${me.projects[0]!.key}`} replace />;
}

/** Data Factory projects get the management overview; the others keep the site-by-site one. */
function ProjectOverview() {
  const { key } = useParams();
  const project = useProject(key);
  return (
    <>
      {project?.kind === "adf" ? <ManagementOverview /> : <Overview />}
      {key && <MyCharts projectKey={key} />}
    </>
  );
}

/** Projects the user doesn't belong to look exactly like projects that don't exist. */
function ProjectGate({ children }: { children: React.ReactNode }) {
  const { key } = useParams();
  const { me } = useMe();
  if (!me.projects.some((p) => p.key === key)) {
    if (!me.projects.length) return <Navigate to="/" replace />;
    return (
      <div className="mx-auto mt-16 max-w-lg rounded-[1.25rem] border border-line bg-surface shadow-card">
        <EmptyState icon={SearchX}>Project not found. It doesn't exist, or your Azure access doesn't include it.{" "}
          <Link to="/dashboard" className="font-semibold text-accent hover:underline">Go to your projects</Link></EmptyState>
      </div>
    );
  }
  return <>{children}</>;
}

function App() {
  const { state } = useAuth();
  if (state.status === "loading") return null;
  if (state.status !== "signedIn") return <SignIn />;
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/bot" element={<BotHome />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/pipelines" element={<AllPipelines />} />
        <Route path="/pipeline-report" element={<PipelineReport />} />
        <Route path="/lineage" element={<Lineage />} />
        <Route path="/databases" element={<Databases />} />
        <Route path="/ai-usage" element={<AiUsage />} />
        <Route path="/warehouse" element={<Warehouse />} />
        <Route path="/compute" element={<Compute />} />
        <Route path="/p/:key" element={<ProjectGate><ProjectOverview /></ProjectGate>} />
        <Route path="/p/:key/incidents" element={<ProjectGate><Incidents /></ProjectGate>} />
        <Route path="/p/:key/i/:id" element={<ProjectGate><IncidentDetail /></ProjectGate>} />
        <Route path="/p/:key/runs" element={<ProjectGate><Runs /></ProjectGate>} />
        <Route path="/p/:key/tables" element={<ProjectGate><Tables /></ProjectGate>} />
        <Route path="/p/:key/alerts" element={<ProjectGate><Alerts /></ProjectGate>} />
        <Route path="/p/:key/report" element={<ProjectGate><MorningReport /></ProjectGate>} />
        <Route path="/p/:key/ask" element={<ProjectGate><AskBot /></ProjectGate>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <TooltipProvider delayDuration={250} skipDelayDuration={150}>
          <App />
        </TooltipProvider>
      </AuthProvider>
    </BrowserRouter>
  </StrictMode>,
);
