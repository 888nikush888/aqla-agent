"""Build the VPS source bundle from an explicit allowlist; never include secrets."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def build(project: Path, output: Path):
    project, output = project.resolve(), output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("src", "hermes_plugin", "skills", "tests", "docs"):
        files.extend(path for path in (project / name).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".secret", ".session"))
    files.extend(project / name for name in ("README.md", "IMPLEMENTATION_STATUS.md", "pyproject.toml", "uv.lock", "config.example.yaml"))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.is_symlink():
                raise ValueError("Symlinks are not included in the release bundle")
            archive.write(path, "aqla-agent/" + path.relative_to(project).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(digest + "  " + output.name + "\n", encoding="utf-8")
    return {"archive": str(output), "sha256": digest, "files": len(files)}


def main():
    parser = argparse.ArgumentParser(description="Build an aqla VPS source archive")
    parser.add_argument("--output", default="dist/aqla-agent-vps.zip")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    if not (project / "pyproject.toml").is_file():
        project = Path.cwd()
    print(json.dumps(build(project, Path(args.output)), indent=2))


if __name__ == "__main__":
    main()
