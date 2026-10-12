"""Default copy fragments for The Revenue Inbox.

DEFAULT_UNSUBSCRIBE is the house unsubscribe block. Copy QA appends it when a body
contains no unsubscribe language (configurable via `unsubscribe.mode` in launch.yaml).
"""

from __future__ import annotations

import re
from typing import Optional

DEFAULT_UNSUBSCRIBE = (
    "{{RANDOM | No thanks? Just reply and I will not follow up. | Not interested? Reply "
    "\u201cno thanks.\u201d | Prefer no more emails? Just say \u201cno thanks.\u201d | "
    "Don\u2019t want to hear from me again? Reply \u201cno thanks.\u201d | Rather not receive "
    "another message? Just reply \u201cno thanks.\u201d | If this isn\u2019t relevant, reply "
    "\u201cno thanks.\u201d | If you\u2019d rather not continue, just say \u201cno thanks.\u201d "
    "| Prefer I don\u2019t follow up? Reply \u201cno thanks.\u201d | If you\u2019re not "
    "interested, just say \u201cno thanks.\u201d | If this isn\u2019t relevant to you, reply "
    "\u201cno thanks.\u201d | Don\u2019t want any follow-ups? Just reply \u201cno thanks.\u201d | "
    "If you\u2019d prefer no further contact, say \u201cno thanks.\u201d | Want me to wrap this "
    "up? Just reply \u201cno thanks.\u201d | If this isn\u2019t a fit, reply \u201cno thanks.\u201d "
    "| Not relevant? Just say \u201cno thanks.\u201d | Rather not hear from me again? Reply "
    "\u201cno thanks.\u201d | If you\u2019d prefer no more emails, just reply \u201cno "
    "thanks.\u201d | No need to follow up? Just say \u201cno thanks.\u201d | Want me to close "
    "this out? Reply \u201cno thanks.\u201d | If this missed the mark, reply \u201cno thanks.\u201d "
    "| If you\u2019d rather pass, just say \u201cno thanks.\u201d | If this isn\u2019t something "
    "you need, reply \u201cno thanks.\u201d | If you\u2019d rather end the conversation, just say "
    "\u201cno thanks.\u201d | If you\u2019d prefer no further emails, reply \u201cno thanks.\u201d | "
    "If this isn\u2019t of interest, just reply \u201cno thanks.\u201d | If you\u2019re not the "
    "right person, reply \u201cno thanks.\u201d | If you don\u2019t want another email, just say "
    "\u201cno thanks.\u201d | If I shouldn\u2019t follow up, reply \u201cno thanks.\u201d | If "
    "you\u2019d rather not receive follow-ups, say \u201cno thanks.\u201d | If you\u2019d like to "
    "opt out, just say \u201cno thanks.\u201d | If you\u2019d like no more messages, reply "
    "\u201cno thanks.\u201d | If you\u2019d rather not receive anything further, say \u201cno "
    "thanks.\u201d | If you\u2019d rather not hear back from me, reply \u201cno thanks.\u201d | If "
    "this isn\u2019t worth pursuing, reply \u201cno thanks.\u201d | If you\u2019re not interested, "
    "\u201cno thanks\u201d works. | If you\u2019d rather end this, \u201cno thanks\u201d is enough. "
    "| If this isn\u2019t a match, just reply \u201cno thanks.\u201d | If you\u2019d rather skip "
    "this, reply \u201cno thanks.\u201d | If this isn\u2019t useful, just say \u201cno thanks.\u201d "
    "| If you\u2019d rather not continue, reply \u201cno thanks.\u201d | If you\u2019re set, just say "
    "\u201cno thanks.\u201d | If this isn\u2019t on your radar, reply \u201cno thanks.\u201d | If "
    "you don\u2019t want me to follow up, say \u201cno thanks.\u201d | If you\u2019d prefer I "
    "don\u2019t email again, reply \u201cno thanks.\u201d | If you\u2019re not looking at this, just "
    "say \u201cno thanks.\u201d | If you\u2019d rather end it there, reply \u201cno thanks.\u201d | "
    "If you want no further emails, just reply \u201cno thanks.\u201d}}"
)

UNSUBSCRIBE_RE = re.compile(
    r"(?i)(no thanks|unsubscribe|opt[\s\-]?out|take you off|remove you from|"
    r"no longer receive|no further contact|stop reaching out|end our correspondence)"
)


def has_unsubscribe(text: str) -> bool:
    return bool(UNSUBSCRIBE_RE.search(text or ""))


def ensure_unsubscribe(
    body: str, *, mode: str = "append", template: Optional[str] = None
) -> tuple[str, bool]:
    """Return (body, changed). mode: append | require | off (require checked by QA)."""
    if mode == "off" or has_unsubscribe(body):
        return body, False
    if mode != "append":
        return body, False
    tpl = (template or DEFAULT_UNSUBSCRIBE).strip()
    if not body.strip():
        return f"<div>{tpl}</div>", True
    return f"{body}<div><br /></div><div>{tpl}</div>", True
