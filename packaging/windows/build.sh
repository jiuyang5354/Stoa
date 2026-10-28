#!/usr/bin/env bash
# 在 Linux 上交叉打包 Windows 绿色版：嵌入式 Python 3.12 + win_amd64 依赖 + mingw 编译的 Stoa.exe
# 依赖：curl unzip zip pip gcc-mingw-w64-x86-64
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../.." && pwd)
OUT=${OUT:-$REPO/dist}; B=$OUT/Stoa; PYV=${PYV:-3.12.10}
rm -rf "$B" && mkdir -p "$B/python" "$B/app"
curl -fsSL -o "$OUT/py.zip" "https://www.python.org/ftp/python/$PYV/python-$PYV-embed-amd64.zip"
unzip -q "$OUT/py.zip" -d "$B/python" && rm "$OUT/py.zip"
printf 'python312.zip\r\n.\r\nLib\r\n..\\app\r\nimport site\r\n' > "$B/python/python312._pth"
pip install -q --target "$B/python/Lib" --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all: -r "$REPO/requirements.txt" tzdata
rm -rf "$B/python/Lib/bin"
git -C "$REPO" archive HEAD | tar -x -C "$B/app"
rm -rf "$B/app/packaging" "$B/app/.github"
cd "$HERE" && x86_64-w64-mingw32-windres res.rc -O coff -o "$OUT/res.o"
x86_64-w64-mingw32-gcc -municode -O2 -s launcher.c "$OUT/res.o" -o "$B/Stoa.exe" && rm "$OUT/res.o"
x86_64-w64-mingw32-windres uninstall.rc -O coff -o "$OUT/u.o"
x86_64-w64-mingw32-gcc -municode -mwindows -O2 -s uninstall.c "$OUT/u.o" -o "$B/卸载 Stoa.exe" -lshell32 && rm "$OUT/u.o"
cp "$HERE/使用说明.txt" "$HERE/启动.bat" "$B/"
(cd "$OUT" && rm -f Stoa-Windows.zip && zip -qr9 Stoa-Windows.zip Stoa)
echo "$OUT/Stoa-Windows.zip"
