"""Test package guard.

Each test makes its throwaway Hermes home with ``tempfile.mkdtemp()``. Hermes treats any
``HERMES_HOME`` under the real Hermes root as a profile of the real install, so with a temp dir
inside it (a Hermes session's own scratch dir is ``~/.hermes/cache/scratch``) the tests would
read and write the live install's records. The same holds when the outer ``HERMES_HOME`` is the
real home. Refuse to run in either case.
"""

import os
import pwd
import tempfile
from pathlib import Path


def _real_roots() -> list[Path]:
    name = ".hermes" + os.environ.get("HERMES_DATA_DIR_SUFFIX", "")
    homes = {Path.home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    real = os.environ.get("HERMES_REAL_HOME", "").strip()
    if real:
        homes.add(Path(real).expanduser())
    return [(home / name).resolve() for home in homes]


def _inside(path: Path, roots: list[Path]) -> Path | None:
    path = path.resolve()
    for root in roots:
        if root.exists() and (path == root or root in path.parents):
            return root
    return None


_roots = _real_roots()
_hit = _inside(Path(tempfile.gettempdir()), _roots)
if _hit:
    raise SystemExit(f"refusing to run: the temp dir {tempfile.gettempdir()} is inside the Hermes "
                     f"home {_hit}, so test homes would resolve to the real install. "
                     "Set TMPDIR to a directory outside it.")
_outer = os.environ.get("HERMES_HOME", "").strip()
if _outer and _inside(Path(_outer).expanduser(), _roots):
    raise SystemExit(f"refusing to run: HERMES_HOME {_outer} is the real Hermes home. "
                     'Run with HERMES_HOME="$(mktemp -d)" and TMPDIR outside it.')
