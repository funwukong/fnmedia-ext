"""P0 测试骨架：只测纯函数与配置，不碰真实 socket / 网络 / 飞牛。

proxy 模块用裸 import（``import providers``），所以把 proxy 目录
加到 sys.path 前面；测试不启动 uvicorn，不读真实 state/。
"""
import sys
from pathlib import Path

PROXY = Path(__file__).resolve().parent.parent / "proxy"
if str(PROXY) not in sys.path:
    sys.path.insert(0, str(PROXY))
