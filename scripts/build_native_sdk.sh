#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
  printf 'Usage: %s INSTALL_DIRECTORY\n' "$0" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk_revision=575347ca742d219ef61b1e214e97043f17f48d23
mkdir -p "$1"
install_dir="$(cd "$1" && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf -- "$scratch"' EXIT

git -C "$scratch" init -q
git -C "$scratch" fetch -q --depth 1 https://github.com/orbbec/OrbbecSDK_v2.git "$sdk_revision"
git -C "$scratch" checkout -q --detach FETCH_HEAD
test "$(git -C "$scratch" rev-parse HEAD)" = "$sdk_revision"
git -C "$scratch" apply --check "$script_dir/native-sdk/gemini2-y16-assembly.patch"
git -C "$scratch" apply "$script_dir/native-sdk/gemini2-y16-assembly.patch"

cmake -S "$scratch" -B "$scratch/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DOB_BUILD_EXAMPLES=OFF \
  -DOB_BUILD_DOCS=OFF \
  -DOB_BUILD_TOOLS=OFF \
  -DOB_BUILD_TESTS=OFF \
  -DOB_BUILD_WITH_EXTENSIONS_COMMIT_HASH=OFF \
  -DOB_LIBUVC_TRANSFER_DIAGNOSTIC=ON
cmake --build "$scratch/build" --target OrbbecSDK --parallel "${CMAKE_BUILD_PARALLEL_LEVEL:-4}"
cmake --install "$scratch/build" --prefix "$install_dir"
test -f "$install_dir/lib/libOrbbecSDK.so.2.10.6"
