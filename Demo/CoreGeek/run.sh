#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 main3.py "$1"
