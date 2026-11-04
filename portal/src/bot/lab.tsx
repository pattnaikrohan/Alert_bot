// Dev-only robot lab (robot-lab.html): the robot in each requested mood on the real stage background.
import { StrictMode, useState } from "react";
import { createRoot } from "react-dom/client";
import "../styles/index.css";
import "./bot.css";
import { RobotSVG, type Mood } from "./RobotSVG";

const q = new URLSearchParams(location.search);
document.documentElement.dataset.theme = q.get("theme") ?? "dark";
const shade = q.get("shade");
if (shade && shade !== "ocean" && shade !== "daylight") document.documentElement.dataset.shade = shade;
const moods = (q.get("mood") ?? "ok,warning,critical").split(",") as Mood[];
const speaking = q.get("speaking") === "1";

function Cell({ mood }: { mood: Mood }) {
  const [poke, setPoke] = useState(0);
  return (
    <main className={`bot-stage mood-${mood}`} style={{ minHeight: 0, height: "100vh", flex: 1 }}>
      <div className="bot-figure" style={{ left: "50%", height: "86%" }}>
        <div className="bot-aura" aria-hidden="true" />
        <RobotSVG mood={mood} speaking={speaking} pokeSignal={poke} onPoke={() => setPoke((p) => p + 1)} />
      </div>
      <div style={{ position: "absolute", left: 16, top: 12, font: "600 12px Inter", color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: "0.08em" }}>{mood}</div>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <div style={{ display: "flex", height: "100vh", background: "var(--canvas)" }}>
      {moods.map((m) => <Cell key={m} mood={m} />)}
    </div>
  </StrictMode>,
);
