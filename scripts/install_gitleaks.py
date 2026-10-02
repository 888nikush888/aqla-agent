"""Install a checksum-pinned scanner for CI, outside the application runtime."""
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "8.30.1"
ASSETS = {
    "Linux": ("linux_x64.tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
    "Windows": ("windows_x64.zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
}


def main():
    if platform.machine().lower() not in ("x86_64", "amd64") or platform.system() not in ASSETS:
        raise SystemExit("This CI scanner installer requires Linux or Windows x64")
    asset, expected = ASSETS[platform.system()]
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/gitleaks_{VERSION}_{asset}"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()
    if hashlib.sha256(payload).hexdigest() != expected:
        raise SystemExit("Gitleaks release checksum does not match the reviewed binary")
    name = "gitleaks.exe" if platform.system() == "Windows" else "gitleaks"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            executable = archive.read(name)
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            member = archive.extractfile(name)
            if member is None:
                raise SystemExit("Gitleaks binary is missing from the release archive")
            with member:
                executable = member.read()
    target = Path(".cache/tools") / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(executable)
    os.chmod(target, 0o755)
    print(f"Installed checksum-verified Gitleaks {VERSION}")


if __name__ == "__main__":
    main()
