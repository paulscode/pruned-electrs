#!/usr/bin/env bash
# Build a Sparrow that can follow the BLAKE2b chain, on Linux.
#
#   ./build.sh [workdir]        default: /tmp/sparrow-blake2b
#
# Clones Sparrow at the revision these patches were made against, checks out its
# submodules at their pins, applies the two patches, and builds. Leaves a tree
# you can run with `./sparrow -n testnet4`.
#
# The patches are deliberately two, against two repositories, because that is
# how the code is split:
#
#   0001  drongo    reads a header's length and hashes a v2 one with BLAKE2b
#   0002  sparrow   walks a mixed run, stores it, and negotiates protocol 1.8
#
# Both confine their changes to one new class each (BlockHeaderV2,
# VariableHeaders) plus small call-site edits, so replacing them with whatever
# upstream settles on should be a deletion and a few reverts rather than a
# merge.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
PATCHES="$PWD"
WORK="${1:-/tmp/sparrow-blake2b}"

# Sparrow needs Java 25. Its releases are built with Temurin 25.0.2+10; anything
# 25 or newer works. The build fails on 21 or below, because drongo uses unnamed
# lambda parameters, which are a preview feature there and final in 22.
if [ -z "${JAVA_HOME:-}" ]; then
    for candidate in /usr/lib/jvm/java-25* /tmp/jdk-25*; do
        [ -x "$candidate/bin/javac" ] && { JAVA_HOME="$candidate"; break; }
    done
fi
[ -n "${JAVA_HOME:-}" ] || {
    echo "Set JAVA_HOME to a JDK 25 or newer." >&2
    echo "  https://github.com/adoptium/temurin25-binaries/releases" >&2
    exit 1
}
export JAVA_HOME
"$JAVA_HOME/bin/java" -version 2>&1 | head -1

# The revision the patches were made against. Pinned, not `main`: these touch
# header parsing and the on-disk store, and applying them to a moved tree with
# --3way would be a merge nobody reviewed.
SPARROW_REV=74060d14723b3805e72db8e137a1f3c326aeda4e

if [ ! -d "$WORK" ]; then
    echo "cloning Sparrow into $WORK"
    git clone -q https://github.com/sparrowwallet/sparrow.git "$WORK"
fi
cd "$WORK"
git fetch -q --all
git checkout -q "$SPARROW_REV"
git submodule update --init --recursive --depth 1 -q

echo "applying patches"
git -C drongo apply --check "$PATCHES"/0001-*.patch
git -C drongo apply "$PATCHES"/0001-*.patch
git apply --check "$PATCHES"/0002-*.patch
git apply "$PATCHES"/0002-*.patch

echo "building"
./gradlew build -x test --console=plain

echo
echo "Built. Run it with:"
echo "  cd $WORK && ./sparrow -n testnet4"
echo
echo "Point it at your Electrum server under Preferences > Server. It must be one"
echo "that serves protocol 1.8; an unpatched server on this chain will not have"
echo "the headers this build expects, and an unpatched Sparrow will be refused by"
echo "a patched server, which is the intended behaviour on both sides."
