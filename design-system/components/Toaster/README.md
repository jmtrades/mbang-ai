The stack that holds toasts: newest at the bottom, 8px apart, right-aligned at the bottom-right of the desktop viewport and centred above the phone tab bar. Each toast enters in 220ms and leaves in 150ms; a toast dismisses itself after a few seconds or on its cross. Hand-written from `App.tsx`.

- Pointer events pass through the stack, so only the toasts themselves can be clicked.
- One toast per completed action; never a toast for something the page already shows, and never for an error that has an ErrorBox.
- The consumer provides `toasts` ({id, msg, tone}) and `onDismiss`.
