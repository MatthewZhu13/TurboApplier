#!/bin/sh
# Usage: ./jobbot.sh <command>   (see ./jobbot.sh --help)
cd "$(dirname "$0")" && exec .venv/bin/python -m jobbot "$@"
