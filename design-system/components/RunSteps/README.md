The trace of what an agent did: one row per planned step in `panel-2` with a `line` border and 10px corners, a status icon (`good` check, `bad` cross, `muted` dot for skipped, spinning `brand-fg` loader while running, `faint` clock while pending), the step label at 13.5px / 500, and its duration in tabular `muted`. A row opens to show its input and output as mono JSON side by side. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- A running step takes a `brand-line` border; a pending label is `muted`.
- `compact` hides the chevrons and the expandable detail (the Builder's five-step tick list).
- The consumer provides `steps` ({tool, label, status, ms, input, output}); unplanned steps come after the plan.
