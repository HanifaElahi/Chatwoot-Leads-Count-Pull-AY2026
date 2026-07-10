#!/usr/bin/env python3
"""Determinism regression test for gender.classify.

Historically, ALL_TOKENS was built from `sorted(set, key=-len)`, whose equal-length
tie order depended on set iteration (PYTHONHASHSEED) — so the same name could
classify as Female on one run and Male on the next. This test re-imports gender.py
under several hash seeds in fresh subprocesses and asserts the results are stable.

Run:
    python3 scripts/test_determinism.py
"""
import os, sys, subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Names that previously flipped between runs (equal-length token tie-breaks).
SAMPLE = ["SabzAlikhan", "Jethanand Arjanani", "Bilal farhah", "Ayesha Khan",
          "Muhammad Ali", "Iqrar", "Sana Ullah", "Anita Nadeem"]

SNIPPET = (
    "import sys; sys.path.insert(0, %r);"
    "from gender import classify;"
    "print('|'.join(classify(n, '') for n in %r))" % (str(REPO), SAMPLE)
)

def run(seed):
    env = dict(os.environ, PYTHONHASHSEED=str(seed))
    out = subprocess.check_output([sys.executable, "-c", SNIPPET], env=env)
    return out.decode().strip()

def main():
    results = {seed: run(seed) for seed in (0, 1, 2, 3, 42, 99, 12345)}
    unique = set(results.values())
    for seed, r in results.items():
        print(f"  seed={seed:<6} {r}")
    if len(unique) != 1:
        print("\nFAIL: classification is NOT deterministic across hash seeds.")
        sys.exit(1)
    print("\nPASS: classification is deterministic across all seeds.")

if __name__ == "__main__":
    main()
