#!/usr/bin/env bash
# Assemble the browser version into browser/dist:
#   index.html   the page
#   worker.js    Ghostscript's loader + pdf-lib + worker-main.js in one file
#   gs-gzip.wasm Ghostscript compiled to WebAssembly, gzipped to stay under
#                the 15 MB per-file limit for Claude-hosted pages (named
#                .wasm because .gz isn't a served type; the worker unpacks it)
set -euo pipefail
cd "$(dirname "$0")"
GS_VERSION=0.0.2
PDFLIB_VERSION=1.17.1

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
(cd "$tmp" && npm pack -q "@jspawn/ghostscript-wasm@$GS_VERSION" "pdf-lib@$PDFLIB_VERSION" >/dev/null)
mkdir -p "$tmp/gs" "$tmp/pl"
tar xzf "$tmp/jspawn-ghostscript-wasm-$GS_VERSION.tgz" -C "$tmp/gs"
tar xzf "$tmp/pdf-lib-$PDFLIB_VERSION.tgz" -C "$tmp/pl"

rm -rf dist && mkdir dist
cp index.html dist/
# pdf-lib has literal U+FFFD characters in string literals; the artifact host
# rejects those, so write them as the equivalent \uFFFD escape.
sed 's/\xEF\xBF\xBD/\\uFFFD/g' "$tmp/pl/package/dist/pdf-lib.min.js" > "$tmp/pdf-lib.js"
cat "$tmp/gs/package/gs.js" <(echo) "$tmp/pdf-lib.js" <(echo) worker-main.js > dist/worker.js
gzip -9 -c "$tmp/gs/package/gs.wasm" > dist/gs-gzip.wasm
ls -l dist
