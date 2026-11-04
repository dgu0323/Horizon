#!/usr/bin/env bash
# 把日报音频/文字稿上传到腾讯云 COS,供 Retro Music App 云同步拉取。
#
# 用法:
#   ./scripts/upload-reports-cos.sh                 # 全量扫描 REPORTS_ROOT 上传
#   ./scripts/upload-reports-cos.sh 2026-10-29      # 只上传指定日期目录
#
# 协议(与 App 端 ReportSyncManager 约定):
#   桶根目录放 manifest.json: {"version":1,"reports":[{"date":"yyyy-MM-dd","files":[{"name","size"}]}]}
#   音频/文字稿按 <date>/<file> 路径存放。
#   先传文件,最后传 manifest.json,避免 App 拉到半成品清单。
#
# 依赖: rclone(配置名为 cos-hunter,见 ~/.config/rclone/rclone.conf)
#
# 环境变量:
#   REPORTS_ROOT  本地日报根目录(默认见下方)
#   REMOTE        rclone 远端(默认 cos-hunter:hunter-daily-1256894360)

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

ICLOUD_HUNTER="${HOME}/Library/Mobile Documents/iCloud~dev~clq~Cosmos-Music-Player/Documents/hunter"
HUNTER_HORIZON_OUTPUT="$(cd "$(dirname "$0")/../.." && pwd)/horizon_output"

if [ -n "${REPORTS_ROOT:-}" ]; then
  :
elif [ -d "$HUNTER_HORIZON_OUTPUT" ]; then
  REPORTS_ROOT="$HUNTER_HORIZON_OUTPUT"
elif [ -d "$ICLOUD_HUNTER" ]; then
  REPORTS_ROOT="$ICLOUD_HUNTER"
else
  REPORTS_ROOT="$HUNTER_HORIZON_OUTPUT"
fi

REMOTE="${REMOTE:-cos-hunter:hunter-daily-1256894360}"
ONLY_DATE="${1:-}"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

if ! command -v rclone >/dev/null 2>&1; then
  echo "error: rclone not found in PATH" >&2
  exit 1
fi

if [ ! -d "$REPORTS_ROOT" ]; then
  echo "error: REPORTS_ROOT not a directory: $REPORTS_ROOT" >&2
  exit 1
fi

log "REPORTS_ROOT=$REPORTS_ROOT"

# 1. 生成 manifest.json(扫描所有 yyyy-MM-dd 目录)
log "生成 manifest.json ..."
python3 - "$REPORTS_ROOT" <<'PY'
import json, re, sys
from pathlib import Path

root = Path(sys.argv[1])
reports = []
for d in sorted(p for p in root.iterdir()
                if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)):
    files = sorted(
        ({"name": f.name, "size": f.stat().st_size}
         for f in d.iterdir() if f.suffix in (".mp3", ".md")),
        key=lambda x: x["name"],
    )
    if files:
        reports.append({"date": d.name, "files": files})
manifest = {"version": 1, "reports": reports}
out = root / "manifest.json"
out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"  {len(reports)} 个日报目录 -> {out}")
PY

# 2. 上传内容文件(mp3/md),--files-from 保持目录结构
LIST_FILE="$(mktemp)"
trap 'rm -f "$LIST_FILE"' EXIT
if [ -n "$ONLY_DATE" ]; then
  (cd "$REPORTS_ROOT" && find "$ONLY_DATE" -type f \( -name '*.mp3' -o -name '*.md' \)) > "$LIST_FILE"
else
  (cd "$REPORTS_ROOT" && find . -type f \( -name '*.mp3' -o -name '*.md' \) | sed 's|^\./||') > "$LIST_FILE"
fi

log "上传音频/文字稿 ($(wc -l < "$LIST_FILE" | tr -d ' ') 个文件)..."
rclone copy "$REPORTS_ROOT" "$REMOTE" \
  --files-from "$LIST_FILE" \
  --checkers 8 --transfers 8 \
  --stats-one-line --stats 10s

# 3. 最后上传 manifest.json
log "上传 manifest.json ..."
rclone copyto "$REPORTS_ROOT/manifest.json" "$REMOTE/manifest.json"

log "完成: https://hunter-daily-1256894360.cos.ap-beijing.myqcloud.com/manifest.json"
