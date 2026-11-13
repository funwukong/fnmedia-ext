#!/usr/bin/env bash
# 把 tools/hooks 下的 git 钩子安装到当前仓库的 .git/hooks。
# 用法：tools/install-hooks.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_SRC="${ROOT}/tools/hooks"
GIT_DIR="$(git -C "${ROOT}" rev-parse --git-dir)"
HOOKS_DST="${ROOT}/${GIT_DIR}/hooks"
mkdir -p "${HOOKS_DST}"
for h in "${HOOKS_SRC}"/*; do
  name="$(basename "$h")"
  cp "$h" "${HOOKS_DST}/${name}"
  chmod +x "${HOOKS_DST}/${name}"
  echo "installed hook: ${name}"
done
