A label above its control, a hint below; the label is the `<label>` element so clicking it focuses the input. `Input`, `TextArea` and `Select` share one 36px style: `panel` fill, `line` border, 9px corners, `sh-xs`, 14px text; `line-strong` on hover; a `brand` border with a 3px `brand-ring` on focus. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Labels are 12.5px / 500 in `ink-2`; hints 12px `muted`. Placeholders are `placeholder`, 4.6:1 on `panel`.
- `TextArea` is 5 rows by default and resizes vertically; `Select` draws its own `muted` chevron.
- A colour input keeps the 36px height with 4px padding.
- Disabled controls drop to 60% opacity with a not-allowed cursor.
- The consumer provides the label, the control and, when the rule is not obvious, a hint that says what the value does.
