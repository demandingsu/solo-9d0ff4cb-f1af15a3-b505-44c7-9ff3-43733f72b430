#!/usr/bin/env bash
# Start both services without Docker (for machines without a Docker daemon).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
EXAMPLE_QUIET=1 python3 -m example_site.server --host 127.0.0.1 --port 8080 &
RECALLQC_QUIET=1 RECALLQC_DATA_DIR="$PWD/data" \
  EXAMPLE_SITE_INTERNAL_URL=http://127.0.0.1:8080 \
  EXAMPLE_SITE_PUBLIC_URL=http://127.0.0.1:8080 \
  python3 -m app.server --port 8000 &
trap 'kill 0' INT TERM EXIT
echo "质检页: http://127.0.0.1:8000   示例站: http://127.0.0.1:8080"
wait
