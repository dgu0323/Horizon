#!/usr/bin/env bash
# 每日一条龙: 抓取日报 → 润色音频 → 卡片式 HTML → 上传腾讯云 COS
#
# 用法:
#   ./scripts/daily-digest.sh            # 完整跑一遍(约 20-30 分钟)
#   ./scripts/daily-digest.sh --audio-only   # 只重做音频 + HTML + 上传(日报已存在时)
#
# 产物:
#   日报  data/summaries/horizon-<今天>-zh.md
#   HTML  data/html/horizon-<今天>-zh.html
#   音频  REPORTS_ROOT/<今天>/ → 腾讯云 COS(App 云同步拉取)
#
# 环境变量:
#   REPORTS_ROOT     音频/上传根目录。未设时: 本机 hunter 用 horizon_output; sshmini 用 iCloud Cosmos hunter
#   AUDIO_TO_ICLOUD  设为 1 时不传 --out-dir, 使用 horizon-audio 默认 iCloud 路径

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

AUDIO_TOP_N=20
TODAY="$(date '+%Y-%m-%d')"
ICLOUD_HUNTER="${HOME}/Library/Mobile Documents/iCloud~dev~clq~Cosmos-Music-Player/Documents/hunter"
HUNTER_ROOT="$(cd "$PROJECT_DIR/.." && pwd)"

if [ -z "${REPORTS_ROOT:-}" ]; then
  if [ "$HUNTER_ROOT" = "/Users/yc/app/ai/hunter" ]; then
    REPORTS_ROOT="/Users/yc/app/ai/hunter/horizon_output"
  elif [ -d "$ICLOUD_HUNTER" ]; then
    REPORTS_ROOT="$ICLOUD_HUNTER"
    AUDIO_TO_ICLOUD="${AUDIO_TO_ICLOUD:-1}"
  else
    REPORTS_ROOT="$HUNTER_ROOT/horizon_output"
  fi
fi

UPLOAD_SCRIPT="$SCRIPT_DIR/upload-reports-cos.sh"
if [ ! -x "$UPLOAD_SCRIPT" ]; then
  UPLOAD_SCRIPT="$HUNTER_ROOT/scripts/upload-reports-cos.sh"
fi

run_audio() {
  if [ "${AUDIO_TO_ICLOUD:-0}" = "1" ] || [ "$REPORTS_ROOT" = "$ICLOUD_HUNTER" ]; then
    uv run horizon-audio -n "$AUDIO_TOP_N" --polish
  else
    mkdir -p "$REPORTS_ROOT/$TODAY"
    uv run horizon-audio -n "$AUDIO_TOP_N" --polish --out-dir "$REPORTS_ROOT/$TODAY"
  fi
}

if [ "${1:-}" = "--audio-only" ]; then
  log "1/3 生成音频(Top $AUDIO_TOP_N, LLM 润色)..."
  run_audio || log "[warn] audio step failed, continuing to html"
  log "2/3 生成 HTML..."
  uv run horizon-html
else
  log "1/4 抓取 + AI 分析(约 15-20 分钟)..."
  uv run horizon --hours 24
  log "2/4 生成音频(Top $AUDIO_TOP_N, LLM 润色)..."
  run_audio || log "[warn] audio step failed, continuing to html"
  log "3/4 生成 HTML..."
  uv run horizon-html
fi

log "4/4 上传到腾讯云 COS ..."
REPORTS_ROOT="$REPORTS_ROOT" "$UPLOAD_SCRIPT" "$TODAY"

log "完成:"
log "  HTML  $PROJECT_DIR/data/html/horizon-$TODAY-zh.html"
log "  音频  $REPORTS_ROOT/$TODAY (已同步到 COS)"
