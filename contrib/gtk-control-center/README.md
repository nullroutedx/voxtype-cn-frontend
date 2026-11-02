# Voxtype 中文工作台（GTK4）

这是独立于原终端配置器的中文图形前端。使用现有 Voxtype 1.1.1+ CLI 和
systemd 用户服务，无需重新编译识别引擎。原版 `voxtype configure` 仍然可用。

## 运行

依赖：Python 3.10+、PyGObject、GTK4、libadwaita 1.5+、已安装的 Voxtype。
本机 Omarchy 已具备这些依赖。其他 Arch 安装可使用发行版软件包
`python-gobject gtk4 libadwaita`。

```bash
python contrib/gtk-control-center/app.py
```

安装独立的用户级入口（无需 sudo）：

```bash
python contrib/gtk-control-center/install.py
```

之后在应用菜单搜索 **Voxtype 中文工作台**，或运行 `~/.local/bin/voxtype-gui`。
入口直接指向当前源码目录，移动或删除目录后需重新安装入口。
不会替换原版程序、终端配置入口或系统服务。

## 功能

- **概览**：真实服务状态、当前模型、启动／停止／重启、登录自启、听写指引。
- **模型**：SenseVoice Small、Paraformer 中文、Whisper Base / Small；
  安装状态来自实时模型目录，下载与启用分开，下载进度按当前文件显示，可取消。
- **设置**：麦克风、听写时长、媒体暂停、输出方式、当前引擎支持的语言。
- **诊断**：按需读取最近 65 条服务日志，不持续刷屏。

设置通过 `voxtype config set` 写入，保留注释；每次保存备份到配置目录内
`config.toml.bak.gui-*`。多项配置失败或新模型无法启动时还原原配置。
服务原本停止时，保存不会启动服务。录音、转写或会议期间拒绝重启或保存。
请勿同时在原终端配置器中编辑并保存，以免两份尚未保存的修改互相覆盖。

首版只提供中文常用模型及常用设置。会议管理、文件字幕、远程识别配置和
权限提升／切换底层二进制继续使用现有 CLI。界面不自动下载模型，不录音、
不发送音频；点击下载时仅调用 Voxtype 官方模型下载流程。
听写快捷键指引针对本机 Omarchy（F9 / Super+Ctrl+X），前端不修改快捷键。

## 验证

```bash
python -m unittest discover -s contrib/gtk-control-center -p 'test_backend.py' -v
# 在有 GTK 图形会话的环境运行（全部使用模拟后端，不更改用户配置）
python contrib/gtk-control-center/test_ui.py
```

GUI 与下载操作异步执行，命令具有超时或下载取消机制。日志与 CLI stderr
不会混入界面绘制。关闭窗口不会停止识别服务。

## 移除入口

仅删除 `~/.local/bin/voxtype-gui` 和
`${XDG_DATA_HOME:-~/.local/share}/applications/io.github.voxtype.ControlCenter.desktop`。
现有模型、配置和服务都保留。
