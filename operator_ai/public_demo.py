"""Public pages served for the pitch demo workspace (webinar sign-up, client intake, lead-magnet minis, proposals).

Anyone on the internet can sign in to the demo as its owner and read everything stored there, and the demo's public
pages live on this site's own address. So a page served for the demo says plainly that it is a fictional demo, and
its forms take only made-up addresses (ending in @example.invalid, the same rule as demo team invites): a member of
the public who follows a demo link never leaves a real address where every other demo visitor can read it."""
from __future__ import annotations

FICTIONAL = "@example.invalid"
REFUSAL = ("This is the pitch demo, a fictional workspace: use a made-up address ending in @example.invalid. "
           "Anyone trying the demo can read what is typed here.")
BANNER = ('<div role="note" style="background:#ede9fe;color:#4c1d95;padding:10px 16px;font:14px/1.45 system-ui,sans-serif;'
          'text-align:center">Pitch demo (DEMO): a fictional workspace. Anyone trying the demo can read what is entered '
          'here, so use made-up details and an address ending in @example.invalid.</div>')


def email_refused(tenant, email) -> str | None:
    """The refusal message when a demo page is given a real-looking address; None when it may be stored."""
    if not getattr(tenant, "is_demo", False) or not isinstance(email, str) or not email.strip():
        return None
    return None if email.strip().lower().endswith(FICTIONAL) else REFUSAL


def label(tenant, page: str) -> str:
    """The page with the demo notice at the top of its body (unchanged outside the demo)."""
    if not getattr(tenant, "is_demo", False):
        return page
    return page.replace("<body>", "<body>" + BANNER, 1)
