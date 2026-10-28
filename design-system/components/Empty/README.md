The nothing-here state: a dashed `line-strong` border on a 60% `panel-2` fill, a 44px `panel` disc holding a 20px `muted` icon, a 600 title in `ink`, a 13px `muted` hint capped at 28rem, and an optional action. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Default icon is the inbox; pass a lucide glyph that matches the list (users for students, send for outreach).
- The title states the fact ("Nothing here yet", "No matches for “approvals”"); the hint says why or what to try.
- The consumer provides the words and, when there is one, a single action button.
