#!/usr/bin/env python3
"""Tighten the architecture ratchet baselines in tests/arch_baselines/.

  update_arch_baselines.py              lower every baseline to what the code measures now; drop fixed entries
  update_arch_baselines.py --accept-new  also record NEW violations (only when creating a baseline; say why in the PR)

Without --accept-new the script never raises a value or adds a key, so it cannot be used to launder a regression.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import arch_metrics as am  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept-new", action="store_true")
    ap.add_argument("metrics", nargs="*", help=f"subset of: {', '.join(am.METRICS)}")
    a = ap.parse_args(argv)
    names = a.metrics or list(am.METRICS)
    refused = 0
    for name in names:
        current = am.METRICS[name]()
        old = am.load_baseline(name)
        if a.accept_new or not (am.BASELINES / f"{name}.json").exists():
            new = current
        else:
            new = {k: min(v, old[k]) for k, v in current.items() if k in old}
            worse = [k for k, v in current.items() if v > old.get(k, 0)]
            if worse:
                refused += len(worse)
                print(f"{name}: {len(worse)} regressions not recorded (fix them, or use --accept-new): {worse[:5]}")
        am.save_baseline(name, new)
        print(f"{name}: {sum(old.values())} -> {sum(new.values())} across {len(new)} entries")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
