A radio group drawn as a 10px-rounded track in `panel-2` with 32px options; the chosen one lifts onto `panel` with `sh-sm` and a `line` ring. For two to four exclusive choices that change a view, such as the theme switch (System / Light / Dark). Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Options are 13px / 500, `muted` until chosen or hovered, with an optional 14px icon.
- `role="radiogroup"` with a required `label`; each option is `role="radio"` with `aria-checked`.
- The consumer provides `options` (value, label, icon), `value` and `onChange`.
