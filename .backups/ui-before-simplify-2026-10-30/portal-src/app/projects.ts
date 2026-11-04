// Small helpers about projects, shared by the dock, the search palette and Home.
import { Factory, Layers, Snowflake, Workflow, Zap, type LucideIcon } from "lucide-react";
import type { ProjectCard } from "@/lib/types";

/** The icon of the Azure service a project watches. */
export function serviceIcon(p: ProjectCard | undefined): LucideIcon {
  if (!p) return Workflow;
  if (p.service === "Synapse") return Layers;
  if (p.service === "Logic Apps") return Zap;
  if (p.service === "Data Factory") return Factory;
  return p.kind === "feeds" ? Snowflake : Workflow;
}

/** A project's name without the service it's already labelled with, e.g. "adfpipelinerohan (Data Factory)" → "adfpipelinerohan". */
export const shortName = (p: ProjectCard) =>
  p.display_name.replace(/\s*\((Data Factory|Synapse|Logic Apps)\)\s*$/, "").replace(/^Logic Apps · /, "") || p.display_name;

export const STATUS_COLOR: Record<string, string> = { critical: "var(--critical)", warning: "var(--warning)", ok: "var(--good)" };
export const STATUS_TEXT: Record<string, string> = { critical: "Needs action", warning: "Needs a look", ok: "Healthy" };
