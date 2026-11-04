// What every page can read about the session: who's signed in, their projects, the project in view, the bot's feed,
// pipeline counts for the navigation badges, and a way to open the command palette.
import { createContext, useContext } from "react";
import type { IncidentBrief, Me, ProjectCard } from "@/lib/types";

export interface Feed {
  active: { id: number; title: string; severity: string; project: string; project_name: string; opened_at: string; likely_cause: string;
    acknowledged_by?: string | null; status: "open" | "acknowledged" }[];
  /** incidents resolved today (opened or resolved since midnight), newest first: Home says what happened and who handled it */
  resolved_today?: (IncidentBrief & { project: string; project_name: string })[];
  recent: { at: string; kind: string; incident_id: number; title: string; severity: string; project: string; project_name: string; by?: string | null }[];
  bot: { last_cycle_at: string | null; stale: boolean; error?: string | null };
  /** the run watcher: when it last looked at Azure, and what it saw change */
  live?: { last_look_at: string | null; watching: boolean; events: RunEvent[] };
}

export interface RunEvent { run_id: string; pipeline: string; status: string; before: string | null; at: string | null; seen_at: string; project: string; project_name: string }

/** failing: broken and nobody on it · handled: someone acknowledged it, or resolved it and the next run will tell ·
 *  unstable: failed this week, fine since (information, not a problem) */
export interface PipelineCounts { total: number; failing: number; handled?: number; unstable: number; running: number; healthy: number; idle: number; never_run: number; disabled: number }

export interface Handled { kind: "acknowledged" | "resolved"; by: string | null; at: string | null; incident_id: number; title: string; note: string | null }
export interface PipelineBrief { name: string; project: string; project_name: string; state: string; handled?: Handled | null;
  last_run: { at: string; status: string } | null; incident: { id: number; title: string; severity: string; status?: string } | null }

export interface Shell {
  me: Me;
  reload: () => void;
  project: ProjectCard | undefined;
  feed: Feed | null;
  pipelines: PipelineCounts | null;
  pipelineItems: PipelineBrief[];
  openPalette: () => void;
}

export const ShellContext = createContext<Shell | null>(null);

export const useShell = () => {
  const v = useContext(ShellContext);
  if (!v) throw new Error("useShell outside the app shell");
  return v;
};
