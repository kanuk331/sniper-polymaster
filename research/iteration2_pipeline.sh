#!/usr/bin/env bash
# Iteration-2 data pipeline, resumable: collect every plan period in a fixed order and evaluate dev periods right
# after their collection. Holdout periods are only collected here; they are evaluated once, after the code freeze.
# Usage: setsid nohup research/iteration2_pipeline.sh >/dev/null 2>&1 &    progress: logs/status.log
set -u
cd "$(dirname "$0")/.."
export SNIPER_DB_PATH="${SNIPER_DB_PATH:-data/live.sqlite}"
# 0.85 triggered Cloudflare 429 blocks on the Data API (HTML body, several minutes); 0.6 held in earlier runs.
export SNIPER_RATE_LIMIT_SHARE="${SNIPER_RATE_LIMIT_SHARE:-0.6}"
ORDER=(p2024-10 p2025-05 p2025-11 p2025-02 p2025-08 p2026-02 p2026-07 p2026-05 p2026-09)
DEV=" p2024-10 p2025-05 p2025-11 p2026-05 p2026-09 "
OUT=reports/walkforward
mkdir -p logs "$OUT"

st() { echo "$(date -u +%FT%TZ) $*" >> logs/status.log; }

collect() {
    local p=$1 attempt rc=1
    for attempt in 1 2 3; do
        st "collect $p start (attempt $attempt)"
        uv run sniper wf collect --periods "$p" --out "$OUT" >> "logs/collect-$p.log" 2>&1
        rc=$?
        [ "$rc" -eq 0 ] && break
        if [ "$attempt" -lt 3 ]; then
            st "collect $p attempt $attempt failed (rc $rc); pause $((300 * attempt)) s"
            sleep $((300 * attempt))
        fi
    done
    st "collect $p exit $rc"
    return "$rc"
}

eval_dev() {
    exec 9> logs/eval.lock
    flock 9  # one evaluation at a time: evaluations share the CLOB rate limit
    st "eval $1 start"
    uv run sniper wf evaluate --role dev --periods "$1" --out "$OUT" >> "logs/eval-$1.log" 2>&1
    st "eval $1 exit $?"
}

st "pipeline start (pid $$)"
for p in "${ORDER[@]}"; do
    if [ -f "$OUT/$p/collect.json" ]; then
        st "collect $p already done"
    else
        collect "$p" || continue
    fi
    if [[ "$DEV" == *" $p "* && ! -f "$OUT/$p/summary.json" ]]; then
        eval_dev "$p" &
    fi
done
wait
st "pipeline done"
