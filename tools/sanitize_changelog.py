#!/usr/bin/env python3
"""构建期清洗【分发包内】的 CHANGELOG（只作用于 fpk 暂存副本，不改源码）。

完整版/公开版安装包的“更新内容”都不应出现：
  - 维护者自用的具体私源名称（如 糯米 / 文采 / 比特 / jinenge 等）；
  - 内置私源清单、私有 jar 的仓库内路径；
  - 限制级（18+）独立库能力的任何描述。

按 Markdown 列表项整体删除（含其续行），避免留下悬空残句。
源码 CHANGELOG.md 保留完整开发记录，便于作者自查。
"""
import re
import sys
from pathlib import Path

# 命中任一关键词的【整个列表项】或普通行都会从分发包中剔除。
FORBIDDEN = (
    "成年", "成人", "18+", "18 禁", "🔞", "限制级",
    "糯米", "文采", "比特", "jinenge",
    "sites.json", "guard.jar",
)

_BULLET = re.compile(r"^(\s*)[-*]\s+")


def _hit(text: str) -> bool:
    return any(t in text for t in FORBIDDEN)


def sanitize(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # 连续引用块（> 开头）作为整体处理，命中则整段删除避免残句。
        if line.lstrip().startswith(">"):
            block = [line]
            j = i + 1
            while j < n and lines[j].lstrip().startswith(">"):
                block.append(lines[j])
                j += 1
            if not _hit("\n".join(block)):
                out.extend(block)
            i = j
            continue
        m = _BULLET.match(line)
        if m:
            # 收集该列表项：起始 bullet 行 + 后续更深缩进的续行。
            base_indent = len(m.group(1))
            block = [line]
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "":
                    # 空行：向后再看一眼，若仍是本项续行则归入，否则结束。
                    k = j + 1
                    if k < n and lines[k].startswith(" " * (base_indent + 1)) \
                            and not _BULLET.match(lines[k]):
                        block.append(nxt)
                        j += 1
                        continue
                    break
                if _BULLET.match(nxt):
                    break
                if nxt.startswith(" " * (base_indent + 1)) or nxt.startswith("\t"):
                    block.append(nxt)
                    j += 1
                    continue
                break
            if not _hit("\n".join(block)):
                out.extend(block)
            i = j
            continue
        # 非列表行：引用块/普通行直接按行判断。
        if not _hit(line):
            out.append(line)
        i += 1
    # 删除清洗后变空的【小节】标题（仅 ### 及更深层级），保留 ## 版本大标题
    # （某版本本周期条目可能全部命中清洗，但版本号本身应保留为分隔）。
    for _ in range(8):
        result: list[str] = []
        k = 0
        removed = False
        while k < len(out):
            head = out[k]
            depth = len(head) - len(head.lstrip("#"))
            if head.startswith("#") and depth >= 3:
                j = k + 1
                while j < len(out) and not out[j].startswith("#"):
                    j += 1
                if not any(s.strip() for s in out[k + 1:j]):
                    result.extend(out[j:])
                    removed = True
                    break
            result.append(out[k])
            k += 1
        out = result
        if not removed:
            break
    # 收敛 3+ 连续空行
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: sanitize_changelog.py <CHANGELOG.md>", file=sys.stderr)
        return 2
    p = Path(sys.argv[1])
    if not p.is_file():
        return 0
    original = p.read_text(encoding="utf-8")
    cleaned = sanitize(original)
    if cleaned != original:
        p.write_text(cleaned, encoding="utf-8")
        print(f"[changelog] 已清洗分发副本（{len(original)} -> {len(cleaned)} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
