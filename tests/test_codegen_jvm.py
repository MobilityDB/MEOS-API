"""Tests for the JVM surface generator this repository owns.

``tools/codegen_jvm.py`` is one generator with three engine arms, shared by
MobilitySpark, MobilityFlink and MobilityKafka rather than vendored by each.
These cover what moving it here can break: the spark arm resolves its
``codegen_spark_udfs.py`` sibling through ``__file__``'s directory, so the two
files are one unit and must travel together, and the engine vocabulary is what
a consumer's build passes on the command line.

Plain unittest, no pytest dependency.
"""
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1] / "tools"
GENERATOR = TOOLS / "codegen_jvm.py"
SPARK_SIBLING = TOOLS / "codegen_spark_udfs.py"

ENGINES = ("spark", "flink", "kafka")


class GeneratorLayoutTests(unittest.TestCase):
    def test_the_generator_is_here(self):
        self.assertTrue(GENERATOR.is_file(), GENERATOR)

    def test_the_spark_arm_finds_its_sibling_beside_it(self):
        # The arm does `Path(__file__).resolve().parent / 'codegen_spark_udfs.py'`,
        # so a move that leaves the sibling behind breaks spark and nothing else
        # — the other two arms never load it.
        self.assertTrue(SPARK_SIBLING.is_file(), SPARK_SIBLING)
        self.assertEqual(SPARK_SIBLING.parent, GENERATOR.parent)

    def test_every_engine_a_consumer_names_is_accepted(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                # No catalog: the run must fail for a MISSING ARGUMENT, never
                # for an unknown engine, which is what argparse rejects first.
                r = subprocess.run(
                    [sys.executable, str(GENERATOR), "--engine", engine],
                    capture_output=True, text=True)
                self.assertNotEqual(r.returncode, 0)
                self.assertNotIn("invalid choice", r.stderr, engine)

    def test_an_engine_no_consumer_names_is_refused(self):
        # The control for the test above: argparse does reject an unknown one.
        r = subprocess.run(
            [sys.executable, str(GENERATOR), "--engine", "nosuchengine"],
            capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid choice", r.stderr)


if __name__ == "__main__":
    unittest.main()
