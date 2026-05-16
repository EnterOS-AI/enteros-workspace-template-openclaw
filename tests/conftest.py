"""Make the template files importable from tests/.

Mirrors the proven molecule-ai-workspace-template-hermes layout: the
repo puts adapter.py at the root rather than under a package dir, so
inject the repo root on sys.path and load adapter.py as a top-level
module straight from its file path — exactly how molecule-runtime and
the canonical validate-workspace-template.py load it (ADAPTER_MODULE=
adapter on a bare file). We deliberately do NOT rely on the repo-root
__init__.py (it uses a package-relative `from .adapter import` that
only resolves inside the runtime's package context).
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Exclude the package-context-only root __init__.py from collection so
# `pytest` works from any rootdir with no extra flags.
collect_ignore = [str(REPO_ROOT / "__init__.py")]

if "adapter" not in sys.modules:
    _ap = REPO_ROOT / "adapter.py"
    _spec = importlib.util.spec_from_file_location("adapter", _ap)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["adapter"] = _mod
        try:
            _spec.loader.exec_module(_mod)
        except Exception:
            del sys.modules["adapter"]
