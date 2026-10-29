"""The trail of a link fix: one log line per step, "fix <message id> +<seconds>: ...".

Every step of fixing a message logs one (found the link, sent the repost, which fixer showed what
preview, what happened to the original, who deleted what), so grepping a message id in the logs gives
the whole story of what BB did with it and why.
"""

from __future__ import annotations

import logging
from typing import Any

from bb.timeutil import now

log = logging.getLogger("bb.fix")


def trail(
    origin: Any, text: str, *args: Any, level: int = logging.INFO, exc_info: Any = None
) -> None:
    """origin: the message the link was posted in (the line then says how long after it was posted), or
    just its id when that's all BB has. exc_info: an exception whose traceback goes with the line."""
    if isinstance(origin, int):
        log.log(level, "fix %s: " + text, origin, *args, exc_info=exc_info)
    else:
        age = now() - origin.created_at.timestamp()
        log.log(level, "fix %s +%.1fs: " + text, origin.id, age, *args, exc_info=exc_info)
