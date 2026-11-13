"""日志与接管守护的纯函数部分：不碰真实 socket。"""
import fnlog
import supervise


def test_get_logger_prefix_and_singleton():
    a = fnlog.get_logger("config")
    b = fnlog.get_logger("config")
    assert a is b
    assert a.name == "fnmedia.config"


def test_setup_logging_idempotent(tmp_path, monkeypatch):
    # 文件 handler 指向仓库外不写盘也行：只验证重复调用不抛错、不重复加 handler。
    fnlog.setup_logging("INFO")
    n = len(__import__("logging").getLogger().handlers)
    fnlog.setup_logging("INFO")
    assert len(__import__("logging").getLogger().handlers) == n


def test_is_official_false_on_missing_socket():
    ok, pid, exe = supervise.is_official("/tmp/fnmedia-ext-no-such.sock")
    assert ok is False
    assert pid is None


def test_rollback_noop_when_no_upstream(monkeypatch, tmp_path):
    # UPSTREAM 不存在时回滚应静默无操作，不抛错。
    monkeypatch.setattr(supervise, "UPSTREAM", tmp_path / "upstream.sock")
    monkeypatch.setattr(supervise, "TARGET", tmp_path / "target.sock")
    supervise.rollback()  # no exception = pass
