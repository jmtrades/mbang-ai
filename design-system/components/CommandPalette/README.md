⌘K / Ctrl K: a 620px dialog on the scrim with a 52px search row, grouped results (Recent, Actions, Pages, or one Results group while typing), and a `panel-2` footer of key hints. Each row is 40px with a 24px icon well that turns `brand-soft` with a `brand-fg` glyph when active; matched letters are bold. Shown open and static; the original filters with a subsequence matcher and keeps recent pages per device. Hand-written from `web/src/components/CommandPalette.tsx`.

- `role="dialog"` with a combobox input; arrows, Home/End, Ctrl-N/P move, Enter runs, Escape closes.
- Key caps hide on coarse pointers.
- The consumer provides `sections` of commands ({id, label, icon, hint, shortcut, run}).
