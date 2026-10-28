Six tones on three heights; secondary is the default and the only one you reach for without a reason. One `primary` per view, the action the page exists for ("Start today's mission", "I sent it"); `good` for Approve in the queue, the confirm that lets something go out, beside a secondary Edit and a danger Reject; `danger` stays outlined and fills `bad-soft` only on hover, so a destructive action is never the loudest thing on screen; `ghost` and `subtle` for toolbars and inline actions. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- Sizes: `sm` 28px / 12.5px, `md` 36px / 13.5px, `lg` 44px / 14.5px; `icon` and `icon-sm` are squares for `IconButton`, which requires a `label`.
- `loading` keeps the width, shows a spinner and sets `aria-busy`; the button is disabled meanwhile.
- Icons inside are 14px (sm), 16px (md) or 18px (lg), from lucide, before the label.
- The consumer provides the label (a verb in sentence case) and the handler; never stack two primaries.
- Focus is the shared 2px `focus` outline; pressed state moves the button down 1px.
