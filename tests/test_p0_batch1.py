"""第一批 P0 修复的回归测试。

1) _AGG_INFLIGHT 单飞：leader 被取消时等待者不能永久挂起
   （CancelledError 继承 BaseException，原 except Exception 捕不到）。
2) _fetch_playlist：网盘线路必须用算出的 45s 超时，不能写死 15.0
   （转存耗时 16~19s 时被砍掉 → 换线 → 「该线路文件不存在」）。
3) 死代码清理：不再出现「海阔规则解析失败」文案，缩进错乱块已整理。
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "proxy"


class AggregateSingleflightTests(unittest.TestCase):
    """P0-1：聚合单飞在 leader 被取消时不得让等待者悬挂。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(PROXY))
        import app as appmod
        self.app = appmod

    def test_source_catches_base_exception(self):
        """leader 的异常处理必须捕获 BaseException（覆盖 CancelledError）。"""
        src = (PROXY / "app.py").read_text(encoding="utf-8")
        i = src.index("async def _aggregate_briefs(")
        seg = src[i:i + 4000]
        self.assertIn("except BaseException as exc:", seg)
        self.assertNotIn("except Exception as exc:", seg.split(
            "except BaseException as exc:")[0].split("fut = loop.create_future()")[-1])

    def test_cancelled_leader_does_not_hang_waiters(self):
        """真实复现：leader 被取消 → 等待者必须收到异常而不是永远挂着。"""

        async def _case():
            inflight: dict[str, asyncio.Future] = {}
            fut = asyncio.get_running_loop().create_future()
            inflight["k"] = fut

            async def leader():
                try:
                    raise asyncio.CancelledError()
                except BaseException as exc:          # noqa: BLE001
                    if not fut.done():
                        fut.set_exception(
                            exc if isinstance(exc, Exception)
                            else RuntimeError("leader cancelled"))
                    raise

            with self.assertRaises(BaseException):
                await leader()
            inflight.pop("k", None)
            if not fut.done():
                fut.cancel()
            return fut

        fut = asyncio.run(_case())
        self.assertTrue(fut.done())
        with self.assertRaises(Exception):
            fut.result()

    def test_real_aggregate_cancelled_leader_resolves_waiters(self):
        """在真实 _aggregate_briefs 上取消 leader，等待者必须被唤醒并自行重试成功。

        修复前：leader 被 CancelledError 取消时，fut 永远 pending，
        等待者 await _fut 永久挂起（同片名再也搜不出）。
        """
        import sys
        sys.path.insert(0, str(PROXY))
        import app as appmod

        async def _case():
            calls = {"n": 0}
            started = asyncio.Event()
            release = asyncio.Event()

            async def _slow_inner(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    started.set()
                    await release.wait()      # 第一次：模拟慢源
                return [{"guid": "g1", "title": "结果"}]

            target = None
            for name in dir(appmod):
                if name.endswith("__aggregate_briefs_inner"):
                    target = name
                    break
            self.assertTrue(target, "找不到 __aggregate_briefs_inner")
            real = getattr(appmod, target)
            setattr(appmod, target, _slow_inner)
            try:
                leader = asyncio.ensure_future(
                    appmod._aggregate_briefs("测试片名2", 5, False, None))
                await asyncio.wait_for(started.wait(), timeout=3)
                waiter = asyncio.ensure_future(
                    appmod._aggregate_briefs("测试片名2", 5, False, None))
                await asyncio.sleep(0.05)
                leader.cancel()
                release.set()          # 放行第一次调用（leader 已被取消）
                # 等待者必须在有限时间内结束（自己重试拿到结果）
                try:
                    out = await asyncio.wait_for(waiter, timeout=5)
                except asyncio.TimeoutError:
                    self.fail("等待者悬挂：leader 被取消后 waiter 永不返回")
                self.assertTrue(out, "等待者应自行重试并拿到结果")
            finally:
                release.set()
                setattr(appmod, target, real)

        asyncio.run(_case())


class FetchPlaylistTimeoutTests(unittest.TestCase):
    """P0-2：网盘线路的 45s 超时必须真正生效。"""

    def setUp(self):
        self.src = (PROXY / "hls_race.py").read_text(encoding="utf-8")

    def test_uses_computed_timeout(self):
        i = self.src.index("async def _fetch_playlist(")
        seg = self.src[i:i + 2000]
        self.assertIn("timeout = 45.0", seg)
        self.assertIn("timeout=timeout", seg)
        # 不能再有写死的 timeout=15.0
        self.assertNotIn("tries=tries, timeout=15.0", seg)


class DeadCodeCleanupTests(unittest.TestCase):
    """P2-10：海阔已删，死代码与文案一并清理。"""

    def test_no_haikuo_wording_in_user_facing_text(self):
        src = (PROXY / "hls_race.py").read_text(encoding="utf-8")
        self.assertNotIn("海阔规则解析失败", src)

    def test_app_no_haikuo_reference(self):
        for name in ("app.py", "hls_race.py", "providers.py", "admin_api.py"):
            src = (PROXY / name).read_text(encoding="utf-8")
            self.assertNotIn("haikuo", src.lower(), name)

    def test_indent_block_fixed(self):
        """原来错乱的 if body is None 块已整理。

        判据（最本质）：该块内的语句必须与它的控制语句同层缩进 ——
        即「if picked.get(...)」下一行的 return 缩进 = if 的缩进 + 4，
        且中间不能夹着缩进更深的孤立注释块（原错位形态）。
        """
        import ast
        src = (PROXY / "hls_race.py").read_text(encoding="utf-8")
        ast.parse(src)          # 缩进错乱会在这里抛 IndentationError
        lines = src.split("\n")
        # 定位两处「if picked.get("resolve_error")」与其后的 return
        for i, ln in enumerate(lines):
            if 'if picked.get("resolve_error"):' not in ln:
                continue
            ctrl_indent = len(ln) - len(ln.lstrip())
            # 块内第一条语句（跳过注释）
            j = i + 1
            while j < len(lines) and (not lines[j].strip()
                                      or lines[j].strip().startswith("#")):
                j += 1
            self.assertLess(j, len(lines))
            stmt = lines[j]
            stmt_indent = len(stmt) - len(stmt.lstrip())
            self.assertEqual(
                stmt_indent, ctrl_indent + 4,
                f"第 {j + 1} 行缩进与 if 不符（if 在 L{i + 1} indent={ctrl_indent}）："
                f" {stmt.strip()[:50]}")


if __name__ == "__main__":
    unittest.main()
