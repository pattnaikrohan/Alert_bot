import { memo, useEffect, useId, useRef } from "react";

/*
 * The alert bot as a vector illustration: pearl-white shell, black glass visor with an LED face, floating mitten hands
 * (one holds the megaphone), a chest screen with a live heartbeat line, and a hover beam onto a holographic floor pad.
 * Calm, its lights take the theme's accent colour (so it matches every shade); worried it turns amber, alarmed red.
 *
 * Performance & stability by construction:
 *  - no WebGL, no SVG filters: every glow is a radial gradient;
 *  - loops that never change (pings, heartbeat, pad rings, scan) are CSS animations; everything that reacts (pointer,
 *    mood, pose, hands, particles) runs in ONE requestAnimationFrame loop writing attributes directly;
 *  - React re-renders only when the mood changes; the component is memoised.
 */

export type Mood = "ok" | "warning" | "critical";

// Shell lights (beacon, rims, rings) and the LED face, per mood.
const LIGHT: Record<Mood, { c: string; hi: string; lo: string }> = {
  ok: { c: "var(--accent)", hi: "color-mix(in srgb, var(--accent) 40%, #ffffff)", lo: "color-mix(in srgb, var(--accent) 70%, #000000)" },
  warning: { c: "#ffae1f", hi: "#ffe0a3", lo: "#d98500" },
  critical: { c: "#ff3d3d", hi: "#ffb3ad", lo: "#c8150f" },
};
const FACE: Record<Mood, { c: string; hi: string }> = {
  ok: { c: "color-mix(in srgb, var(--accent) 35%, #52f2ff)", hi: "#effeff" },
  warning: { c: "#ffb43a", hi: "#fff3d6" },
  critical: { c: "#ff4a3d", hi: "#ffe4df" },
};
const POSE: Record<Mood, number> = { ok: 0, warning: 0.55, critical: 1 };
const PARTICLES = 10;

// Right hand + megaphone: resting at the side (calm) → raised to the mouth (alarm).
const HOLD = { x0: 418, y0: 520, a0: 80, x1: 440, y1: 322, a1: -20 };

type Face = Mood | "sleep";
export interface Point { x: number; y: number }

type Key = "bot" | "shadow" | "head" | "face" | "eyes" | "brows" | "gloss" | "earL" | "earR" | "antenna" | "handL" | "handR"
  | "waves" | "mouth" | "body" | "beaconGlow" | "chestGlow" | "beam" | "rim" | "cheeks" | "emitter";

function Eyes({ mood }: { mood: Face }) {
  if (mood === "sleep") {
    return (
      <g fill="none" strokeWidth="12" strokeLinecap="round">
        <path d="M234 244 Q254 258 274 244" /><path d="M326 244 Q346 258 366 244" />
      </g>
    );
  }
  if (mood === "ok") {
    return (
      <g fill="none" strokeWidth="15" strokeLinecap="round">
        <path d="M232 250 A22 22 0 0 1 276 250" /><path d="M324 250 A22 22 0 0 1 368 250" />
      </g>
    );
  }
  if (mood === "warning") {
    return (
      <g>
        <rect x="240" y="216" width="28" height="46" rx="14" /><rect x="332" y="216" width="28" height="46" rx="14" />
        <circle cx="249" cy="227" r="4.5" fill="#ffffff" opacity="0.9" /><circle cx="341" cy="227" r="4.5" fill="#ffffff" opacity="0.9" />
      </g>
    );
  }
  return (
    <g fill="none" strokeWidth="15" strokeLinecap="round" strokeLinejoin="round">
      <path d="M238 218 L272 240 L238 262" /><path d="M362 218 L328 240 L362 262" />
    </g>
  );
}

function Brows({ mood }: { mood: Face }) {
  // worried: inner ends raised (alarmed, the squeezed > < eyes say it on their own)
  if (mood !== "warning") return null;
  return <><path d="M234 204 L272 194" /><path d="M328 194 L366 204" /></>;
}

function Mouth({ mood }: { mood: Face }) {
  if (mood === "sleep") return <ellipse cx="300" cy="296" rx="7" ry="5.5" />;
  if (mood === "ok") return <path d="M284 290 Q300 304 316 290" fill="none" strokeWidth="6" strokeLinecap="round" />;
  if (mood === "warning") return <path d="M280 296 q5 -5 10 0 t10 0 t10 0 t10 0" fill="none" strokeWidth="5" strokeLinecap="round" />;
  return <rect x="284" y="284" width="32" height="24" rx="12" />;
}

function Hand({ u, mirror = false }: { u: (name: string) => string; mirror?: boolean }) {
  // a mitten hand around (0,0): palm, thumb, wrist cuff with a light line
  const s = mirror ? -1 : 1;
  return (
    <g transform={`scale(${s * 1.14} 1.14)`}>
      <rect x="-17" y="16" width="34" height="16" rx="8" fill={u("darkBand")} />
      <rect x="-13" y="22.5" width="26" height="3.5" rx="1.75" fill={u("ring")} />
      <path d="M-20 4 C-22 -16 -10 -26 2 -26 C16 -26 24 -16 22 2 C21 16 12 22 0 22 C-12 22 -19 16 -20 4 Z" fill={u("shell")} />
      <path d="M-20 4 C-22 -16 -10 -26 2 -26 C16 -26 24 -16 22 2 C21 16 12 22 0 22 C-12 22 -19 16 -20 4 Z" fill={u("shade")} />
      <ellipse cx="-19" cy="-2" rx="8" ry="12" transform="rotate(-24 -19 -2)" fill={u("shell")} stroke="#b7c3d6" strokeOpacity="0.6" strokeWidth="1" />
      <path d="M-4 -24 C4 -27 14 -23 18 -14" fill="none" stroke="#ffffff" strokeWidth="3" strokeLinecap="round" opacity="0.85" />
      <path d="M-20 4 C-22 -16 -10 -26 2 -26 C16 -26 24 -16 22 2 C21 16 12 22 0 22 C-12 22 -19 16 -20 4 Z" fill="none" stroke={u("rim")} strokeWidth="2" />
    </g>
  );
}

interface RobotProps {
  mood: Mood;
  speaking: boolean;
  pokeSignal: number;
  onPoke: () => void;
  /** Screen point the robot looks at and points to with its free hand (e.g. an alert card). */
  pointAt?: Point | null;
  /** Dozing: eyes shut, Zzz, slow float. */
  asleep?: boolean;
  /** Face to show instead of the mood's (e.g. a happy face while cheering). */
  expression?: Mood;
  /** Bump to celebrate: hop with both hands up. */
  cheerSignal?: number;
  /** Working on something (running checks, thinking about a question): eyes search, beacon races. */
  thinking?: boolean;
}

function RobotSVGInner({ mood, speaking, pokeSignal, onPoke, pointAt = null, asleep = false, expression, cheerSignal = 0, thinking = false }: RobotProps) {
  const id = useId().replace(/:/g, "");
  const u = (name: string) => `url(#${id}-${name})`;
  const svg = useRef<SVGSVGElement>(null);
  const refs = useRef<Partial<Record<Key, SVGElement | null>>>({});
  const particles = useRef<(SVGCircleElement | null)[]>([]);
  const set = (k: Key) => (el: SVGElement | null) => { refs.current[k] = el; };
  const live = useRef({ mood, speaking, poke: pokeSignal, pointAt, asleep, cheer: cheerSignal, thinking });
  Object.assign(live.current, { mood, speaking, pointAt, asleep, thinking });
  const face: Face = asleep ? "sleep" : expression ?? mood;
  const L = LIGHT[mood];
  const F = FACE[face === "sleep" ? "ok" : face];
  const stop = (color: string, offset: string, opacity = 1) => <stop offset={offset} style={{ stopColor: color, stopOpacity: opacity }} />;

  useEffect(() => { live.current.poke = pokeSignal; }, [pokeSignal]);
  useEffect(() => { live.current.cheer = cheerSignal; }, [cheerSignal]);

  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const amp = reduced ? 0.3 : 1;
    const ptr = { x: 0, y: 0, sx: 0, sy: 0, lastMove: 0 };
    const st = { raise: POSE[live.current.mood], blink: 1, nextBlink: 1.8, blinkT: -1, waveT: -1, nextWave: 3.5, hopT: -1,
      lastPoke: live.current.poke, talk: 0, swing: 0, swingV: 0, nextGlance: 5, glanceT: -1, sleep: 0, cheerT: -1,
      lastCheer: live.current.cheer, lx: 168, ly: 500, la: 18, rx: HOLD.x0, ry: HOLD.y0, ra: HOLD.a0 };
    const seeds = Array.from({ length: PARTICLES }, (_, i) => ({ off: i / PARTICLES, x: (Math.random() - 0.5) * 230, s: 0.5 + Math.random() * 0.7 }));

    const onMove = (e: PointerEvent) => {
      const r = svg.current?.getBoundingClientRect();
      if (!r) return;
      ptr.x = Math.max(-1, Math.min(1, (e.clientX - (r.left + r.width * 0.47)) / (window.innerWidth * 0.45)));
      ptr.y = Math.max(-1, Math.min(1, (e.clientY - (r.top + r.height * 0.33)) / (window.innerHeight * 0.45)));
      ptr.lastMove = performance.now();
    };
    window.addEventListener("pointermove", onMove, { passive: true });

    const el = (k: Key) => refs.current[k];
    const T = (k: Key, v: string) => el(k)?.setAttribute("transform", v);
    const O = (k: Key, v: number) => el(k)?.setAttribute("opacity", v.toFixed(3));
    let raf = 0;
    let last = performance.now();
    const t0 = last;

    const frame = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const t = (now - t0) / 1000;
      const Lv = live.current;
      const ease = (a: number, b: number, k: number) => a + (b - a) * (1 - Math.exp(-dt * k));

      // idle glance: when calm and the pointer has been still, the eyes look around
      if (Lv.mood === "ok" && !Lv.asleep && !Lv.pointAt && !reduced && now - ptr.lastMove > 3000 && t > st.nextGlance && st.glanceT < 0) st.glanceT = 0;
      let glanceX = 0;
      if (st.glanceT >= 0) {
        st.glanceT += dt;
        const p = st.glanceT / 2.6;
        glanceX = p < 0.3 ? -Math.sin((p / 0.3) * (Math.PI / 2)) : p < 0.55 ? -1 : p < 0.8 ? -1 + ((p - 0.55) / 0.25) * 2 : p < 1 ? 1 - ((p - 0.8) / 0.2) : 0;
        if (p >= 1) { st.glanceT = -1; st.nextGlance = t + 6 + Math.random() * 6; }
      }
      // worried: the eyes dart left and right
      const dart = Lv.mood === "warning" && !reduced ? Math.sign(Math.sin(t * 0.9)) * 0.45 : 0;
      let gx = ptr.x, gy = ptr.y;
      const box = svg.current?.getBoundingClientRect();
      if (Lv.pointAt && box) {
        gx = Math.max(-1, Math.min(1, (Lv.pointAt.x - (box.left + box.width * 0.47)) / (window.innerWidth * 0.45)));
        gy = Math.max(-1, Math.min(1, (Lv.pointAt.y - (box.top + box.height * 0.33)) / (window.innerHeight * 0.45)));
      }
      st.sleep = ease(st.sleep, Lv.asleep ? 1 : 0, 2.5);
      const search = Lv.thinking && !reduced ? Math.sin(t * 3.2) * 0.85 : 0;
      ptr.sx = ease(ptr.sx, (gx + glanceX * 0.55 + dart + search) * (1 - st.sleep), 5);
      ptr.sy = ease(ptr.sy, gy * (1 - st.sleep) + st.sleep * 0.3, 5);
      const px = ptr.sx * amp;
      const py = ptr.sy * amp;

      // float + hop (poke)
      if (Lv.poke !== st.lastPoke) { st.lastPoke = Lv.poke; st.hopT = 0; }
      if (Lv.cheer !== st.lastCheer) { st.lastCheer = Lv.cheer; st.hopT = 0; st.cheerT = 0; }
      if (st.cheerT >= 0) { st.cheerT += dt; if (st.cheerT > 2.8) st.cheerT = -1; }
      const cheering = st.cheerT >= 0;
      let y = Math.sin(t * (1.6 - st.sleep * 0.8)) * (9 - st.sleep * 4) * amp;
      let sqX = 1, sqY = 1, wiggle = 0;
      if (st.hopT >= 0) {
        st.hopT += dt;
        const p = Math.min(1, st.hopT / 0.9);
        if (p < 0.16) { const k = Math.sin((p / 0.16) * Math.PI); sqY = 1 - 0.07 * k; sqX = 1 + 0.05 * k; }
        else { const q = (p - 0.16) / 0.84; y -= Math.sin(q * Math.PI) * 60; sqY = 1 + 0.04 * Math.sin(q * Math.PI); wiggle = Math.sin(q * Math.PI * 3) * 6 * (1 - q); }
        if (p >= 1) st.hopT = -1;
      }
      const shake = Lv.mood === "critical" && !reduced ? Math.sin(t * 38) * 0.6 : 0;
      T("bot", `translate(${shake.toFixed(2)} ${y.toFixed(2)}) translate(300 600) scale(${sqX.toFixed(4)} ${sqY.toFixed(4)}) rotate(${(wiggle + px * 2.5).toFixed(2)}) translate(-300 -600)`);
      const lift = (-y + 9) / 78;
      T("shadow", `translate(300 708) scale(${(1 - lift * 0.22).toFixed(3)} 1) translate(-300 -708)`);
      O("shadow", 0.9 - lift * 0.4);
      O("beam", (0.55 + 0.25 * Math.sin(t * 2.3)) * (1 - lift * 0.5));

      // head turns toward the pointer: layers move by depth
      T("head", `rotate(${(px * 5 + st.sleep * 7).toFixed(2)} 300 360) translate(${(px * 7).toFixed(2)} ${(py * 4 + st.sleep * 6).toFixed(2)})`);
      T("face", `translate(${(px * 11).toFixed(2)} ${(py * 7).toFixed(2)})`);
      T("gloss", `translate(${(-px * 9).toFixed(2)} ${(-py * 5).toFixed(2)})`);
      T("earL", `translate(${(-px * 5 + Math.max(0, px) * 6).toFixed(2)} 0)`);
      T("earR", `translate(${(-px * 5 + Math.min(0, px) * 6).toFixed(2)} 0)`);
      T("body", `translate(${(px * 3).toFixed(2)} 0)`);

      // blink (brows dip a little with it)
      if (t > st.nextBlink && st.blinkT < 0 && !Lv.asleep) st.blinkT = 0;
      if (st.blinkT >= 0) {
        st.blinkT += dt;
        const p = st.blinkT / 0.16;
        st.blink = p < 1 ? 1 - Math.sin(p * Math.PI) * 0.9 : 1;
        if (p >= 1) { st.blinkT = -1; st.nextBlink = t + 2.2 + Math.random() * 3.2; }
      }
      T("eyes", `translate(${(px * 6).toFixed(2)} ${(py * 4 + 240).toFixed(2)}) scale(1 ${st.blink.toFixed(3)}) translate(0 -240)`);
      const browLift = Lv.mood === "warning" ? Math.sin(t * 2.1) * 2 : 0;
      T("brows", `translate(${(px * 6).toFixed(2)} ${(py * 4 + (1 - st.blink) * 4 + browLift).toFixed(2)})`);

      // mouth: talks while the bubble types; shouts when alarmed
      const talking = Lv.speaking ? 0.5 + 0.5 * Math.abs(Math.sin(t * 12) * Math.sin(t * 6.7)) : 0;
      st.talk = ease(st.talk, talking, 16);
      const open = Lv.mood === "critical" ? 0.75 + st.talk * 0.5 : 1 + st.talk * 0.9;
      T("mouth", `translate(${(px * 6).toFixed(2)} ${(py * 4).toFixed(2)}) translate(300 296) scale(${(1 + st.talk * 0.12).toFixed(3)} ${open.toFixed(3)}) translate(-300 -296)`);
      O("cheeks", Lv.mood === "ok" && !Lv.asleep ? 0.55 + 0.15 * Math.sin(t * 1.6) : 0);

      // antenna spring
      const drive = Math.cos(t * 1.6) * 14 + (st.hopT >= 0 ? 90 : 0) + px * 10;
      st.swingV += (-st.swing * 55 - st.swingV * 7 + drive) * dt;
      st.swing += st.swingV * dt;
      T("antenna", `rotate(${(st.swing * amp).toFixed(2)} 300 120)`);
      const rate = Lv.thinking ? 9 : Lv.asleep ? 0.6 : Lv.mood === "critical" ? 6 : Lv.speaking ? 4 : 1.6;
      const pulse = 0.5 + 0.5 * Math.sin(t * rate);
      O("beaconGlow", (0.45 + pulse * 0.55) * (1 - st.sleep * 0.6));
      O("chestGlow", 0.45 + pulse * 0.45);
      O("rim", (Lv.mood === "ok" ? 0.3 : 0.5) + pulse * (Lv.mood === "ok" ? 0.25 : 0.5));
      O("emitter", 0.6 + 0.4 * Math.sin(t * 9) * Math.sin(t * 5.3));

      // particles rise from the floor pad and fade
      seeds.forEach((s, i) => {
        const c = particles.current[i];
        if (!c) return;
        const k = (t * 0.22 * s.s + s.off) % 1;
        c.setAttribute("cx", (300 + s.x * (1 - k * 0.45)).toFixed(1));
        c.setAttribute("cy", (706 - k * 150).toFixed(1));
        c.setAttribute("opacity", (Math.sin(k * Math.PI) * 0.85 * amp).toFixed(3));
      });

      // right hand + megaphone: pose by mood, a tremble when alarmed
      st.raise = ease(st.raise, POSE[Lv.mood], 4);
      const r = st.raise;
      let hx = HOLD.x0 + (HOLD.x1 - HOLD.x0) * r + px * 3;
      let hy = HOLD.y0 + (HOLD.y1 - HOLD.y0) * r;
      let ha = HOLD.a0 + (HOLD.a1 - HOLD.a0) * r + (Lv.mood === "critical" ? Math.sin(t * 7) * 2.5 * amp : 0);
      if (cheering) { hx = 452; hy = 292; ha = -58 + Math.sin(t * 10) * 7 * amp; }
      st.rx = ease(st.rx, hx, 9); st.ry = ease(st.ry, hy, 9); st.ra = ease(st.ra, ha, 12);
      T("handR", `translate(${st.rx.toFixed(2)} ${(st.ry + Math.sin(t * 1.6 + 0.8) * 4 * amp).toFixed(2)}) rotate(${st.ra.toFixed(2)})`);
      const shout = Lv.mood === "critical" ? (Lv.speaking ? 1 : 0.8) : Lv.mood === "warning" && Lv.speaking ? 0.5 : 0;
      const w = el("waves");
      if (w) {
        const k = (t * 1.8) % 1;
        w.setAttribute("opacity", (shout * (1 - k * k)).toFixed(3));
        w.setAttribute("transform", `translate(${(176 + k * 30).toFixed(2)} -29) scale(${(0.7 + k * 0.5).toFixed(3)})`);
      }

      // left hand floats at the side and waves now and then when all is well
      if (Lv.mood === "ok" && !Lv.asleep && !Lv.pointAt && t > st.nextWave && st.waveT < 0 && !reduced) st.waveT = 0;
      let lx = 168, ly = 500, la = 18;
      if (Lv.mood === "critical") { lx = 186; ly = 470; la = -10 + Math.sin(t * 7 + 1) * 4 * amp; }
      if (st.waveT >= 0) {
        st.waveT += dt;
        const p = st.waveT / 2.4;
        const up = Math.sin(Math.min(1, p) * Math.PI);
        lx -= up * 22; ly -= up * 168; la = 18 - up * 34 + Math.sin(st.waveT * 12) * 22 * up;
        if (p >= 1) { st.waveT = -1; st.nextWave = t + 8 + Math.random() * 6; }
      }
      lx += px * 3;
      const m = Lv.pointAt ? svg.current?.getScreenCTM() : null;
      if (m && Lv.pointAt) {
        // reach toward the point from the left shoulder, fingers first
        const target = new DOMPoint(Lv.pointAt.x, Lv.pointAt.y).matrixTransform(m.inverse());
        let ang = Math.atan2(target.y - 446, target.x - 214);
        if (ang < 0) ang += Math.PI * 2;
        ang = Math.max(Math.PI * 0.6, Math.min(Math.PI * 1.4, ang));
        lx = 214 + Math.cos(ang) * 120;
        ly = 446 + Math.sin(ang) * 120;
        la = (ang * 180) / Math.PI - 270 + Math.sin(t * 6) * 4 * amp;
      } else if (cheering) {
        lx = 146; ly = 318; la = -18 + Math.sin(t * 14) * 26 * amp;
      } else if (Lv.asleep) {
        lx = 178; ly = 522; la = 28;
      }
      st.lx = ease(st.lx, lx, 9); st.ly = ease(st.ly, ly, 9); st.la = ease(st.la, la, 14);
      T("handL", `translate(${st.lx.toFixed(2)} ${(st.ly + Math.sin(t * 1.6 + 2.2) * 5 * amp).toFixed(2)}) rotate(${st.la.toFixed(2)})`);

      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("pointermove", onMove); };
  }, []);

  const torso = "M300 380 C374 380 406 418 406 474 C406 544 366 602 300 602 C234 602 194 544 194 474 C194 418 226 380 300 380 Z";
  const ecg = Array.from({ length: 5 }, (_, i) => {
    const x = 196 + i * 60;
    return `L${x + 22} 462 L${x + 28} 450 L${x + 34} 478 L${x + 40} 446 L${x + 46} 466 L${x + 50} 462 L${x + 60} 462`;
  }).join(" ");

  return (
    <svg ref={svg} viewBox="-20 -40 680 780" className="robot-svg" role="img"
      aria-label={asleep ? "Alert bot, dozing: all quiet" : mood === "critical" ? "Alert bot shouting into its megaphone" : mood === "warning" ? "Alert bot, worried" : "Alert bot, all calm"}>
      <defs>
        <radialGradient id={`${id}-shell`} cx="34%" cy="22%" r="92%">
          <stop offset="0" stopColor="#ffffff" /><stop offset="0.42" stopColor="#f5f8fc" /><stop offset="0.76" stopColor="#dfe6f1" /><stop offset="1" stopColor="#bcc8db" />
        </radialGradient>
        <linearGradient id={`${id}-shade`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0.5" stopColor="#4a5f8c" stopOpacity="0" /><stop offset="1" stopColor="#4a5f8c" stopOpacity="0.26" />
        </linearGradient>
        <linearGradient id={`${id}-rim`} x1="0" y1="0" x2="1" y2="1">{stop(L.hi, "0.45", 0)}{stop(L.c, "1", 0.95)}</linearGradient>
        <radialGradient id={`${id}-spec`}><stop offset="0" stopColor="#ffffff" stopOpacity="0.95" /><stop offset="1" stopColor="#ffffff" stopOpacity="0" /></radialGradient>
        <linearGradient id={`${id}-cylH`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#ffffff" /><stop offset="0.6" stopColor="#eef2f8" /><stop offset="1" stopColor="#c6d0e2" /></linearGradient>
        <radialGradient id={`${id}-dark`} cx="35%" cy="30%" r="75%"><stop offset="0" stopColor="#4d5873" /><stop offset="0.38" stopColor="#181e2c" /><stop offset="1" stopColor="#05070c" /></radialGradient>
        <linearGradient id={`${id}-darkBand`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#3a4356" /><stop offset="0.45" stopColor="#141924" /><stop offset="1" stopColor="#05070c" /></linearGradient>
        <radialGradient id={`${id}-visor`} cx="46%" cy="26%" r="85%"><stop offset="0" stopColor="#1c2742" /><stop offset="0.55" stopColor="#0a0f1e" /><stop offset="1" stopColor="#02040a" /></radialGradient>
        <linearGradient id={`${id}-glass`} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#ffffff" stopOpacity="0.36" /><stop offset="1" stopColor="#ffffff" stopOpacity="0" /></linearGradient>
        <linearGradient id={`${id}-scan`} x1="0" y1="0" x2="0" y2="1">{stop(F.c, "0", 0)}{stop(F.c, "0.5", 0.14)}{stop(F.c, "1", 0)}</linearGradient>
        <linearGradient id={`${id}-face`} x1="0" y1="0" x2="0" y2="1">{stop(F.hi, "0")}{stop(F.c, "1")}</linearGradient>
        <radialGradient id={`${id}-faceGlow`}>{stop(F.c, "0", 0.5)}{stop(F.c, "1", 0)}</radialGradient>
        <radialGradient id={`${id}-moodGlow`}>{stop(L.c, "0", 0.6)}{stop(L.c, "1", 0)}</radialGradient>
        <radialGradient id={`${id}-beacon`} cx="36%" cy="32%" r="70%">{stop(L.hi, "0")}{stop(L.c, "0.5")}{stop(L.lo, "1")}</radialGradient>
        <linearGradient id={`${id}-moodRing`} x1="0" y1="0" x2="0" y2="1">{stop(L.hi, "0")}{stop(L.c, "1")}</linearGradient>
        <linearGradient id={`${id}-ring`} x1="0" y1="0" x2="1" y2="1">{stop(L.hi, "0")}{stop(L.c, "0.5")}{stop(L.lo, "1")}</linearGradient>
        <radialGradient id={`${id}-screen`} cx="50%" cy="40%" r="75%"><stop offset="0" stopColor="#13203a" /><stop offset="1" stopColor="#04070e" /></radialGradient>
        <linearGradient id={`${id}-beam`} x1="0" y1="0" x2="0" y2="1">{stop(L.c, "0", 0.42)}{stop(L.c, "0.7", 0.08)}{stop(L.c, "1", 0)}</linearGradient>
        <radialGradient id={`${id}-pad`}>{stop(L.c, "0", 0.38)}{stop(L.c, "0.6", 0.1)}{stop(L.c, "1", 0)}</radialGradient>
        <linearGradient id={`${id}-red`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#ff7a6e" /><stop offset="0.45" stopColor="#ef2f26" /><stop offset="1" stopColor="#b3140e" /></linearGradient>
        <radialGradient id={`${id}-bell`} cx="40%" cy="40%" r="70%"><stop offset="0" stopColor="#ffb0a6" /><stop offset="0.6" stopColor="#f0463c" /><stop offset="1" stopColor="#b5160f" /></radialGradient>
        <radialGradient id={`${id}-floor`}><stop offset="0" stopColor="#0b1530" stopOpacity="0.45" /><stop offset="1" stopColor="#0b1530" stopOpacity="0" /></radialGradient>
        <clipPath id={`${id}-visorClip`}><rect x="180" y="154" width="240" height="166" rx="78" /></clipPath>
        <clipPath id={`${id}-screenClip`}><rect x="250" y="440" width="100" height="46" rx="13" /></clipPath>
      </defs>

      {/* ── holographic floor pad (stays on the floor while the robot floats) ── */}
      <ellipse ref={set("shadow")} cx="300" cy="708" rx="150" ry="16" fill={u("floor")} />
      <ellipse cx="300" cy="704" rx="190" ry="34" fill={u("pad")} />
      <g fill="none" style={{ stroke: L.c }}>
        <ellipse cx="300" cy="704" rx="158" ry="26" strokeWidth="1.5" strokeOpacity="0.35" className="rb-pad" />
        <ellipse cx="300" cy="704" rx="118" ry="19" strokeWidth="2.5" strokeOpacity="0.55" strokeDasharray="10 7" className="rb-pad rb-pad-rev" />
        <ellipse cx="300" cy="704" rx="76" ry="12" strokeWidth="2" strokeOpacity="0.8" />
      </g>
      {Array.from({ length: PARTICLES }, (_, i) => (
        <circle key={i} ref={(c) => { particles.current[i] = c; }} r={i % 3 === 0 ? 2.8 : 1.8} style={{ fill: i % 2 ? L.hi : F.c }} opacity="0" />
      ))}

      <g ref={set("bot")} onClick={onPoke} style={{ cursor: "pointer" }}>
        {/* hover beam from the emitter down to the pad */}
        <path ref={set("beam")} d="M270 600 L328 600 L404 704 L196 704 Z" fill={u("beam")} />

        {/* ── antenna (behind the head): beacon with radar pings ── */}
        <g ref={set("antenna")}>
          <circle ref={set("beaconGlow")} cx="300" cy="50" r="50" fill={u("moodGlow")} />
          <circle cx="300" cy="50" r="18" fill="none" style={{ stroke: L.c }} strokeWidth="2" className="rb-ping" />
          <circle cx="300" cy="50" r="18" fill="none" style={{ stroke: L.c }} strokeWidth="2" className="rb-ping rb-ping-2" />
          <rect x="295.5" y="64" width="9" height="58" rx="4.5" fill={u("darkBand")} />
          <rect x="293" y="88" width="14" height="7" rx="3.5" style={{ fill: L.c }} opacity="0.85" />
          <circle cx="300" cy="50" r="17" fill={u("beacon")} />
          <circle cx="300" cy="50" r="17" fill="none" stroke="#ffffff" strokeOpacity="0.5" strokeWidth="1.5" />
          <ellipse cx="294" cy="43" rx="6" ry="4" fill="#ffffff" opacity="0.8" />
        </g>
        <ellipse cx="300" cy="122" rx="38" ry="10" fill={u("dark")} />
        <ellipse cx="300" cy="119" rx="31" ry="6" fill="none" stroke={u("moodRing")} strokeWidth="3" />

        {/* ── neck + body ── */}
        <g ref={set("body")}>
          <rect x="266" y="346" width="68" height="40" rx="14" fill={u("darkBand")} />
          <ellipse cx="300" cy="384" rx="62" ry="10" fill={u("moodGlow")} opacity="0.8" />
          <ellipse cx="300" cy="382" rx="52" ry="7" fill="none" stroke={u("ring")} strokeWidth="4" />

          <path d={torso} fill={u("shell")} />
          <path d={torso} fill={u("shade")} />
          <path d={torso} fill="none" stroke={u("rim")} strokeWidth="3" />
          {/* panel seams (dark line + light line = a small bevel) */}
          <path d="M214 532 Q300 562 386 532" fill="none" stroke="#8d9bb5" strokeOpacity="0.55" strokeWidth="1.5" />
          <path d="M214 535 Q300 565 386 535" fill="none" stroke="#ffffff" strokeOpacity="0.8" strokeWidth="1.5" />
          <path d="M226 546 Q300 574 374 546" fill="none" style={{ stroke: L.c }} strokeOpacity="0.6" strokeWidth="2.5" strokeLinecap="round" strokeDasharray="5 9" className="rb-dash" />
          <ellipse cx="252" cy="410" rx="38" ry="11" transform="rotate(-20 252 410)" fill={u("spec")} opacity="0.85" />

          {/* chest screen: live heartbeat line */}
          <ellipse ref={set("chestGlow")} cx="300" cy="463" rx="84" ry="48" fill={u("moodGlow")} />
          <rect x="244" y="434" width="112" height="58" rx="18" fill={u("darkBand")} />
          <rect x="250" y="440" width="100" height="46" rx="13" fill={u("screen")} />
          <g clipPath={u("screenClip")}>
            <g stroke="#ffffff" strokeOpacity="0.05">{[452, 463, 474].map((yy) => <path key={yy} d={`M250 ${yy} H350`} />)}</g>
            <path d={`M196 462 ${ecg}`} fill="none" style={{ stroke: L.c }} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" className="rb-ecg" />
            <path d={`M196 462 ${ecg}`} fill="none" style={{ stroke: L.hi }} strokeWidth="1" strokeLinejoin="round" className="rb-ecg" />
          </g>
          <path d="M256 444 Q300 438 344 444" fill="none" stroke="#ffffff" strokeOpacity="0.18" strokeWidth="2" strokeLinecap="round" />

          {/* hover emitter */}
          <ellipse cx="300" cy="596" rx="44" ry="11" fill={u("darkBand")} />
          <ellipse ref={set("emitter")} cx="300" cy="598" rx="34" ry="6" fill={u("face")} />
          <ellipse cx="300" cy="602" rx="58" ry="16" fill={u("faceGlow")} />
        </g>

        {/* ── head ── */}
        <g ref={set("head")}>
          <g ref={set("earL")}>
            <ellipse cx="148" cy="238" rx="28" ry="58" fill={u("cylH")} />
            <ellipse cx="148" cy="238" rx="28" ry="58" fill={u("shade")} />
            <ellipse cx="139" cy="238" rx="14" ry="38" fill={u("dark")} />
            <ellipse cx="139" cy="238" rx="14" ry="38" fill="none" stroke={u("moodRing")} strokeWidth="4.5" strokeDasharray="18 6" className="rb-dash" />
          </g>
          <g ref={set("earR")}>
            <ellipse cx="452" cy="238" rx="28" ry="58" fill={u("cylH")} />
            <ellipse cx="452" cy="238" rx="28" ry="58" fill={u("shade")} />
            <ellipse cx="461" cy="238" rx="14" ry="38" fill={u("dark")} />
            <ellipse cx="461" cy="238" rx="14" ry="38" fill="none" stroke={u("moodRing")} strokeWidth="4.5" strokeDasharray="18 6" className="rb-dash rb-dash-rev" />
          </g>

          <rect x="152" y="118" width="296" height="236" rx="108" fill={u("shell")} />
          <rect x="152" y="118" width="296" height="236" rx="108" fill={u("shade")} />
          <rect x="152" y="118" width="296" height="236" rx="108" fill="none" stroke={u("rim")} strokeWidth="3.5" />
          <path d="M232 132 Q300 121 368 132" fill="none" stroke="#8d9bb5" strokeOpacity="0.45" strokeWidth="1.5" strokeLinecap="round" />
          <path d="M232 135 Q300 124 368 135" fill="none" stroke="#ffffff" strokeOpacity="0.9" strokeWidth="1.5" strokeLinecap="round" />
          <g ref={set("gloss")}>
            <ellipse cx="228" cy="142" rx="68" ry="15" transform="rotate(-15 228 142)" fill={u("spec")} />
            <circle cx="392" cy="140" r="6" fill="#ffffff" opacity="0.8" />
          </g>

          <g ref={set("face")}>
            <rect x="172" y="146" width="256" height="182" rx="86" fill="#090d16" />
            <rect ref={set("rim")} x="173.5" y="147.5" width="253" height="179" rx="85" fill="none" style={{ stroke: L.c }} strokeWidth="2.5" opacity="0.4" />
            <rect x="180" y="154" width="240" height="166" rx="78" fill={u("visor")} />
            <g clipPath={u("visorClip")}>
              <g stroke={F.c} strokeOpacity="0.05" strokeWidth="1">
                {Array.from({ length: 8 }, (_, i) => <path key={i} d={`M180 ${168 + i * 20} H420`} />)}
              </g>
              <rect x="180" y="110" width="240" height="46" fill={u("scan")} className="rb-scan" />
              {/* the face's light spilling on the lower glass */}
              <ellipse cx="300" cy="330" rx="120" ry="26" fill={u("faceGlow")} opacity="0.5" />
            </g>

            <g ref={set("cheeks")} opacity="0">
              <ellipse cx="216" cy="284" rx="20" ry="10" fill={u("faceGlow")} />
              <ellipse cx="384" cy="284" rx="20" ry="10" fill={u("faceGlow")} />
            </g>
            <g ref={set("eyes")}>
              <ellipse cx="254" cy="240" rx="52" ry="42" fill={u("faceGlow")} />
              <ellipse cx="346" cy="240" rx="52" ry="42" fill={u("faceGlow")} />
              <g fill={u("face")} stroke={u("face")}><Eyes mood={face} /></g>
            </g>
            <g ref={set("brows")} fill="none" stroke={u("face")} strokeWidth="7" strokeLinecap="round"><Brows mood={face} /></g>
            <g ref={set("mouth")} fill={u("face")} stroke={u("face")}><Mouth mood={face} /></g>

            {/* glass reflections on top of the face */}
            <path d="M196 226 C198 186 236 166 300 164 C254 176 224 194 212 234 C208 246 196 244 196 226 Z" fill={u("glass")} />
            <path d="M330 164 C372 166 400 184 408 206" fill="none" stroke="#ffffff" strokeOpacity="0.22" strokeWidth="3" strokeLinecap="round" />
          </g>
        </g>

        {/* ── dozing: Zzz drifting up from the head ── */}
        {asleep && (
          <g className="rb-zzz" style={{ fill: L.hi }} fontFamily="var(--font-display), sans-serif" fontWeight="800">
            <text x="168" y="150" fontSize="26" className="rb-z" style={{ animationDelay: "0s" }}>Z</text>
            <text x="128" y="108" fontSize="34" className="rb-z" style={{ animationDelay: "0.9s" }}>Z</text>
            <text x="80" y="60" fontSize="42" className="rb-z" style={{ animationDelay: "1.8s" }}>Z</text>
          </g>
        )}

        {/* ── hands: the left floats free, the right holds the megaphone ── */}
        <g ref={set("handL")} transform="translate(168 500) rotate(18)"><Hand u={u} mirror /></g>

        <g ref={set("handR")} transform={`translate(${HOLD.x0} ${HOLD.y0}) rotate(${HOLD.a0})`}>
          <g transform="scale(0.84)">
            <rect x="-7" y="-14" width="14" height="34" rx="6" fill={u("red")} />
            <rect x="2" y="-48" width="66" height="38" rx="16" fill={u("cylH")} />
            <rect x="2" y="-48" width="66" height="38" rx="16" fill={u("shade")} />
            <ellipse cx="6" cy="-29" rx="8" ry="18" fill="#e3e9f3" />
            <path d="M62 -47 L156 -92 C168 -60 168 2 156 34 L62 -11 Z" fill={u("red")} />
            <path d="M70 -44 L150 -83 L152 -75 L72 -38 Z" fill="#ffffff" opacity="0.34" />
            <ellipse cx="156" cy="-29" rx="19" ry="63" fill={u("bell")} stroke="#ffffff" strokeWidth="9" />
            <ellipse cx="151" cy="-29" rx="7" ry="26" fill="#8f0f0a" opacity="0.4" />
            <g ref={set("waves")} opacity="0" stroke="#ff3d3d" strokeWidth="8" strokeLinecap="round" fill="none">
              <path d="M0 -58 A70 70 0 0 1 0 58" /><path d="M24 -78 A96 96 0 0 1 24 78" strokeOpacity="0.6" />
            </g>
          </g>
          <g transform="rotate(-90) translate(-8 -2)"><Hand u={u} /></g>
        </g>
      </g>
    </svg>
  );
}

export const RobotSVG = memo(RobotSVGInner);
