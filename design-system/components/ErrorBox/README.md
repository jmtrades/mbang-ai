A `role="alert"` strip for a failed request: `bad-soft` fill, `bad-line` border, `bad-ink` text at 13.5px, a 16px warning triangle, and a small secondary Retry button when the caller can retry. Hand-written from `web/src/components/ui.tsx`: a static rendition of the Tailwind original, styled by `bundle.css`.

- 10px corners, 8px vertical margin; it sits where the content would have been.
- Pass the error object or a string; the message is shown as written, so write it for a person ("Couldn't load the queue.").
- Hidden entirely when `error` is null.
