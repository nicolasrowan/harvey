"""Canonical filesystem locations for the Harvey checkout.

Harvey is repo-centric: config (harvey.yaml, .env), knowledge (prompts/,
skills/), and state (data/) all live next to the package. ``resolve()``
matters — when the package is reached through a symlink (some editable
installs), a bare ``Path(__file__).parent`` would point into
site-packages and Harvey would silently read/write the wrong files.

Profiles let one checkout sell several products. With ``HARVEY_PROFILE=cargo``
(or ``harvey --profile cargo ...``) everything product-specific moves into
``profiles/cargo/``: its harvey.yaml, its data/ (database, tokens, logs) and
any skills/ or prompts/ file that should differ from the shared ones.
Knowledge every profile shares goes in ``profiles/_shared/``. The ``.env``
at the project root is shared too; a profile's own ``.env`` overrides it.
No profile set means the classic single-product layout, unchanged.

The profile is read once, at import, so it must be set before any harvey
module is imported (the CLI does this before dispatching).
"""

import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROFILE_ENV = "HARVEY_PROFILE"
SHARED_PROFILE = "_shared"
# Lowercase slug: safe as a directory name and impossible to escape
# PROFILES_DIR with ("..", slashes and drive letters never match).
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class ProfileError(ValueError):
    """The requested profile name is invalid or the profile doesn't exist."""


def validate_profile_name(name: str) -> str:
    name = (name or "").strip()
    if not PROFILE_NAME_RE.match(name):
        raise ProfileError(
            f"Invalid profile name {name!r}: use lowercase letters, digits, "
            "'-' or '_' (e.g. 'cargo', 'rowan-erp')."
        )
    return name


PROFILES_DIR = Path(
    os.environ.get("HARVEY_PROFILES_DIR") or PROJECT_ROOT / "profiles"
).resolve()
SHARED_DIR = PROFILES_DIR / SHARED_PROFILE

_raw_profile = os.environ.get(PROFILE_ENV, "").strip()
PROFILE = validate_profile_name(_raw_profile) if _raw_profile else ""

# Where this run's product lives: its config, data and knowledge overrides.
PROFILE_DIR = PROFILES_DIR / PROFILE if PROFILE else PROJECT_ROOT
DATA_DIR = PROFILE_DIR / "data"


def list_profiles() -> list[str]:
    """Profile names under PROFILES_DIR (directories with a harvey.yaml)."""
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        p.name for p in PROFILES_DIR.iterdir()
        if p.is_dir()
        and PROFILE_NAME_RE.match(p.name)
        and ((p / "harvey.yaml").exists() or (p / "harvey.local.yaml").exists())
    )


def knowledge_dirs(kind: str) -> list[Path]:
    """Directories to search for a ``skills`` or ``prompts`` file.

    Most specific first: the active profile, then the shared profile
    knowledge, then the files that ship with Harvey.
    """
    dirs = []
    if PROFILE:
        dirs += [PROFILE_DIR / kind, SHARED_DIR / kind]
    dirs.append(PROJECT_ROOT / kind)
    return dirs


def find_knowledge_file(kind: str, name: str) -> Path | None:
    """The first ``<kind>/<name>.md`` along knowledge_dirs(), or None."""
    for directory in knowledge_dirs(kind):
        candidate = directory / f"{name}.md"
        if candidate.is_file():
            return candidate
    return None
