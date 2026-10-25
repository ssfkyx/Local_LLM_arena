#!/usr/bin/env bash
# Local Arena launcher / 启动脚本。
#
# Always runs from the repository root, so it works from any working directory
# and after cloning anywhere. / 始终以脚本所在目录为工作目录，
# 因此克隆到任意路径后都能直接运行。
#
#   ./run_arena.sh
#   ./run_arena.sh --list-models
#   ./run_arena.sh --compare
#   ./run_arena.sh --headless --quick --model <模型名>

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
exec python3 run_arena.py "$@"
