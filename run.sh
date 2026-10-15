#!/bin/bash
cd "$(dirname "$0")"

source source/bin/activate

exec python bot.py "$@" >> rentron.log 2>&1
