#!/usr/bin/env bash
# Upload files from a local folder into an S3-compatible bucket via s3cmd
# sync, replacing the manual "upload through the RustFS web console" step
# documented in README.md's "One-time setup of RustFS" section.
set -euo pipefail

usage() {
  cat <<EOF
Usage: $(basename "$0") [-n] SOURCE_DIR S3_DEST

  SOURCE_DIR   Local directory to upload from (e.g. the local insurance-docs
               corpus pointed at by INSURANCE_DOCS_ROOT, or any other local
               folder of documents/data to sync into S3).
  S3_DEST      Destination, e.g. s3://carqna or s3://carqna/insurance-docs --
               for the insurance docs backend specifically, this should match
               the s3:// value INSURANCE_DOCS_ROOT will be set to (see
               .env.example and
               .plans/008-2026-08-15-s3-compatible-backend-plan-INPROG.md).

Options:
  -n           Dry run: show what would be uploaded without uploading.
  -h           Show this help.

Requires s3cmd already configured against your S3-compatible endpoint (see
README.md's "Setup s3cmd" section -- ~/.s3cfg pointing at RustFS or
equivalent).

Example:
  $(basename "$0") ./data/virtual-fs/insurance-docs s3://carqna/insurance-docs
EOF
}

dry_run=()
while getopts "nh" opt; do
  case "$opt" in
    n) dry_run=(--dry-run) ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

source_dir=$1
s3_dest=$2

if ! command -v s3cmd >/dev/null 2>&1; then
  echo "Error: s3cmd not found on PATH. See README.md's 'Setup s3cmd' section." >&2
  exit 1
fi

if [[ ! -d "$source_dir" ]]; then
  echo "Error: source directory not found: $source_dir" >&2
  exit 1
fi

if [[ "$s3_dest" != s3://* ]]; then
  echo "Error: S3_DEST must start with s3:// (got: $s3_dest)" >&2
  exit 1
fi

# Trailing slash matters to s3cmd sync: with it on the source, the
# directory's *contents* land directly under S3_DEST; without it, the
# source directory itself becomes an extra nested prefix under S3_DEST.
# Force it on both sides so callers don't have to think about it.
s3cmd sync "${dry_run[@]}" "${source_dir%/}/" "${s3_dest%/}/"
