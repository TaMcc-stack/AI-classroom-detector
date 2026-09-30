"""便携版入口：启动本地服务，并在模型就绪后打开浏览器。"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"

# 校园网/单位网络常配系统代理。不绕过的话，对 127.0.0.1 的请求会被送去代理，
# 健康检查一直失败，浏览器要等满超时才弹出来。
for _key in ("NO_PROXY", "no_proxy"):
    _merged = [v for v in os.environ.get(_key, "").split(",") if v]
    os.environ[_key] = ",".join(dict.fromkeys(_merged + [HOST, "localhost", "::1"]))


def bundle_dir() -> Path:
    """PyInstaller 解包目录；未打包时就是本文件所在目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def open_when_ready(timeout: float = 180.0) -> None:
    """等模型真正加载完成再开浏览器，避免停在连接被拒的页面上。"""
    # 明确不走代理：直接双击 exe 启动时没有 bat 帮忙设环境变量，只能靠自己。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(f"{URL}/api/health", timeout=1) as resp:
                if json.load(resp).get("ok"):
                    break
        except Exception:
            pass
        time.sleep(0.5)
    webbrowser.open(URL)


def main() -> int:
    if port_in_use():
        print(f"端口 {PORT} 已被占用，请先关闭之前启动的本程序。")
        input("按回车键退出...")
        return 1

    # 切到解包目录，让 ultralytics 用相对名 "yolo11s.pt" 就地找到随程序分发的模型：
    # 既不会联网下载，界面上显示的还是干净的短名字，而不是一长串绝对路径。
    base = bundle_dir()
    if (base / "yolo11s.pt").exists():
        os.chdir(base)

    import uvicorn
    from app import app

    print(f"服务启动中：{URL}")
    print("模型加载完成后会自动打开浏览器。关闭本窗口即可停止服务。")
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
