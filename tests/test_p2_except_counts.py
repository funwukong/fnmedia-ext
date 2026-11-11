"""P2-except 收敛：计数回归 + 日志定义守门。

- 用 tools/count_except.py（与任务基线同一口径）统计 proxy/ 的宽捕获：
  总数不得超过基线 626，有日志比例不得低于 90%，app.py 不得低于 90%。
- 守门：任何用到 ``_log.`` / ``logger.`` 的模块，必须在模块级定义了
  ``_log`` / ``logger``，否则脚本化插入的日志行会在异常时抛 NameError。
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROXY = ROOT / "proxy"


def _run_counter():
    out = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "count_except.py"), "proxy"],
        capture_output=True, text=True, check=True, cwd=str(ROOT),
    ).stdout
    m = re.match(r"TOTAL broad-except: (\d+)  with-log: (\d+)", out.splitlines()[0])
    assert m, f"unexpected counter output: {out.splitlines()[0]!r}"
    total, logged = int(m.group(1)), int(m.group(2))
    per_file = {}
    for line in out.splitlines()[1:]:
        bits = line.split()
        if len(bits) >= 4 and bits[0].isdigit():
            per_file[bits[-1]] = (int(bits[0]), int(bits[2]))
    return total, logged, per_file


def test_broad_except_totals_do_not_grow():
    total, logged, per_file = _run_counter()
    assert total <= 626, f"broad except grew past baseline 626: {total}"
    assert logged / total >= 0.90, f"logged ratio too low: {logged}/{total}"
    app_total, app_logged = per_file["proxy/app.py"]
    assert app_total <= 301, f"app.py broad except grew past 301: {app_total}"
    assert app_logged / app_total >= 0.90, (
        f"app.py logged ratio too low: {app_logged}/{app_total}")


def test_every_module_using_log_defines_a_logger():
    for path in sorted(PROXY.glob("*.py")):
        if "_vendor" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        defined = set()
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in ("_log", "logger"):
                    defined.add(t.id)
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in ("_log", "logger"):
                    used.add(node.value.id)
        assert used <= defined, (
            f"{path.name} uses {sorted(used - defined)} without a module-level "
            "definition (NameError if the handler ever runs)")
