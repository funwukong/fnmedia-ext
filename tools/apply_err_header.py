"""HLS 代理错误响应加标记头（借 FongMi/TV 的 X-TV-Proxy-Error 思路）。

用户要求（2026-11-13，FongMi 借鉴第 3 项）：
  代理出错时，客户端只看得到 502/404，分不清是【源站问题】还是
  【本插件代理自身问题】。FongMi 的 Proxy.java 出错时加
  `X-TV-Proxy-Error: 1`，客户端据此区分。

我们加 `X-Fnmedia-Error: <code>` + 保留原状态码，code 与已有的
_fetch_fail_reason / picked["_pan_error"] 对齐，便于对日志定位。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HLS = ROOT / "proxy" / "hls_race.py"

# 错误响应点：文案 → 错误码
PATCHES = [
    ('        return Response("no hls line", status_code=404)',
     '        return _err_response("no hls line", 404, "no_hls_line")'),
    ('            return Response("fetch fail upstream", status_code=502)',
     '            return _err_response("fetch fail upstream", 502,\n'
     '                                 "fetch_fail")'),
    ('                return Response("暂无可播线路（该源分片已失效，且未找到其他可用源）",\n'
     '                                status_code=502)',
     '                return _err_response(\n'
     '                    "暂无可播线路（该源分片已失效，且未找到其他可用源）",\n'
     '                    502, "no_line_alive")'),
    ('        return Response(f"seg fail {type(exc).__name__}", status_code=502)',
     '        return _err_response(f"seg fail {type(exc).__name__}", 502,\n'
     '                             "seg_fail")'),
]

HELPER = '''

def _err_response(body: str, status: int, code: str,
                  headers: dict | None = None) -> Response:
    """HLS 代理错误响应：带 X-Fnmedia-Error 标记，便于客户端/日志区分
    「源站问题」与「代理自身问题」（借 FongMi/TV Proxy.java 的
    X-TV-Proxy-Error 思路）。

    code 取值与 picked["_fetch_fail_reason"] / picked["_pan_error"] 对齐：
      no_hls_line      没有任何可用 HLS 线路
      fetch_fail       上游清单抓取失败（源站不可达/403/超时）
      no_line_alive    全部线路分片失效
      seg_fail         分片代理异常
    """
    h = {"Cache-Control": "no-store", "X-Fnmedia-Error": code}
    if headers:
        h.update(headers)
    return Response(body, status_code=status, headers=h)

'''


def main() -> None:
    s = HLS.read_text(encoding="utf-8")
    if "_err_response" in s:
        print("  已存在 _err_response，跳过")
        return
    # 插到 hls_main 之前
    anchor = "\nasync def hls_main("
    assert anchor in s, "锚点缺失"
    s = s.replace(anchor, HELPER + "\nasync def hls_main(", 1)
    n = 0
    for old, new in PATCHES:
        if old in s:
            s = s.replace(old, new, 1)
            n += 1
    HLS.write_text(s, encoding="utf-8")
    print(f"  替换 {n} 处错误响应")


if __name__ == "__main__":
    main()
