"""打包提交用的 CoreGeek.tar.gz（run.sh 带可执行位，排除测试脚本）。"""
import io
import pathlib
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT.parent.parent / "CoreGeek.tar.gz"

RUN_SH = (
    b"#!/bin/bash\n"
    b"set -euo pipefail\n"
    b'cd "$(dirname "$0")"\n'
    b'exec python3 main3.py "$1"\n'
)

INCLUDE = ["main3.py", "pyproject.toml"]
SKIP_DIRS = {"__pycache__", ".pytest_cache"}


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    tar.addfile(info, io.BytesIO(data))


with tarfile.open(OUT, "w:gz") as tar:
    for name in INCLUDE:
        add_bytes(tar, f"CoreGeek/{name}", (ROOT / name).read_bytes(), 0o644)
    add_bytes(tar, "CoreGeek/run.sh", RUN_SH, 0o755)
    for path in sorted((ROOT / "src").rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        add_bytes(tar, f"CoreGeek/{rel}", path.read_bytes(), 0o644)

with tarfile.open(OUT) as tar:
    for info in tar.getmembers():
        print(f"{oct(info.mode)[2:]:>4}  {info.size:>7}  {info.name}")
print(f"\n-> {OUT}  {OUT.stat().st_size} bytes")
