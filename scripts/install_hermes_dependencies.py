"""Install exactly the separate Hermes dependency recipe exercised by CI."""
import argparse
import shutil
import subprocess
from pathlib import Path

from aqla_agent.install import hermes_dependency_command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--python", required=True)
    args = parser.parse_args()
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv is required")
    overrides = Path(__file__).resolve().parents[1] / "hermes-runtime-overrides.txt"
    subprocess.run(hermes_dependency_command(uv, Path(args.source).absolute(), Path(args.python).absolute(), overrides), check=True)


if __name__ == "__main__":
    main()
