"""The rules check-test-outcome.py enforces, one case per dialect it reads.

The script is what the check-test-outcome action runs and what a developer runs
over a local build log, so the dialects it recognises and the two verdicts it
reaches are the contract every consumer depends on.
"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_test_outcome", ROOT / "tools" / "check-test-outcome.py")
outcome = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(outcome)


class DialectTests(unittest.TestCase):
    """Each dialect's own summary, read for the total and the skips."""

    def test_surefire_sums_every_module(self):
        log = (
            "Tests run: 7, Failures: 0, Errors: 0, Skipped: 1\n"
            "Tests run: 4, Failures: 0, Errors: 0, Skipped: 0\n"
        )
        total, skipped, dialect, lines = outcome.read_summaries(log)
        self.assertEqual(("surefire", 11, 1), (dialect, total, skipped))
        self.assertEqual(2, len(lines))

    def test_surefire_ignores_the_per_class_line(self):
        # A per-CLASS line carries a trailing `-- in <class>`; counting it too
        # would count every test twice.
        log = (
            "Tests run: 7, Failures: 0, Errors: 0, Skipped: 0 -- in org.meos.T\n"
            "Tests run: 7, Failures: 0, Errors: 0, Skipped: 0\n"
        )
        total, _, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("surefire", 7), (dialect, total))

    def test_pytest_reads_passed_and_skipped(self):
        total, skipped, dialect, _ = outcome.read_summaries(
            "257 passed, 11 skipped in 13.58s\n")
        self.assertEqual(("pytest", 257, 11), (dialect, total, skipped))

    def test_go_counts_one_result_line_per_test(self):
        log = (
            "--- PASS: TestOne (0.01s)\n"
            "    --- SKIP: TestOne/sub (0.00s)\n"
            "--- FAIL: TestTwo (0.02s)\n"
        )
        total, skipped, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("go", 3, 1), (dialect, total, skipped))

    def test_vstest_reads_the_total_and_the_skips(self):
        log = ("Passed!  - Failed:     0, Passed:    38, Skipped:     0, "
               "Total:    38, Duration: 182 ms - MEOS.NET.Tests.dll (net8.0)\n")
        total, skipped, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("vstest", 38, 0), (dialect, total, skipped))

    def test_vstest_reads_a_failed_run_too(self):
        log = ("Failed!  - Failed:     1, Passed:    37, Skipped:     2, "
               "Total:    40, Duration: 190 ms - MEOS.NET.Tests.dll (net8.0)\n")
        total, skipped, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("vstest", 40, 2), (dialect, total, skipped))

    def test_vstest_sums_every_assembly(self):
        log = (
            "Passed!  - Failed: 0, Passed: 3, Skipped: 0, Total: 3, "
            "Duration: 1 ms - A.dll (net8.0)\n"
            "Passed!  - Failed: 0, Passed: 5, Skipped: 1, Total: 6, "
            "Duration: 2 ms - B.dll (net8.0)\n"
        )
        total, skipped, dialect, lines = outcome.read_summaries(log)
        self.assertEqual(("vstest", 9, 1), (dialect, total, skipped))
        self.assertEqual(2, len(lines))


class Catch2Tests(unittest.TestCase):
    """The Catch2 console reporter, which DuckDB's test runner writes."""

    def test_catch2_reads_the_line_a_mobilityduck_run_writes(self):
        # Verbatim from a MobilityDuck CI run of its 102 sqllogictest files.
        total, skipped, dialect, _ = outcome.read_summaries(
            "All tests passed (2695 assertions in 102 test cases)\n")
        self.assertEqual(("catch2", 102, 0), (dialect, total, skipped))

    def test_catch2_adds_the_skipped_cases_the_passing_count_excludes(self):
        # The reporter prints `testCases.passed - skippedTests`, so 99 printed
        # beside 3 skipped is a suite of 102.
        total, skipped, dialect, _ = outcome.read_summaries(
            "All tests passed (3 skipped tests, 2695 assertions in "
            "99 test cases)\n")
        self.assertEqual(("catch2", 102, 3), (dialect, total, skipped))

    def test_catch2_reads_the_singular_forms(self):
        total, skipped, dialect, _ = outcome.read_summaries(
            "All tests passed (1 skipped test, 1 assertion in 1 test case)\n")
        self.assertEqual(("catch2", 2, 1), (dialect, total, skipped))

    def test_catch2_reads_the_failure_table(self):
        log = (
            "test cases: 102 | 101 passed | 1 failed\n"
            "assertions: 2695 | 2694 passed | 1 failed\n"
        )
        total, skipped, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("catch2", 102, 0), (dialect, total, skipped))

    def test_catch2_reads_the_skipped_column_when_the_table_carries_one(self):
        # A column whose count is zero is omitted, so the skipped column is
        # present only when it is not zero, and the leading total includes it.
        log = "test cases: 102 | 99 passed | 1 failed | 2 skipped\n"
        total, skipped, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("catch2", 102, 2), (dialect, total, skipped))

    def test_catch2_reads_a_wholly_skipped_run(self):
        total, skipped, dialect, _ = outcome.read_summaries(
            "All tests were skipped (total skipped 7)\n")
        self.assertEqual(("catch2", 7, 7), (dialect, total, skipped))

    def test_catch2_is_read_before_pytest(self):
        # THE ORDER IS THE TEST: `101 passed` in the failure table satisfies the
        # pytest pattern too, and pytest-first would report 101 as the total,
        # missing the failure and any skip beside it.
        log = "test cases: 102 | 101 passed | 1 failed\n"
        total, _, dialect, _ = outcome.read_summaries(log)
        self.assertEqual(("catch2", 102), (dialect, total))


class NoSummaryTests(unittest.TestCase):
    """A log carrying no summary at all names no dialect, which is a failure."""

    def test_an_empty_log_names_no_dialect(self):
        self.assertEqual((0, 0, None, []), outcome.read_summaries(""))

    def test_a_vstest_run_over_an_empty_assembly_names_no_dialect(self):
        # `dotnet test` over an assembly holding no test writes this and exits
        # 0, so the summary it does NOT write is the only thing to read.
        log = ("No test is available in /src/MEOS.NET.Tests.dll. Make sure that "
               "test discoverer & executors are registered.\n")
        self.assertIsNone(outcome.read_summaries(log)[2])


if __name__ == "__main__":
    unittest.main()
