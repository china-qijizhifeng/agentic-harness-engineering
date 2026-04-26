#!/usr/bin/env bash
set -euo pipefail

# # # 恢复 gpt54 实验，从第 1 轮开始，跳过评测（直接用已有 rollout 结果）
# exec ./evolve.sh \
#     --experiment xxx \
#     --start-iteration 13 \
#     --skip-eval \
#     configs/experiments/exp-003-gpt54-32.yaml


exec ./evolve.sh \
    --experiment xxx \
    --start-iteration 13 \
    --skip-eval \
    xxx
    

    # --skip-eval \
# exec ./evolve.sh \
    # --experiment xxx \
    # --start-iteration 1 \
    # --skip-eval \
    # xxx