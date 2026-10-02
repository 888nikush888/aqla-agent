"""Reject committed local credentials, installation state and QA outputs."""
import subprocess
from pathlib import PurePosixPath

FORBIDDEN_PARTS = {".cache", ".venv", "__pycache__", "secrets", "hermes-home", "hermes-source", "hermes-venv", ".python", "reports", "dist"}
FORBIDDEN_NAMES = {"vps.env", "config.yaml", "state.json", ".coverage"}


def forbidden(name):
    path = PurePosixPath(name)
    return bool(FORBIDDEN_PARTS.intersection(path.parts) or path.name in FORBIDDEN_NAMES
                or path.suffix in (".secret", ".session", ".pyc", ".db", ".sqlite", ".sqlite3")
                or path.name.startswith(".env") and path.name != ".env.example")


def main():
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    files = [name for name in result.stdout.decode("utf-8").split("\0") if name]
    bad = [name for name in files if forbidden(name)]
    if bad:
        raise SystemExit("Local state or credential paths are tracked: " + ", ".join(bad))
    print(f"Repository hygiene passed ({len(files)} tracked files)")


if __name__ == "__main__":
    main()
