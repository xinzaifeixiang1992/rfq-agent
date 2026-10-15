#!/bin/sh
set -eu

PORT="${PORT:-8501}"
API_PORT="${API_PORT:-8000}"
export API_BASE="${API_BASE:-http://127.0.0.1:${API_PORT}}"

uvicorn app.api.main:app --host 127.0.0.1 --port "$API_PORT" &
API_PID=$!

cleanup() {
  kill "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python - <<'PY'
import os, sys, time, urllib.request
url = os.environ["API_BASE"].rstrip("/") + "/health"
for _ in range(90):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(1)
raise SystemExit("FastAPI failed to become healthy within 90 seconds")
PY

streamlit run web/app.py \
  --server.address 0.0.0.0 \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false
