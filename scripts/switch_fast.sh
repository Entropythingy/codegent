#!/usr/bin/env bash
# FAST MODE — Qwen2.5-3B-Instruct (chat nhanh, no reasoning)
set -e

MODEL="$HOME/models/Qwen2.5-3B-Instruct-Q4_K_M.gguf"
BIN="$HOME/llama.cpp/build/bin/llama-server"
LOG="$HOME/backup_ai/llama-server.log"
PORT=8081

if [ ! -f "$MODEL" ]; then
  echo "✗ Không tìm thấy model: $MODEL"
  exit 1
fi

if [ ! -x "$BIN" ]; then
  echo "✗ Không tìm thấy binary: $BIN"
  exit 1
fi

# Kill server cũ (nếu có)
pkill -f "$BIN" 2>/dev/null || true
sleep 1

# Đợi port free
for i in {1..10}; do
  if ! ss -tln 2>/dev/null | grep -q ":$PORT "; then break; fi
  sleep 0.5
done

echo "Starting Qwen2.5-3B (FAST) on :$PORT..."

nohup "$BIN" \
  -m "$MODEL" \
  -t 18 -tb 18 \
  -fa \
  -c 4096 \
  -b 512 -ub 128 \
  --parallel 1 \
  --mlock --no-mmap \
  --host 127.0.0.1 --port $PORT \
  > "$LOG" 2>&1 &

echo "Started (PID $!) → log: $LOG"
