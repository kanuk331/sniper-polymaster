#!/usr/bin/env bash
# Holdout evaluation, run once after the code freeze (preregistration §1): each holdout period is evaluated right
# after research/iteration2_pipeline.sh has collected it. src/ and config/ must stay unchanged until this finishes.
# Usage: setsid nohup research/iteration2_holdout.sh >/dev/null 2>&1 &    progress: logs/holdout-status.log
set -u
cd "$(dirname "$0")/.."
export SNIPER_DB_PATH="${SNIPER_DB_PATH:-data/live.sqlite}"
export SNIPER_RATE_LIMIT_SHARE="${SNIPER_RATE_LIMIT_SHARE:-0.6}"
OUT=reports/walkforward
HOLDOUT=(p2025-02 p2025-08 p2026-02 p2026-07)

st() { echo "$(date -u +%FT%TZ) $*" >> logs/holdout-status.log; }

st "holdout start (pid $$), commit $(git rev-parse HEAD)"
for p in "${HOLDOUT[@]}"; do
    until [ -f "$OUT/$p/collect.json" ]; do
        if grep -qE "collect $p exit [1-9]" logs/status.log; then
            st "eval $p skipped: collection failed"
            continue 2
        fi
        sleep 60
    done
    if [ -f "$OUT/$p/HOLDOUT_EVALUATED" ]; then
        st "eval $p already evaluated, not repeated"
        continue
    fi
    exec 9> logs/eval.lock
    flock 9  # shared with dev evaluations: one evaluation at a time
    st "eval $p start"
    uv run sniper wf evaluate --role holdout --periods "$p" --confirm-holdout --out "$OUT" >> "logs/eval-$p.log" 2>&1
    st "eval $p exit $?"
    exec 9>&-
done
st "holdout done"
