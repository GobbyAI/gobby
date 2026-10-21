#!/bin/bash
# Host-side simulator capture of the Annotate export list (#23729).
#
# Builds the iOS app and its UI tests, boots a named simulator, seeds three staged ZIPs into the
# App Group's Exports folder, and runs the ExportUITests capture cases. Each case cold-launches
# the app in one text size and appearance, checks the trailing swipe (Delete and Share), Edit mode
# and the long-press menu, runs a contrast accessibility audit, and writes a PNG.
#
# Usage: scripts/capture-board.sh [device-name] [ios-version] [output-dir]
#   defaults: iPhone 17, 26.0. The pair must match an installed simulator; the error lists how to
#   check the installed ones.
#   output-dir: where the PNGs, result.xcresult and build products go. Defaults to
#   build/capture-board at the worktree root, which is gitignored. Pass an absolute path to keep
#   the run out of the worktree (for example a host scratchpad).
#
# Output: <output-dir>/*.png and <output-dir>/result.xcresult. Build products go to
# <output-dir>-derived. Expected runtime: about 10 to 15 minutes warm, longer on a cold build cache.
set -euo pipefail

readonly GROUP_ID="group.ai.gobby.annotate"

# Resolves the UDID of an available simulator from the `simctl list` text. Empty when none matches.
find_udid() {
  local device="$1" os="$2"
  xcrun simctl list devices available | awk -v os="$os" -v dev="$device" '
    /^-- / { section = $0 }
    index(section, "iOS " os) && index($0, "    " dev " (") == 1 {
      match($0, /[0-9A-F]+(-[0-9A-F]+)+/)
      print substr($0, RSTART, RLENGTH)
      exit
    }'
}

# Prints the App Group container directory for GROUP_ID under one simulator, if it exists.
group_container() {
  local udid="$1" candidate metadata
  for candidate in "$HOME/Library/Developer/CoreSimulator/Devices/$udid/data/Containers/Shared/AppGroup"/*/; do
    metadata="${candidate}.com.apple.mobile_container_manager.metadata.plist"
    if [ -f "$metadata" ] &&
      [ "$(/usr/bin/plutil -extract MCMMetadataIdentifier raw "$metadata")" = "$GROUP_ID" ]; then
      printf '%s\n' "${candidate%/}"
      return 0
    fi
  done
  return 1
}

main() {
  local device="${1:-iPhone 17}" os="${2:-26.0}" output="${3:-}"
  local here root project out derived udid app group_dir name status
  here="$(cd "$(dirname "$0")/.." && pwd)"
  root="$(cd "$here/../.." && pwd)"
  project="$here/GobbyAnnotate.xcodeproj"
  out="${output:-$root/build/capture-board}"
  out="${out%/}"
  derived="$out-derived"

  for tool in xcodebuild xcrun trash; do
    command -v "$tool" >/dev/null || {
      echo "capture-board: missing $tool" >&2
      exit 1
    }
  done

  udid="$(find_udid "$device" "$os")"
  if [ -z "$udid" ]; then
    echo "capture-board: no available simulator named '$device' with iOS $os" >&2
    echo "capture-board: list them with: xcrun simctl list devices available" >&2
    exit 1
  fi
  echo "capture-board: device=$device iOS=$os udid=$udid"

  for path in "$out" "$derived"; do
    if [ -e "$path" ]; then trash "$path"; fi
  done
  mkdir -p "$out"

  xcrun simctl boot "$udid" 2>/dev/null || true
  xcrun simctl bootstatus "$udid" -b >/dev/null

  xcodebuild build-for-testing \
    -project "$project" -scheme "GobbyAnnotate (iOS)" \
    -destination "platform=iOS Simulator,id=$udid" \
    -derivedDataPath "$derived" -quiet

  app="$derived/Build/Products/Debug-iphonesimulator/GobbyAnnotate.app"
  xcrun simctl install "$udid" "$app"

  if ! group_dir="$(group_container "$udid")"; then
    echo "capture-board: no App Group container for $GROUP_ID; the app did not install" >&2
    exit 1
  fi
  mkdir -p "$group_dir/Exports"
  local seeded=()
  for _ in 1 2 3; do
    name="gobby-annotate-$(uuidgen).zip"
    printf 'PK\003\004' >"$group_dir/Exports/$name"
    seeded+=("$group_dir/Exports/$name")
  done

  set +e
  TEST_RUNNER_CAPTURE_OUT="$out" xcodebuild test-without-building \
    -project "$project" -scheme "GobbyAnnotate (iOS)" \
    -destination "platform=iOS Simulator,id=$udid" \
    -derivedDataPath "$derived" \
    -only-testing:GobbyAnnotateUITests/ExportUITests \
    -resultBundlePath "$out/result.xcresult" -quiet
  status=$?
  set -e

  trash "${seeded[@]}"
  echo "capture-board: exit=$status device=$device iOS=$os PNGs=$out result=$out/result.xcresult"
  exit "$status"
}

main "$@"
