#!/bin/sh
# Build the macOS process-tap binary into tap/build/tap. Needs the Xcode command line tools and macOS 14.4+.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root/tap"
swift build -c release \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$root/tap/Info.plist"
mkdir -p build
cp "$(swift build -c release --show-bin-path)/tap" build/tap
codesign --force --sign - --identifier meeting-copilot.tap build/tap
echo "$root/tap/build/tap"
