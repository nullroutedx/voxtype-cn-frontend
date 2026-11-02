#!/usr/bin/env python3
"""Install a per-user launcher pointing at this checkout; no root required."""
from pathlib import Path
import os
import shlex


def install():
    source = Path(__file__).resolve().parent / "app.py"
    bin_dir = Path.home() / ".local/bin"
    applications = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
    bin_dir.mkdir(parents=True, exist_ok=True)
    applications.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / "voxtype-gui"
    launcher.write_text("#!/bin/sh\nexec /usr/bin/python " + shlex.quote(str(source)) + ' "$@"\n')
    launcher.chmod(0o755)
    # Desktop Exec has different quoting rules from shell quoting.
    executable = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
    desktop = applications / "io.github.voxtype.ControlCenter.desktop"
    desktop.write_text('[Desktop Entry]\nType=Application\nName=Voxtype 中文工作台\nComment=管理本地语音识别、中文模型与麦克风\n' + f'Exec="{executable}"\n' + 'Icon=audio-input-microphone\nTerminal=false\nCategories=AudioVideo;Audio;\nKeywords=voxtype;speech;dictation;语音;中文;听写;\nStartupWMClass=io.github.voxtype.ControlCenter\n')
    print(f"已安装：{launcher}\n应用菜单：Voxtype 中文工作台\n入口文件：{desktop}")


if __name__ == "__main__":
    install()
