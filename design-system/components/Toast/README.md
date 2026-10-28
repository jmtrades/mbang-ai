A transient status line: `panel` on a `line` border with `sh-lg`, 12px corners, 13.5px `ink` text, a 16px icon in `good`, `bad` or `brand-fg` for info, and a dismiss cross. Toasts stack bottom-right on desktop and above the tab bar on phones. Hand-written from `App.tsx`.

- Entry is 220ms (fade, 14px lift, 0.97 scale); exit 150ms.
- The message is a completed fact in sentence case: "Approved and sent", "Marked as sent", "Couldn't change that. Try again."
- `role="status"`; the consumer provides the text, the tone and `onDismiss`.
