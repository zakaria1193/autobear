#!/usr/bin/env bash
set -euo pipefail

run() {
    printf '\033[1m' >&2
    printf '%q ' "$@" >&2
    printf '\033[0m\n' >&2
    "$@"
}

version=4.2.4
prefix=${AUTOCOMPDB_INSTALL_PREFIX:-"$HOME/.local"}
bin_dir="$prefix/bin"
lib_dir="$prefix/lib/autocompdb"
bear="$bin_dir/bear"
installed_version=$("$bear" --version 2>/dev/null || true)
if [[ $installed_version =~ ^bear[[:space:]]+4\.([0-9]+)\.([0-9]+) ]] &&
    (( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 4) )) &&
    "$bear" parse-sh --help >/dev/null 2>&1; then
    printf 'Compatible Bear already exists at %s.\n' "$bear"
    exit 0
fi

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)
        arch=amd64
        checksum=a028bfa0831c83eca10cb233b81165c2a9e057c750a4a3050f0d195bb7271d13
        ;;
    Linux-aarch64)
        arch=arm64
        checksum=3a5fd6b4e03a8db50906fc76bf40addef0f1b82a38456988dcc04d285f4f0f9d
        ;;
    *)
        printf 'The Bear package is available through this script on Linux x86-64 and AArch64.\n' >&2
        exit 1
        ;;
esac

for tool in curl dpkg-deb sha256sum; do
    if ! command -v "$tool" >/dev/null; then
        printf 'Install %s before running this script.\n' "$tool" >&2
        exit 1
    fi
done

libc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true)
if ! [[ $libc =~ ^glibc[[:space:]]+([0-9]+)\.([0-9]+) ]] ||
    ! (( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 39) )); then
    printf 'The Bear package requires glibc 2.39 or later. Found %s.\n' "${libc:-no glibc}" >&2
    exit 1
fi

package="bear_${version}-1_${arch}.deb"
install_dir="$lib_dir/bear-$version"
printf 'Install Bear %s at %s? [y/N] ' "$version" "$bear"
if ! read -r answer || [[ $answer != [yY] && $answer != [yY][eE][sS] ]]; then
    printf 'Installation canceled.\n'
    exit 0
fi

stage=$(run mktemp --directory)
install_stage=
link_stage=
cleanup() {
    rm --recursive --force -- "$stage"
    if [[ -n $install_stage ]]; then
        rm --recursive --force -- "$install_stage"
    fi
    if [[ -n $link_stage ]]; then
        rm --recursive --force -- "$link_stage"
    fi
}
trap cleanup EXIT

run curl --fail --silent --show-error --location \
    "https://deb.debian.org/debian/pool/main/b/bear/$package" --output "$stage/$package"
printf '%s  %s\n' "$checksum" "$stage/$package" | run sha256sum --check
run dpkg-deb --extract "$stage/$package" "$stage/package"

payload="$stage/package/usr/libexec/bear"
driver="$payload/bin/bear-driver"
check_source=autocompdb-compatibility-check.c
if ! printf 'gcc -c %s\n' "$check_source" |
    run "$driver" parse-sh --directory "$stage" --output "$stage/check.json" ||
    ! grep --fixed-strings --quiet "$check_source" "$stage/check.json"; then
    printf 'The extracted Bear could not parse a compiler command on this host. Check glibc and libgcc_s.\n' >&2
    exit 1
fi

run mkdir --parents "$bin_dir" "$lib_dir"
if [[ ! -e $install_dir ]]; then
    install_stage=$(run mktemp --directory "$lib_dir/.bear-install.XXXXXX")
    run cp --archive -- "$payload" "$install_stage/bear"
    run mv --no-target-directory --no-clobber -- "$install_stage/bear" "$install_dir"
fi
run "$install_dir/bin/bear-driver" parse-sh --help >/dev/null

link_stage=$(run mktemp --directory "$bin_dir/.bear-link.XXXXXX")
run ln --symbolic -- "../lib/autocompdb/bear-$version/bin/bear-driver" "$link_stage/bear"
run mv --no-target-directory -- "$link_stage/bear" "$bear"
run "$bear" --version
printf 'Installed %s. Put %s before the system directories on PATH.\n' "$bear" "$bin_dir"
