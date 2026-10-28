The phone navigation: a 58px bar fixed to the bottom, on `panel` at 86% over a blur with a `line` top edge and the safe-area inset below. It shows four role-specific pages plus "More", which opens the full sidebar as a sheet. Hand-written from `App.tsx`.

- Items are a 21px lucide icon over an 11px / 500 label in `muted`; the current page turns `brand-fg`, its icon stroke goes from 1.8 to 2.1 and a 2px `brand-solid` bar sits at the top of the item.
- "More" is the active item whenever the current page is not one of the four.
- Toasts sit 72px above the bottom so they clear the bar.
- The consumer provides `tabs` (key, label, short, icon), the current `page`, `onNavigate` and `onMore`; each role has its own four (`ROLE_TABBAR` in the app).
