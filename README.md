# 教室 YOLO 人员检测与节能控制

双击 `start.bat` 即可。首次运行会创建 `.venv`、安装依赖并下载默认的 `yolo11s.pt` 模型，随后浏览器打开 `http://127.0.0.1:8765`。

如需桌面快捷方式，在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\install-shortcut.ps1
```

## 安全策略

- 上传图片默认只做识别；勾选“测试图片参与控制”后会循环检测，并可触发关闭程序。
- 摄像头帧才参与持续无人确认。
- 默认持续无人 10 秒且至少 3 个有效帧后，按空调、风扇、灯光、班班通顺序关闭。
- 检测到人员时只取消待关闭程序，不改变现有设备状态。
- 推理失败、服务异常或摄像头中断时不关设备。
- 当前 `DeviceManager` 是模拟适配器；接入真实硬件时，应在 `app.py` 的 `shutdown_sequence()` 中调用经过认证的 PLC、继电器或物联网网关接口，并保留状态回读。

## 可选配置

在启动前设置环境变量：

```powershell
$env:YOLO_MODEL='yolo11m.pt'
$env:YOLO_CONFIDENCE='0.40'
$env:YOLO_IMAGE_SIZE='1280'
$env:EMPTY_CONFIRM_SECONDS='10'
```

模型越大、输入尺寸越高，通常越有利于远处小目标，但需要更多算力。最终部署应使用实际教室数据验证人员召回率，并优先微调专用模型。
