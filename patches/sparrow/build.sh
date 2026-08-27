#!/usr/bin/env bash
# Build a Sparrow that can follow the BLAKE2b chain, on Linux.
#
#   ./build.sh [workdir]        default: a sparrow-blake2b/ beside the repo
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
# A sibling of the repository, not /tmp. The tree is ~1.4 GB and takes a clone
# plus a full build to recreate, and /tmp would lose it to a reboot with no
# warning partway through a test session. A sibling also lands it on whatever
# volume the checkout is on, which is the one with room for it by construction.
WORK="${1:-$(cd "$PATCHES/../.." && pwd)/../sparrow-blake2b}"
WORK="$(mkdir -p "$(dirname "$WORK")" && cd "$(dirname "$WORK")" && pwd)/$(basename "$WORK")"

# Sparrow needs Java 25. Its releases are built with Temurin 25.0.2+10; anything
# 25 or newer works. The build fails on 21 or below, because drongo uses unnamed
# lambda parameters, which are a preview feature there and final in 22.
#
# Distributions are still shipping 21 as their newest, so an unpacked Temurin in
# $HOME/bin is the usual way to have one. /tmp is searched last and deliberately:
# it is where an unpack lands by accident, and it does not survive a reboot.
if [ -z "${JAVA_HOME:-}" ]; then
    for candidate in "$HOME"/bin/jdk-25* "$HOME"/jdk-25* /usr/lib/jvm/java-25* /opt/jdk-25* /tmp/jdk-25*; do
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
SPARROW_REV=624f999ec5b8681b79ecd0f025cfe0999a496b4f

if [ ! -d "$WORK" ]; then
    echo "cloning Sparrow into $WORK"
    git clone -q https://github.com/sparrowwallet/sparrow.git "$WORK"
fi
cd "$WORK"
git fetch -q --all
git checkout -q "$SPARROW_REV"
git submodule update --init --recursive --depth 1 -q

# Idempotent, because the tree persists between runs now that it is not in /tmp.
# A patch that reverse-applies cleanly is already in the tree, and re-applying it
# would fail and take the script down with it. Anything else is left to fail
# loudly: a tree that is neither clean nor patched is one to look at, not one to
# paper over.
apply_patch() {
    local repo="$1" patch="$2" name
    name="$(basename "$patch")"
    if git -C "$repo" apply --reverse --check "$patch" 2>/dev/null; then
        echo "  $name already applied"
    else
        git -C "$repo" apply --check "$patch"
        git -C "$repo" apply "$patch"
        echo "  $name applied"
    fi
}

echo "applying patches"
apply_patch drongo "$PATCHES"/0001-*.patch
apply_patch . "$PATCHES"/0002-*.patch

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
