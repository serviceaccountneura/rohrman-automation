#!/usr/bin/env bash
# Serve dots.ocr on the Tesla T4 via vLLM.
# Turing (sm_75) has no bf16 -> float16; cap context so KV cache fits in 16GB;
# enforce-eager to avoid CUDA-graph memory overhead.
set -euo pipefail
VENV=/home/ubuntu/ocr_testing/venvs/dots
MODEL=/home/ubuntu/dots_ocr_repo/weights/DotsMOCR

exec "$VENV/bin/vllm" serve "$MODEL" \
  --served-model-name model \
  --trust-remote-code \
  --chat-template-content-format string \
  --dtype float16 \
  --max-model-len 16384 \
  --gpu-memory-utilization 0.92 \
  --enforce-eager \
  --port 8000
