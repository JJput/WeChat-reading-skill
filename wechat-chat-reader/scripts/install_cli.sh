#!/bin/sh
# Install the pinned Apple Silicon query binary from this Skill or an upstream zip.
set -eu
usage() {
  echo '用法：sh scripts/install_cli.sh [--prefix /目录] [--archive /官方压缩包.zip]'
}
prefix="$HOME/.local"
archive=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --prefix) [ "$#" -ge 2 ] || { usage; exit 2; }; prefix="$2"; shift 2 ;;
    --archive) [ "$#" -ge 2 ] || { usage; exit 2; }; archive="$2"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done
case "$prefix" in /*) ;; *) echo '安装目录请使用绝对路径。' >&2; exit 2 ;; esac
[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || {
 echo '内置 CLI 需要 Apple Silicon Mac，并在原生终端中运行。' >&2; exit 1;
}
macos=$(sw_vers -productVersion)
[ "${macos%%.*}" -ge 14 ] || { echo '需要 macOS 14 或更高版本。' >&2; exit 1; }
assets=$(CDPATH= cd -- "$(dirname -- "$0")/../assets/greenbubbles" && pwd)
bin_dir="$prefix/bin"
mkdir -p "$bin_dir"
stage=$(mktemp -d "$bin_dir/.wechat-chat-reader.XXXXXXXX")
trap 'rm -rf "$stage"' EXIT
trap 'exit 1' HUP INT TERM
binary="$stage/greenbubbles"
digest() { /usr/bin/shasum -a 256 < "$1" | /usr/bin/awk '{print $1}'; }
if [ -n "$archive" ]; then
  [ "$(digest "$archive")" = 3886b441d85c36a087de34956a14071dbf235166ef4d943df949ea7936c99047 ] || {
    echo '官方压缩包校验失败。' >&2; exit 1;
  }
  /usr/bin/unzip -p "$archive" greenbubbles-0.10.0-macos-arm64/bin/greenbubbles > "$binary"
else
  /bin/cp "$assets/greenbubbles" "$binary"
fi
[ "$(digest "$binary")" = 6e5afb134872bc0596dea4f7fb8ac8e10defc20a12bfdaf2c200625a95ce49b0 ] || {
  echo 'CLI 校验失败。' >&2; exit 1;
}
chmod 755 "$binary"
/usr/bin/codesign --verify --strict -R='anchor apple generic and identifier "greenbubbles" and certificate leaf[subject.OU] = "5BR9M56H9W"' "$binary"
"$binary" --version
dest="$bin_dir/greenbubbles"
if [ -e "$dest" ] || [ -L "$dest" ]; then
  if [ -f "$dest" ] && /usr/bin/cmp -s "$binary" "$dest"; then
    echo "已安装 $dest"; exit 0
  fi
  echo "该路径已有不同的 CLI。可用 --prefix 指定另一个目录：$dest" >&2
  exit 1
fi
# Link on the same volume so a concurrent installer cannot overwrite another binary.
/bin/ln "$binary" "$dest"
echo "安装完成 $dest"
