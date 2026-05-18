# Fetches the MEOS public headers and PostgreSQL stub headers from the
# MobilityDB GitHub repository, then wires them into the expected layout.

# Usage:
#     python setup.py                   # uses the default branch (master)
#     python setup.py --branch v1.2.0   # pin to a specific tag or branch

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_URL         = "https://github.com/MobilityDB/MobilityDB"
CLONE_DIR        = Path("_mobilitydb")
MEOS_INCLUDE_SRC = CLONE_DIR / "meos" / "include"
POSTGRES_SRC     = CLONE_DIR / "postgres"
MEOS_INCLUDE_DST = Path("meos") / "include"
POSTGRES_LINK    = Path("meos") / "postgres"

CUSTOM_STUBS     = {"pg_config.h", "postgres_int_defs.h", "postgres_ext_defs.in.h"}


def run(cmd: list[str]) -> None:
    print(f"    $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def step_clone(branch: str) -> None:
    print(f"[1/3] Cloning MobilityDB (branch: {branch})...")
    if CLONE_DIR.exists():
        print(f"      {CLONE_DIR}/ already exists, updating...")
        run(["git", "-C", str(CLONE_DIR), "fetch", "--depth=1", "origin", branch])
        run(["git", "-C", str(CLONE_DIR), "reset", "--hard", f"origin/{branch}"])
    else:
        run([
            "git", "clone",
            "--depth", "1",
            "--filter=blob:none",
            "--sparse",
            "--branch", branch,
            REPO_URL,
            str(CLONE_DIR),
        ])
    # `meos/src` is needed by the object-model stage: the error-contract
    # scan and the lattice drift gate read the predicate bodies and
    # MEOS_TEMPTYPE_CATALOG. Applied idempotently so existing clones pick
    # it up on update too.
    run(["git", "-C", str(CLONE_DIR), "sparse-checkout", "set",
         "meos/include", "meos/src", "postgres"])
    print(f"      Done.")


def step_sync_headers() -> None:
    print("[2/3] Syncing MEOS public headers...")
    MEOS_INCLUDE_DST.mkdir(parents=True, exist_ok=True)

    copied = []
    for src in MEOS_INCLUDE_SRC.glob("*.h"):
        if src.name in CUSTOM_STUBS:
            continue
        dst = MEOS_INCLUDE_DST / src.name
        shutil.copy2(src, dst)
        copied.append(src.name)

    if copied:
        for name in copied:
            print(f"      → meos/include/{name}")
    else:
        print("      No headers to copy.")


def step_symlink() -> None:
    print("[3/3] Setting up meos/postgres symlink...")

    if POSTGRES_LINK.is_symlink():
        POSTGRES_LINK.unlink()
    elif POSTGRES_LINK.exists():
        print(f"Error: {POSTGRES_LINK} exists and is not a symlink. Remove it manually.", file=sys.stderr)
        sys.exit(1)

    target = POSTGRES_SRC.resolve()
    if not target.exists():
        print(f"Error: expected postgres headers at {target}", file=sys.stderr)
        sys.exit(1)

    POSTGRES_LINK.symlink_to(target)
    print(f"      → meos/postgres -> {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up MEOS headers from MobilityDB GitHub.")
    parser.add_argument("--branch", default="master", metavar="BRANCH",
                        help="Branch or tag to clone (default: master)")
    args = parser.parse_args()

    print(f"Setting up MEOS IDL Generator...\n")
    step_clone(args.branch)
    step_sync_headers()
    step_symlink()
    print(f"\nAll done. Run `python run.py` to generate output/meos-idl.json.")


if __name__ == "__main__":
    main()
