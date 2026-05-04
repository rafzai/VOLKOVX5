"""
conftest.py — pytest configuration for VOLKOVX tests.

Adds the VOLKOVX_V4 root directory to sys.path so test files can do:

    from bregman_projection import ...
    from frank_wolfe_solver import ...
    from engine_v2          import ...

without needing the full `VOLKOVX_V4.` package prefix.  This keeps tests
readable and makes the modules importable both as a package
(`VOLKOVX_V4.bregman_projection`) and directly (`bregman_projection`).
"""
import sys
from pathlib import Path

# tests/ → VOLKOVX_V4/
_V4_ROOT = Path(__file__).resolve().parent.parent

if str(_V4_ROOT) not in sys.path:
    sys.path.insert(0, str(_V4_ROOT))
