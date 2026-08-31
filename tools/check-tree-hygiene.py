#!/usr/bin/env python3
# check-tree-hygiene.py — refuse the four ways a binding repository accumulates a
# state that its CI cannot see. This is the single, runnable definition of those
# rules: the check-tree-hygiene GitHub action calls it, and a developer runs the
# same script over a working tree, so the CI path and the by-hand path cannot
# drift. It reads only `git ls-files`, so it needs nothing built and nothing
# installed.
#
# Each rule answers a state that was found in the tree rather than imagined:
#
#   build-output   a 5.9 MB libmeos.so tracked under a benchmark module, which a
#                  local run loaded while CI loaded the one it had just built.
#                  The tracked copy answered for fewer families than the catalog
#                  the generator reads, so the two runs bound different surfaces.
#   skipped-tests  -DskipTests and -Dmaven.test.skip in build commands, a jar
#                  published from a suite that never ran.
#   stale-pin      a Dockerfile cloning MobilityDB at a release branch and a
#                  personal fork, while the jar in the same image is generated
#                  from the catalog of master.
#   orphan-image   a Dockerfile no script, workflow or compose file names.
#
# A repository states a deliberate exception in tools/tree-hygiene-allow.txt: one
# `<rule> <path-glob>  # why` per line. An exception is a recorded decision, so it
# carries its reason on the same line.
#
# Usage:
#   tools/check-tree-hygiene.py [--root DIR] [--rule RULE]...
#
# Exit status is 0 when every rule passes and 1 when any finding stands.

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

RULES = ("build-output", "skipped-tests", "stale-pin", "orphan-image")

ALLOW_FILE = "tools/tree-hygiene-allow.txt"

# Build output never belongs in a tree: it is the product of the commit, so a
# tracked copy is a second answer to the question the build already answers.
BUILD_OUTPUT_GLOBS = (
    "*.so", "*.so.*", "*.dylib", "*.dll", "*.a", "*.o", "*.obj",
    "*.jar", "*.war", "*.class", "*.pyc",
    "target/*", "*/target/*",
    "dependency-reduced-pom.xml", "*/dependency-reduced-pom.xml",
)

# A suite that does not run cannot witness the surface the jar carries.
SKIP_PATTERNS = (
    (re.compile(r"-DskipTests\b"), "-DskipTests"),
    (re.compile(r"-Dmaven\.test\.skip\b"), "-Dmaven.test.skip"),
    (re.compile(r"<skipTests>"), "<skipTests>"),
    (re.compile(r"<maven\.test\.skip>"), "<maven.test.skip>"),
    (re.compile(r"\bskipITs\b"), "skipITs"),
)

# The chain derives every artifact from one MobilityDB commit on master. A clone
# naming another branch, or another owner, binds a surface the catalog does not
# describe.
ECOSYSTEM_REPOS = ("MobilityDB", "MEOS-API", "JMEOS", "MobilityFlink",
                   "MobilitySpark", "MobilityKafka", "MobilityDuck")
DEFAULT_BRANCHES = ("master", "main")

# `git clone` takes its flags on either side of the URL, so the branch is read
# from the whole command rather than from the text before the URL.
CLONE_RE = re.compile(
    r"git\s+clone\b(?P<args>[^\n]*)"
)
CLONE_URL_RE = re.compile(
    r"https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?(?=[\s\"']|$)"
)
# A ref written in prose carries the surrounding markup with it — a closing
# markdown fence, a quote, a sentence's punctuation — so the capture stops at the
# characters a git ref cannot hold.
BRANCH_RE = re.compile(r"(?:-b|--branch)[=\s]+(?P<ref>[^\s\"'`,;)\]]+)")

# A repository ref in a workflow's `with:` block is the same pin in another form.
WORKFLOW_REF_RE = re.compile(
    r"repository:\s*(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)")

TEXT_SUFFIXES = {
    ".sh", ".bash", ".yml", ".yaml", ".xml", ".md", ".py", ".java", ".sql",
    ".txt", ".cfg", ".toml", ".properties", ".json", ".conf", "",
}


class Finding:
    def __init__(self, rule, path, line, message):
        self.rule = rule
        self.path = path
        self.line = line
        self.message = message

    def __str__(self):
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}\n      {self.message}"


def tracked_files(root):
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=True, capture_output=True, text=True).stdout
    return [p for p in out.split("\0") if p]


def read_allow(root):
    """Return {rule: [glob, ...]} from the repository's recorded exceptions."""
    allow = {rule: [] for rule in RULES}
    path = root / ALLOW_FILE
    if not path.exists():
        return allow
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        rule, glob = parts[0], parts[1].strip()
        if rule in allow:
            allow[rule].append(glob)
    return allow


def allowed(allow, rule, path):
    return any(fnmatch.fnmatch(path, glob) for glob in allow[rule])


def is_text(path):
    return Path(path).suffix.lower() in TEXT_SUFFIXES or Path(path).name.startswith("Dockerfile")


def read_lines(root, path):
    try:
        return (root / path).read_text(errors="replace").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def check_build_output(root, files, allow):
    found = []
    for path in files:
        if allowed(allow, "build-output", path):
            continue
        if any(fnmatch.fnmatch(path, glob) for glob in BUILD_OUTPUT_GLOBS):
            size = ""
            full = root / path
            if full.exists():
                size = f" ({full.stat().st_size} bytes)"
            found.append(Finding(
                "build-output", path, None,
                f"build output is tracked{size}; the commit produces it, so a "
                f"tracked copy is a second answer that goes stale on its own"))
    return found


def check_skipped_tests(root, files, allow):
    found = []
    for path in files:
        if allowed(allow, "skipped-tests", path) or not is_text(path):
            continue
        if path == ALLOW_FILE or path.endswith("check-tree-hygiene.py"):
            continue
        for n, line in enumerate(read_lines(root, path), 1):
            for pattern, name in SKIP_PATTERNS:
                if pattern.search(line):
                    found.append(Finding(
                        "skipped-tests", path, n,
                        f"`{name}` keeps the suite from running; a jar whose "
                        f"tests did not run witnesses no surface"))
    return found


def check_stale_pin(root, files, allow):
    found = []
    for path in files:
        if allowed(allow, "stale-pin", path) or not is_text(path):
            continue
        if path.endswith("check-tree-hygiene.py"):
            continue
        for n, line in enumerate(read_lines(root, path), 1):
            for m in CLONE_RE.finditer(line):
                args = m.group("args")
                url = CLONE_URL_RE.search(args)
                if not url:
                    continue
                repo, owner = url.group("repo"), url.group("owner")
                if repo not in ECOSYSTEM_REPOS:
                    continue
                if owner != "MobilityDB":
                    found.append(Finding(
                        "stale-pin", path, n,
                        f"clones {owner}/{repo}; the chain derives every artifact "
                        f"from MobilityDB/{repo}, so a fork binds a surface the "
                        f"catalog does not describe"))
                    continue
                bm = BRANCH_RE.search(args)
                # A ref written as a variable expansion is parameterised, not
                # pinned: what it resolves to is the caller's to decide, and this
                # file does not hold it.
                if bm and "$" in bm.group("ref"):
                    continue
                if bm and bm.group("ref") not in DEFAULT_BRANCHES:
                    found.append(Finding(
                        "stale-pin", path, n,
                        f"clones MobilityDB/{repo} at `{bm.group('ref')}`; the "
                        f"catalog and the jar come from master, so this library "
                        f"answers for a different surface"))
            for m in WORKFLOW_REF_RE.finditer(line):
                if m.group("repo") in ECOSYSTEM_REPOS and m.group("owner") != "MobilityDB":
                    found.append(Finding(
                        "stale-pin", path, n,
                        f"checks out {m.group('owner')}/{m.group('repo')} rather "
                        f"than MobilityDB/{m.group('repo')}"))
    return found


def check_orphan_image(root, files, allow):
    found = []
    dockerfiles = [p for p in files if Path(p).name.startswith("Dockerfile")]
    if not dockerfiles:
        return found
    haystack = []
    for path in files:
        if is_text(path) and not Path(path).name.startswith("Dockerfile"):
            haystack.append((path, read_lines(root, path)))
    for df in dockerfiles:
        if allowed(allow, "orphan-image", df):
            continue
        name = Path(df).name
        # A plain `Dockerfile` is what `docker build` takes by default, so it
        # needs no reference to be reachable; a named variant does.
        if name == "Dockerfile":
            continue
        refs = sum(1 for _, lines in haystack for line in lines if name in line)
        if refs == 0:
            found.append(Finding(
                "orphan-image", df, None,
                "no tracked script, workflow or compose file names this image, "
                "so nothing builds it and nothing keeps it current"))
    return found


CHECKS = {
    "build-output": check_build_output,
    "skipped-tests": check_skipped_tests,
    "stale-pin": check_stale_pin,
    "orphan-image": check_orphan_image,
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".", help="repository root (default: .)")
    ap.add_argument("--rule", action="append", choices=RULES,
                    help="run only this rule; repeatable (default: all)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    rules = args.rule or list(RULES)
    files = tracked_files(root)
    allow = read_allow(root)

    print(f"tree hygiene: {len(files)} tracked files under {root}")
    if any(allow[r] for r in RULES):
        print(f"recorded exceptions from {ALLOW_FILE}:")
        for rule in RULES:
            for glob in allow[rule]:
                print(f"  - {rule} {glob}")
    print("rules:")

    findings = []
    for rule in rules:
        hits = CHECKS[rule](root, files, allow)
        print(f"  {rule}: {len(hits)}")
        findings.extend(hits)

    if not findings:
        print("tree hygiene: clean")
        return 0

    print("\ntree hygiene: %d finding(s)\n" % len(findings))
    for rule in rules:
        hits = [f for f in findings if f.rule == rule]
        if not hits:
            continue
        print(f"{rule}:")
        for f in hits:
            print(f)
        print()
    print(f"State a deliberate exception in {ALLOW_FILE}, with its reason on "
          f"the same line.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
