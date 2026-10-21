"""Create a Registry v1 ZIP with a pinned SHA-256 checksum."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_ID = "webvpn_browser"
SEMVER = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
FILES = ("__init__.py", "plugin.py", "plugin.json", "history.py",
         "browser.py", "routes.py", "gateway_status.py", "README.md")
DIRECTORIES = ("templates", "static")


def validate_manifest(data: dict, version: str) -> None:
    if data["id"] != PLUGIN_ID or data["version"] != version:
        raise ValueError("插件 ID 或版本与发行包不符")
    if (data["api_version"] != 1 or data["execution"] != "in_process"
            or data["entry"] != "plugin.py" or data["requires"] != []):
        raise ValueError("插件必须为可信任的 Plugin API v1 in_process")
    if (data["rpc_api_version"] is not None or data["route_prefix"] is not None
            or data["rpc_permissions"] != []):
        raise ValueError("共享 WebVPN 会话不允许通过进程 RPC 传输")


def build(version: str, root: Path) -> tuple[Path, Path]:
    if not SEMVER.fullmatch(version):
        raise ValueError(f"无效的 SemVer: {version}")
    data = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    if version.split("-", 1)[0] != data["version"].split("-", 1)[0]:
        raise ValueError("插件清单的基本版本与发行包不符")
    name = f"{PLUGIN_ID}-{version}.zip"
    artifact, checksum = root / name, root / f"{name}.sha256"
    with tempfile.TemporaryDirectory(prefix="f-browser-registry-") as temporary:
        destination = Path(temporary)
        for relative in FILES:
            source = root / relative
            if not source.is_file():
                raise FileNotFoundError(relative)
            shutil.copyfile(source, destination / relative)
        for relative in DIRECTORIES:
            shutil.copytree(root / relative, destination / relative)
        data["version"] = version
        validate_manifest(data, version)
        (destination / "plugin.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(artifact, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for source in sorted(destination.rglob("*")):
                if source.is_file():
                    z.write(source, source.relative_to(destination).as_posix())
    with zipfile.ZipFile(artifact) as z:
        validate_manifest(json.loads(z.read("plugin.json")), version)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {name}\n", encoding="utf-8")
    return artifact, checksum


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    args = parser.parse_args()
    archive, checksum = build(args.version, Path(__file__).resolve().parents[1])
    print(f"{archive.name}\n{checksum.name}")


if __name__ == "__main__":
    main()
