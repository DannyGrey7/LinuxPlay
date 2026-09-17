#!/usr/bin/env python3
"""Runner for LinuxPlay's standalone test scripts.

Each test_*.py file is a self-checking script that runs as its own process.
This is the missing piece that ties them together:

    .venv/bin/python test_all.py            # everything
    .venv/bin/python test_all.py parsers    # only files matching a substring

Exit codes reported by a script: 0 = pass, 77 = skipped (the environment
does not have what the test needs), anything else = fail. `test_all.py`
itself exits non-zero if any script failed, so it can gate a commit.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_EXIT = 77
SELF = os.path.basename(__file__)

# Longest first so "test_client" does not shadow a longer name.
def _discover():
    return sorted(f for f in os.listdir(HERE)
                  if f.startswith("test_") and f.endswith(".py") and f != SELF)


def main(argv):
    patterns = [a for a in argv if not a.startswith("-")]
    files = _discover()
    if patterns:
        files = [f for f in files if any(p in f for p in patterns)]
    if not files:
        print("No test files matched.")
        return 1

    results = []
    started = time.time()
    for name in files:
        print(f"\n{'=' * 72}\n== {name}\n{'=' * 72}", flush=True)
        t0 = time.time()
        try:
            rc = subprocess.call([sys.executable, os.path.join(HERE, name)], cwd=HERE)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130
        results.append((name, rc, time.time() - t0))

    passed = [r for r in results if r[1] == 0]
    skipped = [r for r in results if r[1] == SKIP_EXIT]
    failed = [r for r in results if r[1] not in (0, SKIP_EXIT)]

    print(f"\n{'=' * 72}\nSummary ({time.time() - started:.1f}s)\n{'=' * 72}")
    for name, rc, dt in results:
        if rc == 0:
            state = "PASS"
        elif rc == SKIP_EXIT:
            state = "SKIP"
        else:
            state = f"FAIL (exit {rc})"
        print(f"  {state:16s} {name}  ({dt:.1f}s)")
    print(f"\n  {len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
