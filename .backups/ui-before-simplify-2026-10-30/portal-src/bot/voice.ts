// The bot's voice: an AI voice (Fish Audio through the API) when available, otherwise the browser's built-in speech.
// Long text is spoken in sentence groups (the next group is fetched while the current one plays).
import { useCallback, useEffect, useRef, useState } from "react";

const KEY = "alertbot.voice";
const supported = typeof window !== "undefined" && "speechSynthesis" in window;
const canPlay = typeof window !== "undefined" && typeof Audio !== "undefined";

function pickVoice(): SpeechSynthesisVoice | null {
  const voices = window.speechSynthesis.getVoices().filter((v) => v.lang.toLowerCase().startsWith("en"));
  const prefer = [/aria/i, /jenny/i, /natural/i, /google us english/i, /samantha/i, /zira/i, /en-gb/i];
  for (const p of prefer) {
    const v = voices.find((x) => p.test(x.name) || p.test(x.lang));
    if (v) return v;
  }
  return voices[0] ?? null;
}

/** Text as it should sound: no bullets, quotes or symbols read aloud. */
function speakable(text: string): string[] {
  const clean = text
    .replace(/[•⚠“”"]/g, " ")
    .replace(/\(([^)]*)\)/g, ", $1,")
    .replace(/\s+%/g, "%")
    .replace(/ {2,}/g, " ");
  return clean
    .split(/\n+|(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function useVoice() {
  const [enabled, setEnabled] = useState(() => {
    try { return localStorage.getItem(KEY) === "on"; } catch { return false; }
  });
  const [speaking, setSpeaking] = useState(false);
  const run = useRef(0);
  const audio = useRef<HTMLAudioElement | null>(null);
  const aiVoice = useRef(true); // turns off for the session after the AI voice fails, so the browser voice takes over

  useEffect(() => {
    if (!supported) return;
    window.speechSynthesis.getVoices(); // voices load lazily; ask early
    return () => window.speechSynthesis.cancel();
  }, []);

  const stop = useCallback(() => {
    run.current++;
    audio.current?.pause();
    if (supported) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, []);

  /** The AI voice (Fish Audio via the API), sentence groups played in order; false if it isn't available. */
  const sayWithAI = useCallback(async (text: string, me: number): Promise<boolean> => {
    if (!aiVoice.current) return false;
    const chunks: string[] = [];
    for (const part of speakable(text)) {
      const last = chunks[chunks.length - 1];
      if (last && last.length + part.length < 500) chunks[chunks.length - 1] = `${last} ${part}`;
      else chunks.push(part);
    }
    const fetchAudio = (t: string) => fetch("/api/speak", { method: "POST", headers: { "Content-Type": "application/json", "X-AlertBot": "1" }, body: JSON.stringify({ text: t }) })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))));
    try {
      let next = fetchAudio(chunks[0]!);
      for (let i = 0; i < chunks.length; i++) {
        const blob = await next;
        if (me !== run.current) return true;
        if (i + 1 < chunks.length) next = fetchAudio(chunks[i + 1]!);
        const url = URL.createObjectURL(blob);
        await new Promise<void>((done, fail) => {
          const a = new Audio(url);
          audio.current = a;
          a.onended = () => { URL.revokeObjectURL(url); done(); };
          a.onerror = () => { URL.revokeObjectURL(url); fail(new Error("playback")); };
          void a.play().catch(fail);
        });
        if (me !== run.current) return true;
      }
      return true;
    } catch {
      aiVoice.current = false;
      return false;
    }
  }, []);

  /** Speak `text`, replacing anything being said. Resolves when finished (or interrupted). */
  const say = useCallback((text: string) => new Promise<void>((resolve) => {
    if (!text.trim()) return resolve();
    audio.current?.pause();
    if (supported) window.speechSynthesis.cancel();
    const me = ++run.current;
    setSpeaking(true);
    void sayWithAI(text, me).then((done) => {
      if (done || me !== run.current) { if (me === run.current) setSpeaking(false); return resolve(); }
      if (!supported) { setSpeaking(false); return resolve(); }
      browserSay(text, me, resolve);
    });
  }), [sayWithAI]);

  /** The browser's built-in voice: the fallback. */
  const browserSay = useCallback((text: string, me: number, resolve: () => void) => {
    const synth = window.speechSynthesis;
    const parts = speakable(text);
    const voice = pickVoice();
    let i = 0;
    const next = () => {
      if (me !== run.current) return resolve();
      if (i >= parts.length) { setSpeaking(false); return resolve(); }
      const u = new SpeechSynthesisUtterance(parts[i++]!);
      if (voice) u.voice = voice;
      u.rate = 1.03;
      u.pitch = 1.12;
      u.onend = next;
      u.onerror = () => { setSpeaking(false); resolve(); };
      synth.speak(u);
    };
    next();
  }, []);

  const toggle = useCallback(() => {
    setEnabled((on) => {
      const next = !on;
      try { localStorage.setItem(KEY, next ? "on" : "off"); } catch { /* ignore */ }
      if (!next) stop();
      return next;
    });
  }, [stop]);

  return { supported: supported || canPlay, enabled, toggle, say, stop, speaking };
}
