#!/bin/sh
# Runs the sections one after another (never in parallel: the machine has one core).
unset PYTHONDONTWRITEBYTECODE
cd "$(dirname "$0")"
for s in env installer host nm reconnect store library misc pipeline cancel; do
  python3 run.py $s --out=results/baseline.json >> results/run.log 2>&1 || echo "section $s FAILED" >> results/run.log
done
echo ALL-DONE >> results/run.log
