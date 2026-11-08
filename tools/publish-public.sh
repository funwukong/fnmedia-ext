#!/usr/bin/env bash
# 生成并（可选）推送「公开构建」到 GitHub。
#
# 规则（务必遵守）：
#   - GitHub 的 main / 标签永不接收全功能源码；
#   - 本脚本把当前 main 的源码经 tools/public_strip.py 剥离后，
#     提交到本地 public 分支（其 upstream 为 origin/main 的公开线）；
#   - 默认只构建并提交到本地 public，不推送；加 --push 才推送。
#
# 用法：
#   tools/publish-public.sh            # 剥离 + 提交到本地 public，并打公开 fpk
#   tools/publish-public.sh --push     # 额外推送到 origin（钩子只放行 public）
#   tools/publish-public.sh --release  # 推送 + 打标签 + 创建 GitHub Release 并上传公开 fpk
#                                       （等价 --push --tag，重发同名版本会强更标签/Release）
#   可选 --notes <file.md> 指定发行说明，否则用公开 CHANGELOG 该版本段落。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY3="${PYTHON:-python3}"
cd "$ROOT"

DO_PUSH=0; DO_TAG=""; DO_RELEASE=0; NOTES_FILE=""; VERSION="$(head -n1 VERSION | tr -d '[:space:]')"
while [ $# -gt 0 ]; do
  case "$1" in
    --push) DO_PUSH=1; shift ;;
    --tag) DO_TAG="$2"; shift 2 ;;
    # 创建/更新 GitHub Release 并上传公开 fpk（隐含 --push --tag）
    --release) DO_RELEASE=1; DO_PUSH=1; shift ;;
    --notes) NOTES_FILE="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done
[ -n "$VERSION" ] || { echo "ERROR: 读不到 VERSION" >&2; exit 1; }

echo "[publish] 版本 ${VERSION}"
git fetch origin --quiet
echo "[publish] origin/main = $(git rev-parse --short origin/main)"

# 1) public 分支：首次基于 origin/main；已存在则确保其与 origin/main 连续
if ! git show-ref --verify --quiet refs/heads/public; then
  git branch public origin/main
  echo "[publish] 已从 origin/main 创建 public 分支"
fi

# 2) 临时公开 worktree（git 忽略目录）
WT=".release/public"
git worktree remove --force "$WT" 2>/dev/null || true
rm -rf "$WT"
mkdir -p "$WT"
git worktree add --quiet "$WT" public
trap 'git worktree remove --force "'"$WT"'" 2>/dev/null || true' EXIT

# 3) 用「剥离后的当前源码」覆盖 public worktree（保留其 .git 指针）
STAGE="$(mktemp -d)"; trap_rc='rm -rf "'"$STAGE"'"'
trap "$(printf '%s\n' "$trap_rc"); git worktree remove --force \"$WT\" 2>/dev/null || true" EXIT
rsync -a \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.release/' --exclude='state/' --exclude='logs/' --exclude='tools/' \
  --exclude='dist/' --exclude='packaging/fpk/.build/' --exclude='.DS_Store' \
  "$ROOT/" "$STAGE/"
"$PY3" "$ROOT/tools/public_strip.py" "$STAGE"
# 公开 worktree 只保留剥离后的跟踪文件：先清空其非 .git 内容再拷入
find "$WT" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
rsync -a --exclude='.git/' "$STAGE/" "$WT/"
rm -rf "$STAGE"

# 4) 在 public worktree 提交（若有差异）
( cd "$WT"
  git add -A
  if git diff --cached --quiet; then
    echo "[publish] public 内容无变化，跳过提交"
  else
    git -c user.name="$(git -C "$ROOT" config user.name || echo jan)" \
        -c user.email="$(git -C "$ROOT" config user.email || echo jan@users.noreply.github.com)" \
        commit -q -m "release: v${VERSION} 公开构建

由 tools/publish-public.sh 从全功能源码剥离生成：
移除非标准源适配与成年模式，仅含通用公开能力。"
  fi
)

# 5) 构建公开 fpk（可选，产出到 dist/，不进 git）
if [ -x packaging/fpk/build.sh ] || [ -f packaging/fpk/build.sh ]; then
  echo "[publish] 构建公开 fpk…"
  PYTHON="$PY3" bash packaging/fpk/build.sh --public || echo "[publish] fpk 构建跳过（环境无 fnpack/网络）"
fi

# 6) 推送 / 标签 / Release（pre-push 钩子只放行 public 与指向 public 的标签）
if [ "$DO_PUSH" -eq 1 ]; then
  git push origin public:main
  if [ "$DO_RELEASE" -eq 1 ]; then
    DO_TAG="${DO_TAG:-v${VERSION}}"
  fi
  if [ -n "$DO_TAG" ]; then
    git tag -f -a "$DO_TAG" -m "fnmedia-ext ${DO_TAG}（公开构建）" public >/dev/null
    # 远端已存在同名标签（重发热修版本）时先删除再推送
    if git ls-remote --exit-code --tags origin "refs/tags/${DO_TAG}" >/dev/null 2>&1; then
      git push origin ":refs/tags/${DO_TAG}" >/dev/null 2>&1
    fi
    git push origin "refs/tags/${DO_TAG}:refs/tags/${DO_TAG}"
  fi
  echo "[publish] 已推送公开构建到 origin/main${DO_TAG:+，标签 $DO_TAG}"

  if [ "$DO_RELEASE" -eq 1 ]; then
    command -v gh >/dev/null 2>&1 || { echo "ERROR: 未找到 gh，无法创建 Release（仍已推送代码/标签）" >&2; exit 1; }

    # —— 固定双版本发布：先全功能(-full)，后轻量(裸名与-lite)。
    # 两个 build 都产出同一裸名 dist/fnmedia-ext-<ver>.fpk（互相覆盖），
    # 故必须先构建全功能并复制为 -full，最后再构建轻量版占位裸名/-lite，
    # 保证无后缀裸名恒为轻量版（轻量版用户自更新按裸名下载）。
    LITE_PUB="dist/fnmedia-ext-${VERSION}-public.fpk"
    LITE_FPK="dist/fnmedia-ext-${VERSION}-lite.fpk"
    LITE_BASE="dist/fnmedia-ext-${VERSION}.fpk"
    FULL_PUB="dist/fnmedia-ext-${VERSION}.fpk"
    FULL_FPK="dist/fnmedia-ext-${VERSION}-full.fpk"

    # 1) 全功能版：全功能源码树构建（不剥离）；打包脚本已剔除维护者个人源/凭证/jar/APK。
    echo "[publish] 构建全功能版 fpk（剔除个人源）…"
    PYTHON="$PY3" bash packaging/fpk/build.sh
    [ -f "$FULL_PUB" ] || { echo "ERROR: 缺少全功能版 $FULL_PUB" >&2; exit 1; }
    cp -f "$FULL_PUB" "$FULL_FPK"
    FSTAGE="$ROOT/packaging/fpk/.build/fpk/app/repo"
    grep -q '^DDUO = True' "$FSTAGE/proxy/features.py" 2>/dev/null \
      || { echo "ERROR: 全功能版 DDUO 不为 True，拒绝发布" >&2; exit 1; }
    [ -f "$FSTAGE/proxy/dduo.py" ] || { echo "ERROR: 全功能版缺少 dduo.py" >&2; exit 1; }
    if tar tzf "$FULL_FPK" 2>/dev/null | grep -qiE \
        'guard\.jar$|guard/sites\.json|guard/imported\.json|spiders/jars/.*\.jar|spiders/jars\.json|catvod\.local\.json|/state/|\.apk$'; then
      echo "ERROR: 全功能版检出个人源/凭证/APK，拒绝发布" >&2; exit 1
    fi

    # 2) 轻量版：重建裁剪公开包（覆盖被全功能占用的裸名），派生 -lite 与裸名。
    echo "[publish] 构建轻量版 fpk（裁剪）…"
    PYTHON="$PY3" bash packaging/fpk/build.sh --public
    [ -f "$LITE_PUB" ] || { echo "ERROR: 缺少轻量版 $LITE_PUB" >&2; exit 1; }
    cp -f "$LITE_PUB" "$LITE_FPK"
    cp -f "$LITE_PUB" "$LITE_BASE"
    for f in "$LITE_FPK" "$LITE_BASE" "$FULL_FPK"; do
      ( cd dist && sha256sum "$(basename "$f")" > "$(basename "$f").sha256" )
    done

    # 发行说明：优先 --notes 指定，否则从公开树 CHANGELOG 抽取该版本段落
    NOTES="$(mktemp)"
    if [ -n "$NOTES_FILE" ] && [ -f "$NOTES_FILE" ]; then
      cp "$NOTES_FILE" "$NOTES"
    else
      ( cd "$WT" && awk '/^## v'"${VERSION}"' /{f=1} f&&/^## v/&&$0!~/^## v'"${VERSION}"' /{exit} f' CHANGELOG.md ) > "$NOTES"
    fi
    REPO_SLUG="$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"
    if ! gh release view "$DO_TAG" --repo "$REPO_SLUG" >/dev/null 2>&1; then
      gh release create "$DO_TAG" \
        --repo "$REPO_SLUG" \
        --title "飞牛影视在线扩展 ${DO_TAG}" \
        --notes-file "$NOTES"
    else
      gh release edit "$DO_TAG" --repo "$REPO_SLUG" \
        --title "飞牛影视在线扩展 ${DO_TAG}" --notes-file "$NOTES"
    fi
    # gh 2.x 用独立的 upload 子命令上传/覆盖资产；一次发布两个版本及其校验值
    gh release upload "$DO_TAG" \
      "$LITE_BASE" "${LITE_BASE}.sha256" \
      "$LITE_FPK" "${LITE_FPK}.sha256" \
      "$FULL_FPK" "${FULL_FPK}.sha256" \
      --repo "$REPO_SLUG" --clobber
    rm -f "$NOTES"
    echo "[publish] 已发布 GitHub Release（轻量版 + 全功能版）：https://github.com/${REPO_SLUG}/releases/tag/${DO_TAG}"

    # 归档一份全功能版到本地仓库外目录（与 Release 的 -full 同源，已剔除个人源）。
    echo "[publish] 归档全功能版到本地目录…"
    ARC="${FNM_LOCAL_ARCHIVE_DIR:-/vol1/1000/Ai工作区/fnmedia-ext-local-fpk}"
    mkdir -p "$ARC"
    cp -f "$FULL_FPK" "$ARC/fnmedia-ext-${VERSION}-full.fpk"
    ( cd "$ARC" && sha256sum "fnmedia-ext-${VERSION}-full.fpk" > "fnmedia-ext-${VERSION}-full.fpk.sha256" )
    echo "[publish] 本地全功能归档：$ARC/fnmedia-ext-${VERSION}-full.fpk"
  fi
else
  echo "[publish] 已提交到本地 public，未推送。确认无误后运行："
  echo "          tools/publish-public.sh --push [--tag v${VERSION}]"
  echo "          或一把发布：tools/publish-public.sh --release"
fi
