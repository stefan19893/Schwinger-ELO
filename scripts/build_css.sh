#!/usr/bin/env bash
# Rebuild web/css/style.css from tailwind/input.css (developer tool, not part of the
# pipeline: the generated file is committed, so a fresh clone needs only Python).
#
# Uses the Tailwind standalone CLI (a single binary, no Node). Run this after adding
# or removing classes in web/*.html or web/js/*.js; tests/test_web.py fails when a
# class is used that the committed stylesheet does not define.
#
#   ./scripts/build_css.sh                 # downloads the pinned CLI into .cache/ once
#   TAILWIND_BIN=/path/tailwindcss ./scripts/build_css.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="v3.4.17"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  ASSET="tailwindcss-linux-x64";   SHA256="7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4" ;;
  Linux-aarch64) ASSET="tailwindcss-linux-arm64"; SHA256="" ;;
  Darwin-arm64)  ASSET="tailwindcss-macos-arm64"; SHA256="" ;;
  Darwin-x86_64) ASSET="tailwindcss-macos-x64";   SHA256="" ;;
  *) echo "unsupported platform: $(uname -s)-$(uname -m); set TAILWIND_BIN" >&2; exit 1 ;;
esac

BIN="${TAILWIND_BIN:-$REPO_ROOT/.cache/$ASSET-$VERSION}"
if [[ ! -x "$BIN" ]]; then
  mkdir -p "$(dirname "$BIN")"
  URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
  echo "==> downloading $URL"
  curl -fsSL -o "$BIN.tmp" "$URL"
  if [[ -n "$SHA256" ]]; then
    echo "$SHA256  $BIN.tmp" | sha256sum -c -
  else
    echo "    no pinned checksum for $ASSET - compare with sha256sums.txt of the release:" >&2
    (sha256sum "$BIN.tmp" 2>/dev/null || shasum -a 256 "$BIN.tmp") >&2
  fi
  chmod +x "$BIN.tmp"
  mv "$BIN.tmp" "$BIN"
fi

cd "$REPO_ROOT"
"$BIN" -c tailwind/tailwind.config.js -i tailwind/input.css -o web/css/style.css --minify
echo "==> wrote web/css/style.css ($(wc -c < web/css/style.css) bytes)"
