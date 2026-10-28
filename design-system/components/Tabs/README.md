In-page sections as a row of 13.5px / 500 tabs on a hairline, with one 2px `brand-solid` indicator that slides between them in 300ms. The active tab is `ink`, the others `muted` until hovered. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- `role="tablist"`; arrow keys move between tabs and the active one alone is in the tab order.
- The row scrolls horizontally without a scrollbar when it overflows; 20px margin below.
- The consumer provides `tabs` (key, label), the selected `value` and `onChange`; keep labels to one or two words.
