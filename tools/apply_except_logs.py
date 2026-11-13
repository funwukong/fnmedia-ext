"""给零日志的 except 补上日志（sidecar_io.py / resume_diag.py）。

背景：测试 tools/count_except.py 统计「宽捕获中带日志的比例」必须 ≥90%。
这两个文件是尽力清理/诊断辅助路径，历史上一个日志都没有，把整体比例压到
88.8%。补日志同时也确实有助于定位 sidecar 清理失败与续播诊断异常。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
changed: list[str] = []


def edit(rel: str, old: str, new: str, tag: str) -> None:
    p = ROOT / rel
    s = p.read_text(encoding="utf-8")
    if new in s:
        print(f"  [skip] {tag}")
        return
    if old not in s:
        raise SystemExit(f"[FAIL] {tag}: 锚点未找到 in {rel}")
    p.write_text(s.replace(old, new, 1), encoding="utf-8")
    changed.append(tag)
    print(f"  [ok] {tag}")


# ---------------- sidecar_io.py ----------------
p = ROOT / "proxy/sidecar_io.py"
s = p.read_text(encoding="utf-8")
if "import logging" not in s:
    lines = s.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("import ") or line.startswith("from "):
            last_import = i
    insert_at = last_import + 1
    lines.insert(insert_at, "\nimport logging")
    lines.insert(insert_at + 1, "\n_log = logging.getLogger(\"fnmedia.sidecar_io\")")
    p.write_text("\n".join(lines), encoding="utf-8")
    changed.append("sidecar_io logger")
    print("  [ok] sidecar_io logger")

edit(
    "proxy/sidecar_io.py",
    """        except Exception as exc:  # noqa: BLE001""",
    """        except Exception as exc:  # noqa: BLE001
            _log.debug("sidecar io cleanup failed: %s", exc, exc_info=True)""",
    "sidecar_io except#1",
)

edit(
    "proxy/sidecar_io.py",
    """            except Exception:  # noqa: BLE001""",
    """            except Exception:  # noqa: BLE001
                _log.debug("sidecar io drain failed", exc_info=True)""",
    "sidecar_io except#2",
)

edit(
    "proxy/sidecar_io.py",
    """    except Exception:  # noqa: BLE001""",
    """    except Exception:  # noqa: BLE001
        _log.debug("sidecar io operation failed", exc_info=True)""",
    "sidecar_io except#3",
)

edit(
    "proxy/sidecar_io.py",
    """        except Exception:  # noqa: BLE001""",
    """        except Exception:  # noqa: BLE001
            _log.debug("sidecar io nested op failed", exc_info=True)""",
    "sidecar_io except#4",
)

# ---------------- resume_diag.py ----------------
p2 = ROOT / "proxy/resume_diag.py"
s2 = p2.read_text(encoding="utf-8")
if "import logging" not in s2:
    lines2 = s2.split("\n")
    last = 0
    for i, line in enumerate(lines2):
        if line.startswith("import ") or line.startswith("from "):
            last = i
    lines2.insert(last + 1, "\nimport logging")
    lines2.insert(last + 2,
                  "\n_log = logging.getLogger(\"fnmedia.resume_diag\")")
    p2.write_text("\n".join(lines2), encoding="utf-8")
    changed.append("resume_diag logger")
    print("  [ok] resume_diag logger")

print()
print("changed:", ", ".join(changed) if changed else "(none)")
