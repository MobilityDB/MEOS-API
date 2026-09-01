#!/usr/bin/env python3
# check-test-outcome.py — read a build log's own test summary and refuse the two
# ways a suite stops covering the code while the job still reports success.
#
# This is the single, runnable definition of both rules: the check-test-outcome
# GitHub action calls it, and a developer runs it over a local build log, so the
# CI answer and the by-hand answer cannot differ.
#
#   SKIPPED  A skipped test asserts nothing and is indistinguishable from a
#            passing one in the job's conclusion. Every construct that reaches
#            it is reported in the summary: a JUnit @Disabled, an unmet
#            @EnabledIf*, a failed assumeTrue, a Python skipUnless/skipTest.
#            MEASURED: MobilityFlink's binding module under
#            `-Dmeos.enabled=false` reports `Tests run: 7 ... Skipped: 7` and
#            `BUILD SUCCESS` — the whole MEOS surface disabled by one flag, with
#            a green build over it.
#
#   SHRANK   A test the build never collects is reported NOWHERE, so the skip
#            rule above is blind to it: a surefire <excludes>, a `-Dtest=`
#            filter, a class renamed out of the `*Test` convention, or a file
#            simply deleted. What moves is the TOTAL, so the total carries a
#            floor that may rise and may not fall. Once skipping is refused,
#            deleting a test is the remaining way to stop running it, and this
#            is what stands in that path.
#
# Both summary dialects are read, and every module's line is summed rather than
# the last one taken — a multi-module build prints one summary per module, and
# reading only the last understates the total (measured: MobilityKafka prints 7
# and 4, so its total is 11 rather than the 4 a tail would report).
#
# Usage:
#   tools/check-test-outcome.py <build.log> [--min-tests N] [--allow-skips]
#
# Exit status is 0 when the log satisfies both rules and 1 otherwise.

import argparse
import re
import sys
from pathlib import Path

# `Tests run: 12, Failures: 0, Errors: 0, Skipped: 0` — surefire prints this per
# CLASS (with a trailing `-- in <class>`) and once per MODULE without it. Only
# the module lines are summed, or every test counts twice.
SUREFIRE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
    r"\s*Skipped:\s*(\d+)\s*$")

# `268 passed in 12.19s` / `257 passed, 11 skipped in 13.58s` — pytest's summary.
PYTEST = re.compile(r"(?:^|\s)(\d+)\s+passed(?:,\s*(\d+)\s+skipped)?")


def read_summaries(text: str):
    """Return (total, skipped, dialect, lines) summed over every summary found."""
    total = skipped = 0
    lines = []
    for raw in text.splitlines():
        line = raw.rstrip()
        m = SUREFIRE.search(line)
        if m:
            total += int(m.group(1))
            skipped += int(m.group(4))
            lines.append(line.strip())
    if lines:
        return total, skipped, "surefire", lines

    for raw in text.splitlines():
        m = PYTEST.search(raw)
        if m:
            total += int(m.group(1))
            skipped += int(m.group(2) or 0)
            lines.append(raw.strip())
    if lines:
        return total, skipped, "pytest", lines

    return 0, 0, None, []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("log", help="build log carrying the test summary")
    ap.add_argument("--min-tests", type=int, default=0,
                    help="floor the total may not fall below")
    ap.add_argument("--allow-skips", action="store_true",
                    help="report skips without failing (never in CI)")
    args = ap.parse_args()

    path = Path(args.log)
    if not path.exists():
        print(f"check-test-outcome: no log at {path}", file=sys.stderr)
        return 1
    text = path.read_text(errors="replace")

    total, skipped, dialect, lines = read_summaries(text)

    # An extraction that finds nothing is a statement about the parser until it
    # shows it found its input, so a log with no summary at all is a failure
    # rather than a silent pass.
    if dialect is None:
        print(f"check-test-outcome: {path} ({len(text)} bytes) carries no test "
              f"summary in either dialect — the suite did not run, or the log "
              f"is not the one the run wrote", file=sys.stderr)
        return 1

    print(f"check-test-outcome: {dialect}, {len(lines)} summary line(s)")
    for line in lines:
        print(f"  {line}")
    print(f"  total={total} skipped={skipped} floor={args.min_tests}")

    failed = False

    if skipped and not args.allow_skips:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("SKIPPED") or " SKIPPED " in s:
                print(f"  {s}")
        print(f"::error::{skipped} test(s) skipped. A skipped test asserts "
              f"nothing and reports as success. Supply the precondition its "
              f"guard reads, or remove the guard.")
        failed = True

    if total < args.min_tests:
        print(f"::error::the suite ran {total} tests against a floor of "
              f"{args.min_tests}. Tests that are not collected are reported "
              f"nowhere, so a total that falls is how coverage leaves without "
              f"a skip. Restore them, or lower the floor deliberately in the "
              f"same commit that removes them.")
        failed = True

    if failed:
        return 1
    print("check-test-outcome: nothing skipped, and the suite did not shrink")
    return 0


if __name__ == "__main__":
    sys.exit(main())
