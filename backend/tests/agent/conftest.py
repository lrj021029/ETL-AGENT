import pathlib
import sys

_BACKEND = pathlib.Path(__file__).resolve().parents[2]
_COMPILER_TESTS = _BACKEND / "tests" / "compiler"
for p in (str(_BACKEND), str(_COMPILER_TESTS)):
    if p not in sys.path:
        sys.path.insert(0, p)
