#!/bin/bash
# ~/switch_agent.sh - Script khởi chạy luân phiên Multi-Agent AI tối ưu cho RAM Single-Channel

MODELS_DIR="$HOME/models"
SERVER_BIN="$HOME/llama.cpp/build/bin/llama-server"
LOG_DIR="$HOME/backup_ai"
HOST="127.0.0.1"
PORT=8081
SERVER_LOG="$LOG_DIR/llama-server.log"

# 1. Dọn dẹp tiến trình cũ (Giải phóng RAM/VRAM hoàn toàn trước khi chạy cái mới)
echo "[*] Đang kiểm tra và dừng tiến trình llama-server cũ trên port $PORT..."
OLD_PID=$(lsof -t -i:$PORT 2>/dev/null)
if [ -n "$OLD_PID" ]; then
    kill -9 $OLD_PID 2>/dev/null
    sleep 2
    echo "[OK] Đã dừng tiến trình PID: $OLD_PID"
else
    echo "[OK] Không có tiến trình nào đang chạy trên port $PORT."
fi

# 2. Chọn cấu hình dựa trên tham số truyền vào (draft hoặc coder)
if [ "$1" == "coder" ]; then
    echo "[*] Khởi động Agent 2: Qwen2.5-Coder-32B (Chính) + 0.5B (Draft)"
    MODEL="$MODELS_DIR/Qwen2.5-Coder-32B-Instruct-Q3_K_M.gguf"
    DRAFT="$MODELS_DIR/qwen2.5-coder-0.5b-instruct-q8_0.gguf"
else
    echo "[*] Khởi động Agent 1: DeepSeek-R1-Distill-Qwen-14B (Chính) + 1.5B (Draft)"
    MODEL="$MODELS_DIR/DeepSeek-R1-Distill-Qwen-14B-Q4_K_M.gguf"
    DRAFT="$MODELS_DIR/qwen2.5-coder-0.5b-instruct-q8_0.gguf"
fi

# Kiểm tra file model tồn tại
if [ ! -f "$MODEL" ] || [ ! -f "$DRAFT" ]; then
    echo "[FAIL] Thiếu file model! Kiểm tra lại thư mục $MODELS_DIR"
    exit 1
fi

# 3. Cấu hình tối ưu cho RAM Single-Channel & GTX 960 Maxwell
# - physcpubind=0-13 : Khóa cứng 14 nhân vật lý, LOẠI BỎ Hyper-Threading (giảm nghẽn RAM)
# - membind=0        : Khóa cứng vào RAM Node 0
# - np 1             : Chỉ dùng 1 slot, dồn 100% băng thông cho model hiện tại
# - fa off           : Tắt Flash Attention để tránh overhead trên kiến trúc Maxwell (GTX 960)
# - ctk/ctv q4_0     : Giảm 50% băng thông RAM cho KV Cache (đánh đổi không đáng kể về chất lượng)
# - c 4096           : Giảm context size để tiết kiệm RAM tối đa

NUMA_CMD="numactl --physcpubind=0-13 --membind=0"

echo "[*] Đang khởi động llama-server với cấu hình tối ưu..."
echo "Lệnh thực thi: $NUMA_CMD $SERVER_BIN ..."

$NUMA_CMD "$SERVER_BIN" \
  -m "$MODEL" \
  -md "$DRAFT" \
  -ngl 0 \
  -ngld 99 \
  -c 4096 \
  -np 1 \
  -t 14 \
  -td 2 \
  -tbd 2 \
  -ctk q4_0 -ctv q4_0 \
  -ctkd q8_0 -ctvd q8_0 \
  -fa on \
  --spec-type draft-simple \
  --host "$HOST" --port "$PORT" \
  > "$SERVER_LOG" 2>&1 &
  
SERVER_PID=$!
echo "[OK] Đã khởi động llama-server với PID: $SERVER_PID"
echo "[*] Log được ghi tại: $SERVER_LOG"
echo "[*] Chờ 5 giây để server khởi tạo..."
sleep 5

# 4. Kiểm tra trạng thái cuối cùng
if curl -s "http://$HOST:$PORT/health" | grep -q "ok"; then
    echo "[OK] Server đã sẵn sàng nhận yêu cầu tại http://$HOST:$PORT"
else
    echo "[FAIL] Server chưa phản hồi. Kiểm tra log: tail -n 20 $SERVER_LOG"
fi
