#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
./build.sh
exec python3 -m unittest discover -s tests -p 'test_*.py' -v
