"""Root pytest conftest.

Ensures the worktree's own src/ directory takes priority over the system-wide
editable install so tests always exercise the code in this worktree, not the
code installed from the main checkout.
"""

import sys
from pathlib import Path

# Prepend the worktree src so it shadows the editable install.
_worktree_src = str(Path(__file__).parent / "src")
if _worktree_src not in sys.path:
    sys.path.insert(0, _worktree_src)
