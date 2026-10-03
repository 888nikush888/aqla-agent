"""Audit installed native-runtime distributions, excluding only its editable source."""
import argparse
import subprocess
from pathlib import Path

METADATA = "import importlib.metadata as m;print('\\n'.join(sorted(set(d.metadata['Name']+'=='+d.version for d in m.distributions() if d.metadata['Name'].lower()!='hermes-agent'))))"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--report", default="reports/hermes-dependencies.json")
    args = parser.parse_args()
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    requirements = report.with_suffix(".requirements.txt")
    result = subprocess.run([args.python, "-c", METADATA], check=True, capture_output=True, text=True)
    if not result.stdout.strip():
        raise SystemExit("Hermes dependency metadata is empty")
    requirements.write_text(result.stdout, encoding="utf-8")
    subprocess.run([args.uv, "tool", "run", "--from", "pip-audit==2.10.1", "pip-audit", "-r", str(requirements),
                    "--disable-pip", "--no-deps", "--strict", "--progress-spinner", "off", "--format", "json", "--output", str(report)], check=True)


if __name__ == "__main__":
    main()
