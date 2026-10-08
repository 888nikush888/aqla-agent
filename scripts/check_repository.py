"""Reject committed local credentials, installation state and QA outputs."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aqla_agent.artifact_rules import forbidden  # noqa: E402


def main():
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    files = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    bad = [name for name in files if forbidden(name)]
    if bad:
        raise SystemExit("Local state or credential paths are tracked: " + ", ".join(bad))
    print(f"Repository hygiene passed ({len(files)} tracked files)")


if __name__ == "__main__":
    main()
