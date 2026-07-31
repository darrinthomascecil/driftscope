#!/usr/bin/env bash
# Install the standalone `kustomize` binary for THIS platform (Linux or macOS, amd64 or arm64) --
# no brew, no package manager. The bootstrap needs standalone kustomize (kubectl's built-in
# `kubectl kustomize` lacks `kustomize edit`, which image.sh uses). Pinned for reproducibility.
#
#   make kustomize                                  # installs to /usr/local/bin
#   BIN_DIR=~/.local/bin make kustomize             # or somewhere on your PATH without sudo
#   KUSTOMIZE_VERSION=v5.4.3 make kustomize         # pin a different version
set -euo pipefail

VERSION="${KUSTOMIZE_VERSION:-v5.8.1}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

os="$(uname -s | tr '[:upper:]' '[:lower:]')"   # linux | darwin
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
case "$os" in linux | darwin) ;; *) echo "unsupported OS: $os" >&2; exit 1 ;; esac

# Release tag is `kustomize/vX.Y.Z` (the %2F is the URL-encoded slash); asset is a per-OS/arch tgz.
url="https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${VERSION}/kustomize_${VERSION}_${os}_${arch}.tar.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "==> downloading kustomize ${VERSION} (${os}/${arch})"
curl -fsSL "$url" | tar -xz -C "$tmp"

if [ -w "$BIN_DIR" ]; then
  install -m 0755 "$tmp/kustomize" "$BIN_DIR/kustomize"
else
  echo "==> $BIN_DIR not writable; using sudo (set BIN_DIR=~/.local/bin to avoid)"
  sudo install -m 0755 "$tmp/kustomize" "$BIN_DIR/kustomize"
fi

echo "==> installed: $("$BIN_DIR/kustomize" version)"
