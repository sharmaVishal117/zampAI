"""Zero-dependency test runner (no pytest required).

    python3 tests/run_tests.py
"""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ap  # noqa: E402

FAILURES = []
PASSED = 0

for name in sorted(n for n in dir(test_ap) if n.startswith("test_")):
    fn = getattr(test_ap, name)
    try:
        fn()
        PASSED += 1
        print(f"  PASS  {name}")
    except Exception:
        FAILURES.append(name)
        print(f"  FAIL  {name}")
        traceback.print_exc()

print(f"\n{PASSED} passed, {len(FAILURES)} failed")
if FAILURES:
    sys.exit(1)
