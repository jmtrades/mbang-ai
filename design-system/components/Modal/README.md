A dialog on the `overlay` scrim with a 2px blur: `panel`, `line` border, `radius-xl` corners, `sh-xl`; a header row with a `t-h3` title and a 28px close button on a `line-2` rule, then a scrolling body. On phones it becomes a bottom sheet with only the top corners rounded. Shown here in its open state; the original animates in over 220ms. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- 42rem wide, 56rem with `wide` (the source viewer); at most 92vh tall.
- Escape closes; focus moves to the first field and is trapped inside; it returns to the opener on close.
- The consumer provides `title`, `children`, `open` and `onClose`; the primary action lives in the body, right-aligned.
