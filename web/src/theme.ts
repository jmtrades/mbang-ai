// Light / dark theme. The preference ("system" | "light" | "dark") lives in localStorage; the resolved
// theme is written to <html data-theme>. index.html applies it before first paint, this module keeps it live.
import { useSyncExternalStore } from "react";

export type ThemePref = "system" | "light" | "dark";
export type Theme = "light" | "dark";

const KEY = "op.theme";
const mq = typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
const subs = new Set<() => void>();

function readPref(): ThemePref {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

let pref: ThemePref = readPref();

export const resolveTheme = (p: ThemePref = pref): Theme => (p === "system" ? (mq?.matches ? "dark" : "light") : p);

function paint() {
  const t = resolveTheme();
  const el = document.documentElement;
  el.dataset.theme = t;
  el.style.colorScheme = t;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "dark" ? "#0a0a0c" : "#f2f2f4");
}

function emit() {
  subs.forEach((f) => f());
}

export function setThemePref(next: ThemePref) {
  pref = next;
  try {
    if (next === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, next);
  } catch {
    /* private mode: the choice still applies for this visit */
  }
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  if (doc.startViewTransition && !reduce) doc.startViewTransition(paint);
  else paint();
  emit();
}

export function toggleTheme() {
  setThemePref(resolveTheme() === "dark" ? "light" : "dark");
}

mq?.addEventListener?.("change", () => {
  if (pref === "system") {
    paint();
    emit();
  }
});

const subscribe = (f: () => void) => {
  subs.add(f);
  return () => {
    subs.delete(f);
  };
};
const snapshot = () => `${pref}:${resolveTheme()}`;

export function useTheme() {
  useSyncExternalStore(subscribe, snapshot, snapshot);
  return { pref, theme: resolveTheme(), setPref: setThemePref, toggle: toggleTheme };
}

paint();
