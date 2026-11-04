// Small looping animations for the tour, one per step, each acting out what that part of the app does.
// SVG with theme colours; motion is transform / opacity / stroke-dashoffset only (composited, smooth at high refresh rates).
import { BotAvatar } from "@/components/BotAvatar";

const W = 360, H = 150;
const Frame = ({ children, label }: { children: React.ReactNode; label: string }) => (
  <svg viewBox={`0 0 ${W} ${H}`} className="tour-svg" role="img" aria-label={label}>{children}</svg>
);

export function Welcome() {
  return (
    <div className="tour-welcome" aria-label="The Alert Bot robot">
      <span className="tw-ring" /><span className="tw-ring tw-ring-2" /><span className="tw-ring tw-ring-3" />
      {[0, 1, 2, 3, 4, 5].map((k) => <i key={k} className="tw-spark" style={{ ["--a" as string]: `${k * 60}deg`, animationDelay: `${k * 0.25}s` }} />)}
      <BotAvatar mood="ok" className="tw-bot" />
    </div>
  );
}

export function Dock() {
  return (
    <Frame label="Icons magnify under the pointer, then two swap places">
      <rect x="40" y="88" width="280" height="44" rx="14" className="ta-glass" />
      {[0, 1, 2, 3, 4].map((k) => (
        <g key={k} className={`td-ico td-ico-${k}`} style={{ transformOrigin: `${70 + k * 55}px 110px` }}>
          <rect x={56 + k * 55} y="94" width="28" height="28" rx="8" className={k === 2 ? "ta-accent" : "ta-tile"} />
        </g>
      ))}
      <circle r="5" className="td-cursor ta-ink" cx="0" cy="0" />
      <text x="180" y="40" textAnchor="middle" className="ta-caption">hover · click · drag to rearrange</text>
    </Frame>
  );
}

export function Home() {
  return (
    <Frame label="Cards arrive, the one that needs attention pulses">
      {[0, 1, 2].map((k) => (
        <g key={k} className="th-card" style={{ animationDelay: `${k * 0.18}s` }}>
          <rect x={44} y={22 + k * 40} width="272" height="32" rx="10" className="ta-tile" />
          <rect x={60} y={33 + k * 40} width={k === 0 ? 120 : 160 - k * 20} height="8" rx="4" className="ta-line" />
          <circle cx="292" cy={38 + k * 40} r="6" className={k === 0 ? "ta-crit th-pulse" : "ta-good"} />
        </g>
      ))}
    </Frame>
  );
}

export function Pipelines() {
  return (
    <Frame label="Pipelines and their status, one turns red then green again">
      <rect x="44" y="18" width="272" height="114" rx="12" className="ta-tile" />
      <rect x="44" y="18" width="272" height="26" rx="12" className="tp-scan" />
      {[0, 1, 2, 3].map((k) => (
        <g key={k}>
          <rect x="62" y={30 + k * 25} width={90 + (k % 2) * 30} height="7" rx="3.5" className="ta-line" />
          <rect x="190" y={30 + k * 25} width="70" height="7" rx="3.5" className="ta-accent-soft" />
          <circle cx="290" cy={33.5 + k * 25} r="5.5" className={k === 1 ? "tp-flip" : "ta-good"} />
        </g>
      ))}
    </Frame>
  );
}

export function Lineage() {
  return (
    <Frame label="Data flows from a file through a pipeline into tables; a failure travels downstream">
      <path d="M86 75 C120 75 128 75 158 75" className="tl-edge" />
      <path d="M202 75 C236 75 236 42 270 42" className="tl-edge" />
      <path d="M202 75 C236 75 236 108 270 108" className="tl-edge" />
      <circle r="5" className="ta-crit tl-pulse"><animateMotion dur="2.4s" repeatCount="indefinite" path="M86 75 C120 75 128 75 158 75 L202 75 C236 75 236 108 270 108" /></circle>
      <rect x="40" y="58" width="46" height="34" rx="9" className="ta-tile" /><text x="63" y="79" textAnchor="middle" className="ta-tiny">file</text>
      <rect x="158" y="56" width="44" height="38" rx="10" className="ta-accent" /><text x="180" y="79" textAnchor="middle" className="ta-tiny ta-on-accent">ADF</text>
      <rect x="270" y="26" width="52" height="32" rx="9" className="ta-tile" /><text x="296" y="46" textAnchor="middle" className="ta-tiny">table</text>
      <rect x="270" y="92" width="52" height="32" rx="9" className="ta-tile tl-hit" /><text x="296" y="112" textAnchor="middle" className="ta-tiny">report</text>
    </Frame>
  );
}

export function Databases() {
  return (
    <Frame label="Rows stay in the database; only a count comes out">
      <g className="tdb-db">
        <ellipse cx="96" cy="44" rx="36" ry="11" className="ta-accent-soft" />
        <path d="M60 44 v62 a36 11 0 0 0 72 0 v-62" className="ta-accent-soft" />
        <ellipse cx="96" cy="106" rx="36" ry="11" className="ta-accent-soft" />
        {[0, 1, 2].map((k) => <rect key={k} x="74" y={56 + k * 14} width="44" height="6" rx="3" className="ta-line tdb-row" style={{ animationDelay: `${k * 0.2}s` }} />)}
      </g>
      <path d="M140 75 H210" className="tl-edge" />
      <g className="tdb-count">
        <rect x="214" y="52" width="110" height="46" rx="12" className="ta-tile" />
        <text x="269" y="74" textAnchor="middle" className="ta-big">12,402</text>
        <text x="269" y="90" textAnchor="middle" className="ta-tiny">rows today</text>
      </g>
      <text x="180" y="140" textAnchor="middle" className="ta-caption">counts and percentages, never rows</text>
    </Frame>
  );
}

export function Score() {
  const r = 46, c = 2 * Math.PI * r;
  return (
    <Frame label="The project's health score fills up">
      <circle cx="110" cy="75" r={r} className="ts-track" />
      <circle cx="110" cy="75" r={r} className="ts-arc" style={{ strokeDasharray: `${c}`, ["--c" as string]: `${c}`, ["--to" as string]: `${c * 0.08}` }} transform="rotate(-90 110 75)" />
      <text x="110" y="83" textAnchor="middle" className="ta-big ts-num">92</text>
      {[0, 1, 2].map((k) => (
        <g key={k} className="th-card" style={{ animationDelay: `${0.3 + k * 0.15}s` }}>
          <rect x="190" y={34 + k * 30} width="130" height="22" rx="7" className="ta-tile" />
          <rect x="200" y={42 + k * 30} width={[86, 60, 74][k]} height="6" rx="3" className={k === 1 ? "ta-warn" : "ta-good"} />
        </g>
      ))}
    </Frame>
  );
}

export function Checks() {
  return (
    <Frame label="Checks tick off one by one; one needs a look">
      {["Row count", "Empty values", "Duplicates", "Freshness", "Schema"].map((label, k) => (
        <g key={label} className="tc-row" style={{ animationDelay: `${k * 0.32}s` }}>
          <circle cx="78" cy={26 + k * 24} r="8" className={k === 2 ? "ta-warn-soft" : "ta-good-soft"} />
          {k === 2 ? <text x="78" y={30 + k * 24} textAnchor="middle" className="ta-tiny tc-warn">!</text>
            : <path d={`M73 ${26 + k * 24} l3.5 3.5 l6 -7`} className="tc-tick" style={{ animationDelay: `${0.15 + k * 0.32}s` }} />}
          <text x="96" y={30 + k * 24} className="ta-label">{label}</text>
          <rect x="220" y={22 + k * 24} width={[80, 54, 96, 66, 72][k]} height="7" rx="3.5" className={k === 2 ? "ta-warn" : "ta-accent-soft"} />
        </g>
      ))}
    </Frame>
  );
}

export function Incident() {
  return (
    <Frame label="A bell rings, then the incident explains itself">
      <g className="ti-bell" style={{ transformOrigin: "84px 46px" }}>
        <path d="M84 40 c-14 0 -20 10 -20 24 v14 l-6 8 h52 l-6 -8 v-14 c0 -14 -6 -24 -20 -24z" className="ta-crit" />
        <circle cx="84" cy="94" r="5" className="ta-crit" />
      </g>
      <rect x="140" y="30" width="184" height="92" rx="12" className="ta-tile" />
      {[0, 1, 2, 3].map((k) => (
        <rect key={k} x="156" y={46 + k * 18} width={[120, 150, 96, 136][k]} height="7" rx="3.5" className={k === 0 ? "ta-ink-soft ti-type" : "ta-line ti-type"}
          style={{ animationDelay: `${0.6 + k * 0.35}s` }} />
      ))}
    </Frame>
  );
}

export function Charts() {
  return (
    <Frame label="Your words become a chart">
      <rect x="34" y="18" width="150" height="30" rx="15" className="ta-accent" />
      <text x="109" y="38" textAnchor="middle" className="ta-tiny ta-on-accent">“failed runs per day”</text>
      {[0, 1, 2, 3, 4, 5].map((k) => (
        <rect key={k} x={52 + k * 22} width="14" rx="3" className="tch-bar ta-accent"
          y={130 - [30, 52, 22, 64, 40, 74][k]!} height={[30, 52, 22, 64, 40, 74][k]} style={{ animationDelay: `${0.4 + k * 0.08}s` }} />
      ))}
      <g className="tch-pie" style={{ transformOrigin: "276px 88px" }}>
        <circle cx="276" cy="88" r="40" style={{ fill: "var(--cat-1)" }} />
        <path d="M276 88 L276 48 A40 40 0 0 1 314 100 Z" style={{ fill: "var(--cat-2)" }} />
        <path d="M276 88 L314 100 A40 40 0 0 1 270 128 Z" style={{ fill: "var(--cat-3)" }} />
      </g>
    </Frame>
  );
}

export function Bot() {
  return (
    <div className="tour-botart">
      <BotAvatar mood="ok" className="tb-bot" />
      <div className="tb-bubble"><i /><i /><i /></div>
    </div>
  );
}

export function Live() {
  return (
    <Frame label="A radar sweeps every 30 seconds">
      {[0, 1, 2].map((k) => <circle key={k} cx="180" cy="75" r="18" className="tlv-wave" style={{ animationDelay: `${k * 0.8}s` }} />)}
      <circle cx="180" cy="75" r="9" className="ta-good" />
      <text x="180" y="140" textAnchor="middle" className="ta-caption">checked within seconds of every run</text>
    </Frame>
  );
}

export function Tools() {
  return (
    <Frame label="Refresh spins, the bell rings, the theme changes">
      <g className="tt-spin" style={{ transformOrigin: "90px 72px" }}>
        <path d="M90 52 a20 20 0 1 1 -18 11" className="tt-stroke" /><path d="M66 56 l6 8 l8 -5" className="tt-stroke" />
      </g>
      <g className="ti-bell" style={{ transformOrigin: "180px 54px" }}>
        <path d="M180 50 c-11 0 -15 8 -15 18 v10 l-5 6 h40 l-5 -6 v-10 c0 -10 -4 -18 -15 -18z" className="ta-accent" />
      </g>
      <g style={{ transformOrigin: "270px 72px" }}>
        <circle cx="270" cy="72" r="18" className="tt-sun" />
        <circle cx="280" cy="64" r="16" className="tt-moon" />
      </g>
      <text x="180" y="132" textAnchor="middle" className="ta-caption">refresh · alerts · full screen · theme</text>
    </Frame>
  );
}

export function Search() {
  return (
    <Frame label="Search finds the right row as you type">
      <rect x="60" y="16" width="240" height="28" rx="14" className="ta-tile" />
      <text x="84" y="35" className="ta-label tsr-typing">pl_sql</text>
      {[0, 1, 2].map((k) => (
        <g key={k}>
          <rect x="60" y={56 + k * 26} width="240" height="20" rx="7" className={k === 1 ? "tsr-hit" : "ta-tile"} />
          <rect x="74" y={63 + k * 26} width={[110, 80, 130][k]} height="6" rx="3" className="ta-line" />
        </g>
      ))}
      <g className="tsr-glass"><circle cx="0" cy="0" r="10" className="tt-stroke" /><path d="M7 7 l9 9" className="tt-stroke" /></g>
    </Frame>
  );
}

export function Done() {
  return (
    <Frame label="All set">
      {Array.from({ length: 14 }, (_, k) => (
        <circle key={k} cx="180" cy="72" r="4" className="tdn-dot" style={{ ["--a" as string]: `${k * (360 / 14)}deg`, fill: `var(--cat-${(k % 8) + 1})`, animationDelay: `${0.25 + (k % 3) * 0.05}s` }} />
      ))}
      <circle cx="180" cy="72" r="30" className="ta-good-soft" />
      <path d="M166 72 l10 10 l18 -20" className="tdn-check" />
    </Frame>
  );
}
