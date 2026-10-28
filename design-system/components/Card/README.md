The page is made of cards: `panel` on `bg`, a 1px `line` border, `radius-lg` corners and the faint `sh-xs` under a dark-only top highlight. A header is optional and carries a `t-h3` title, an optional 12.5px `muted` subtitle and right-aligned actions; it is 52px tall when there is no subtitle so action buttons sit on the title's line. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Body padding is 20px (16px on phones); pass `pad={false}` for tables and lists that run edge to edge.
- `hover` lifts to `line-strong` and `sh-md`: only for cards that are themselves a link.
- A grid of cards gives every child `min-width: 0` so a wide table scrolls inside its card, never the page.
- The consumer provides title, subtitle, actions and children; keep one idea per card and write the subtitle as a full sentence about the rule that governs it.
