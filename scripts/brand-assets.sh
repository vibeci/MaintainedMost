#!/usr/bin/env bash
#
# Mattermore's brand assets.
#
#   ./scripts/brand-assets.sh render          regenerate brand/images from brand/*.svg
#   ./scripts/brand-assets.sh apply <path>    copy them into a Mattermost checkout
#
# The rendered files are committed, so a build needs no image tooling. Only
# 'render' does, and only when the artwork changes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/brand"
OUT="$SRC/images"

# Upstream's canvases are kept as they are, because stylesheets size against
# them. The art is fitted inside and centred, so nothing is stretched.
render() {
    command -v inkscape >/dev/null || { echo "needs inkscape" >&2; exit 1; }
    command -v magick   >/dev/null || { echo "needs ImageMagick" >&2; exit 1; }

    local tmp; tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' RETURN
    mkdir -p "$OUT/favicon"

    local svg
    for svg in dark white gray; do
        inkscape "$SRC/wordmark-$svg.svg" --export-type=png \
            --export-filename="$tmp/$svg.png" --export-width=1880 \
            --export-background-opacity=0 >/dev/null 2>&1
    done

    fit() {  # source, WxH, destination
        magick "$1" -trim +repage -resize "$2" \
            -background none -gravity center -extent "$2" "$3"
    }

    fit "$tmp/dark.png"  465x91 "$OUT/logo.png"
    fit "$tmp/white.png" 219x36 "$OUT/logoWhite.png"
    fit "$tmp/dark.png"  288x48 "$OUT/logo-email.png"
    fit "$tmp/dark.png"  264x44 "$OUT/logo_email_dark.png"
    fit "$tmp/dark.png"  264x44 "$OUT/logo_email_blue.png"
    fit "$tmp/gray.png"  132x22 "$OUT/logo_email_gray.png"
    cp "$SRC/wordmark-dark.svg" "$OUT/logo.svg"

    icon() {  # source svg, destination, size
        inkscape "$1" --export-type=png --export-filename="$2" \
            --export-width="$3" --export-background-opacity=0 >/dev/null 2>&1
    }

    # Three states: the plain mark, a mention dot, an unread dot.
    local size
    for size in 16 24 32 64 96; do
        icon "$SRC/mark.svg"          "$OUT/favicon/favicon-default-${size}x${size}.png"  "$size"
        icon "$SRC/mark-mentions.svg" "$OUT/favicon/favicon-mentions-${size}x${size}.png" "$size"
        icon "$SRC/mark-unread.svg"   "$OUT/favicon/favicon-unread-${size}x${size}.png"   "$size"
    done
    for size in 16 32 96; do
        icon "$SRC/mark.svg" "$OUT/favicon/favicon-${size}x${size}.png" "$size"
    done
    for size in 57 60 72 76 120 144 152; do
        icon "$SRC/mark.svg" "$OUT/favicon/apple-touch-icon-${size}x${size}.png" "$size"
    done
    icon "$SRC/mark.svg" "$OUT/favicon/android-chrome-192x192.png" 192

    echo "==> rendered into $OUT"
}

apply() {
    local dest="${1:?usage: brand-assets.sh apply <mattermost checkout>}"
    local images="$dest/webapp/channels/src/images"
    [ -d "$images/favicon" ] || { echo "not a mattermost checkout: $dest" >&2; exit 1; }
    cp "$OUT"/*.png "$OUT"/logo.svg "$images/"
    cp "$OUT"/favicon/*.png "$images/favicon/"
    echo "==> brand assets applied to $dest"
}

case "${1:-}" in
    render) render ;;
    apply)  shift; apply "$@" ;;
    *) echo "usage: brand-assets.sh render | apply <mattermost checkout>" >&2; exit 1 ;;
esac
