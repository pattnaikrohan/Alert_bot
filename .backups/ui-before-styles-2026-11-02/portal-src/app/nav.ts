// Every page of the app, in one place: the sidebar, the search palette and the top bar all read this.
// Each page has its own colour (CSS class nav-<id>), used for its icon everywhere.
import {
  BellRing, Bot, BrainCircuit, Coins, Database, HeartPulse, House, LayoutDashboard, ListChecks, Network, PiggyBank, Siren, Snowflake, Sunrise, Table2, Wallet, Workflow, type LucideIcon,
} from "lucide-react";

export interface NavItem {
  id: string;
  label: string;
  /** what the page is for, in one line (sidebar tooltip, top bar, palette) */
  hint: string;
  icon: LucideIcon;
  path: (project: string) => string;
  match: (pathname: string) => boolean;
  /** extra words the palette should match, e.g. "rows" finds Tables */
  words?: string;
}

/** Pages that span every project. */
export const GLOBAL_NAV: NavItem[] = [
  { id: "home", label: "Home", hint: "What needs attention, and where everything is", icon: House, path: () => "/", match: (p) => p === "/", words: "start dashboard landing" },
  { id: "pipelines", label: "All pipelines", hint: "Every pipeline and Logic App you can see, run or not", icon: Workflow, path: () => "/pipelines", match: (p) => p === "/pipelines", words: "logic apps synapse data factory adf workflows" },
  { id: "pipeline-report", label: "Pipeline report", hint: "Runs, data volume and errors per day, today against the usual, and what to look at first", icon: HeartPulse, path: () => "/pipeline-report", match: (p) => p === "/pipeline-report", words: "health report execution runs per day volume rows errors failures summary daily dashboard insights pace overdue success rate" },
  { id: "lineage", label: "Lineage", hint: "Where data comes from and goes, across platforms, and what a failure affects", icon: Network, path: () => "/lineage", match: (p) => p === "/lineage", words: "lineage impact upstream downstream dependencies flow source target" },
  { id: "databases", label: "Databases", hint: "Databases you connected, and which pipeline loads which table", icon: Database, path: () => "/databases", match: (p) => p === "/databases", words: "sql server snowflake azure sql connect connection data quality mapping warehouse" },
  { id: "warehouse", label: "Storage cost", hint: "Tables nobody has used for months, what they cost to keep, and the script to remove them", icon: PiggyBank, path: () => "/warehouse", match: (p) => p === "/warehouse", words: "storage cost warehouse cost unused stale old tables drop delete cleanup savings save money gb snowflake sql server archive" },
  { id: "compute", label: "Compute cost", hint: "What Snowflake compute costs, who and what spends it, idle waste, savings and alerts", icon: Snowflake, path: () => "/compute", match: (p) => p === "/compute", words: "snowflake compute credits warehouse spend bill budget cost idle auto suspend resource monitor query cost" },
  { id: "ai", label: "Native AI usage", hint: "AlertBot's own AI: every call it made, its tokens and dollars, and what the same work would cost elsewhere", icon: Coins, path: () => "/ai-usage", match: (p) => p === "/ai-usage", words: "tokens cost dollars price spend llm openai anthropic claude gpt gemini billing usage" },
  { id: "cortex", label: "Cortex AI usage", hint: "What Snowflake Cortex AI costs: agents, Cortex Code, AI functions and more, by user and model", icon: BrainCircuit,
    path: () => "/cortex", match: (p) => p === "/cortex", words: "cortex ai snowflake ai agents cortex code analyst search ai_complete tokens credits llm intelligence" },
  { id: "incidents", label: "Incidents", hint: "What's broken, why, and what to do about it, across all your projects", icon: Siren, path: () => "/incidents",
    match: (p) => p === "/incidents" || /^\/p\/[^/]+\/(incidents|i\/)/.test(p), words: "problems failures errors alerts broken register" },
  { id: "runs", label: "Runs", hint: "Every run, step by step, across all your projects", icon: ListChecks, path: () => "/runs",
    match: (p) => p === "/runs" || /^\/p\/[^/]+\/runs$/.test(p), words: "executions history activity steps" },
  { id: "bot", label: "Ask the bot", hint: "Opens the assistant over this page: ask anything, or tell it what to do", icon: Bot, path: () => "/bot", match: () => false,
    words: "robot assistant voice chat ask question" },
];

/** Pages of the project in view. */
export const PROJECT_NAV: NavItem[] = [
  { id: "overview", label: "Overview", hint: "Health score, runs, tables and trends for this project", icon: LayoutDashboard, path: (k) => `/p/${k}`, match: (p) => /^\/p\/[^/]+\/?$/.test(p), words: "management health score kpi" },
  { id: "tables", label: "Tables", hint: "Rows inserted per table, and data quality", icon: Table2, path: (k) => `/p/${k}/tables`, match: (p) => /^\/p\/[^/]+\/tables$/.test(p), words: "rows inserted loads snowflake data quality nulls duplicates freshness" },
  { id: "report", label: "Morning report", hint: "The daily summary the team gets in Teams", icon: Sunrise, path: (k) => `/p/${k}/report`, match: (p) => /^\/p\/[^/]+\/report$/.test(p), words: "daily summary teams email" },
  { id: "alerts", label: "Alerts", hint: "What was sent to Teams, and muted alerts", icon: BellRing, path: (k) => `/p/${k}/alerts`, match: (p) => /^\/p\/[^/]+\/alerts$/.test(p), words: "notifications teams mute power automate" },
];

/** Pages that belong together share one dock icon, which branches out to show them. A group with only one page
 *  available (e.g. no project chosen yet) shows that page on its own. Order of `members` is the order shown. */
export interface NavGroup {
  id: string;
  label: string;
  hint: string;
  icon: LucideIcon;
  members: string[];
}

export const NAV_GROUPS: NavGroup[] = [
  { id: "g-pipelines", label: "Pipelines", hint: "All pipelines, the pipeline report and lineage", icon: Workflow, members: ["pipelines", "pipeline-report", "lineage"] },
  { id: "g-cost", label: "Cost", hint: "Snowflake compute and storage, AlertBot's own AI, and Snowflake Cortex AI", icon: Wallet, members: ["compute", "warehouse", "ai", "cortex"] },
  { id: "g-activity", label: "Runs & tables", hint: "Every run step by step, and rows loaded per table", icon: ListChecks, members: ["runs", "tables"] },
  { id: "g-teams", label: "Reports & alerts", hint: "The morning report and the alerts sent to Teams", icon: BellRing, members: ["report", "alerts"] },
];

export const ALL_NAV = [...GLOBAL_NAV, ...PROJECT_NAV];
export const pageOf = (pathname: string) => ALL_NAV.find((n) => n.match(pathname));
export const isProjectPage = (pathname: string) => pathname.startsWith("/p/");
