"""Minimal .env loader.

We avoid the python-dotenv dependency: the format we care about is
`KEY=VALUE` with optional quoting and `#` comments. ViMax's own `.env` (in
the fork) follows this shape exactly.

Why we need this: launchd plists shouldn't store secrets in plain text.
Instead the agent boots and reads `$VIMAX_HOME/.env` at startup. The
.env file is already gitignored on the ViMax side.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Return {KEY: VALUE} from a dotenv-style file. Ignores blank/comment lines."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Drop trailing inline comment that's NOT inside quotes.
        if value and value[0] not in ("'", '"'):
            if "#" in value:
                value = value.split("#", 1)[0].rstrip()
        # Strip a single pair of matching quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env_file(path: Path, *, override: bool = False) -> dict[str, str]:
    """Parse `path` and inject into os.environ. Returns the parsed dict.

    By default, existing env vars take precedence (matches dotenv-cli default).
    """
    parsed = parse_env_file(path)
    for k, v in parsed.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return parsed


def maybe_load_vimax_env() -> Path | None:
    """Load $VIMAX_HOME/.env if present. Returns the path that was loaded, or None."""
    vimax_home = Path(os.environ.get("VIMAX_HOME", os.path.expanduser("~/projects/ViMax")))
    candidate = vimax_home / ".env"
    if candidate.is_file():
        load_env_file(candidate, override=False)
        return candidate
    return None
