"""Docker-style ``*_FILE`` secrets convention.

For any environment variable ``X``, if ``X_FILE`` is set and points at a readable
file, that file's stripped contents become the value of ``X``. This lets sensitive
settings (``AZURE_CLIENT_ID``, ``ONENOTE_API_TOKEN``, a seed token cache, ...) arrive
as mounted Docker secrets at ``/run/secrets/<name>`` instead of inline environment
values -- the same pattern used by the official Postgres/MySQL images.

An explicitly-set plain ``X`` always wins over ``X_FILE`` (so you can override a
secret for local dev without editing the compose file).

Call :func:`load_file_backed_env` once, as early as possible at process start,
before anything reads the target variables.
"""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SUFFIX = "_FILE"


def load_file_backed_env(environ: Optional[Dict[str, str]] = None) -> List[str]:
    """Resolve every ``<VAR>_FILE`` into ``<VAR>``.

    Args:
        environ: mapping to mutate; defaults to ``os.environ``. Injectable for tests.

    Returns:
        Sorted list of variable names that were populated from a file.
    """
    env = os.environ if environ is None else environ
    applied: List[str] = []

    for key in list(env.keys()):
        if not key.endswith(_SUFFIX):
            continue
        target = key[: -len(_SUFFIX)]
        if not target:
            continue

        path = (env.get(key) or "").strip()
        if not path:
            continue

        # A plain value that's already set takes precedence over the file.
        if env.get(target):
            logger.debug("Secrets: %s already set; ignoring %s", target, key)
            continue

        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError as exc:
            logger.warning("Secrets: could not read %s from %s: %s", target, path, exc)
            continue

        if not value:
            logger.warning("Secrets: %s is empty; leaving %s unset", path, target)
            continue

        env[target] = value
        applied.append(target)

    if applied:
        logger.info("Secrets: loaded %s from *_FILE", ", ".join(sorted(applied)))
    return sorted(applied)
