#!/usr/bin/env python3
"""Chinese GTK4 control center for an existing Voxtype installation."""
from __future__ import annotations

import sys
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from backend import Backend


CSS = b"""
.hero { background: linear-gradient(120deg, #183f45, #226c68); color: white; border-radius: 20px; padding: 26px; }
.hero-title { font-size: 28px; font-weight: 800; }
.hero-subtitle { color: #d0eeea; }
.eyebrow { font-size: 11px; font-weight: 800; letter-spacing: 2px; }
.page-title { font-size: 26px; font-weight: 800; }
.card-title { font-size: 18px; font-weight: 700; }
.model-card { padding: 18px; border-radius: 16px; }
.pill { border-radius: 20px; padding: 4px 10px; background: alpha(@accent_color, 0.13); color: @accent_color; font-size: 12px; }
.muted { opacity: 0.65; }
.step { font-size: 20px; font-weight: 800; color: @accent_color; }
.error-box { padding: 12px; border-radius: 12px; background: alpha(@error_color, 0.12); }
textview { padding: 12px; }
"""

MODELS = [
    ("sensevoice", "small", "SenseVoice Small", "中文首选", "普通话、粤语、中英夹杂。轻量量化版，适合本机 CPU。", "约 239 MB"),
    ("paraformer", "paraformer-zh", "Paraformer 中文", "中文备选", "普通话与中英双语，可与 SenseVoice 对比实际录音效果。", "约 487 MB"),
    ("whisper", "base", "Whisper Base", "轻量多语言", "适合简短录音和多语言入门，速度优先。", "约 142 MB"),
    ("whisper", "small", "Whisper Small", "多语言进阶", "更大的多语言模型，识别时通常需要等待更久。", "约 466 MB"),
]


def label(text, css=None, wrap=False):
    item = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if css:
        item.add_css_class(css)
    return item


def box(spacing=12, horizontal=False):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if horizontal else Gtk.Orientation.VERTICAL, spacing=spacing)


def button(text, callback, primary=False):
    item = Gtk.Button(label=text, valign=Gtk.Align.CENTER)
    if primary:
        item.add_css_class("suggested-action")
    item.connect("clicked", callback)
    return item


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Voxtype · 语音工作台", default_width=940, default_height=780)
        self.backend = Backend()
        self.snapshot_data = None
        self.busy = False
        self.polling = False
        self.status_pair = None
        self.model_controls = []
        self.device_names = ["default"]
        self.dirty = False
        self.loading = False
        self.is_downloading = False
        self.connect("close-request", self.on_close)

        self.toast = Adw.ToastOverlay()
        self.set_content(self.toast)
        shell = box(0)
        self.toast.set_child(shell)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Voxtype", subtitle="语音工作台"))
        header.pack_end(button("诊断", lambda _: self.show_logs()))
        shell.append(header)
        tabs = Adw.ViewSwitcher()
        tabs.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        tabs.set_halign(Gtk.Align.CENTER)
        self.stack = Adw.ViewStack()
        self.stack.set_vexpand(True)
        tabs.set_stack(self.stack)
        shell.append(tabs)

        self.operation = box(8)
        self.operation.set_margin_start(28)
        self.operation.set_margin_end(28)
        self.operation.set_margin_top(10)
        self.operation.set_margin_bottom(10)
        op_row = box(12, True)
        self.operation_text = label("正在读取本机配置…", wrap=True)
        self.operation_text.set_hexpand(True)
        op_row.append(self.operation_text)
        self.cancel_button = button("取消下载", lambda _: self.backend.download_cancel.set())
        self.cancel_button.set_visible(False)
        op_row.append(self.cancel_button)
        self.operation.append(op_row)
        self.progress = Gtk.ProgressBar()
        self.operation.append(self.progress)
        shell.append(self.operation)
        self.error = label("", wrap=True)
        self.error.add_css_class("error-box")
        self.error.set_margin_start(28)
        self.error.set_margin_end(28)
        self.error.set_visible(False)
        shell.append(self.error)
        shell.append(self.stack)
        self.make_home()
        self.make_models()
        self.make_settings()
        self.refresh_snapshot()
        self.timer = GLib.timeout_add_seconds(3, self.poll)
        self.poll()

    def page(self, name, title, icon):
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=850, tightening_threshold=650)
        content = box(22)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(26)
        clamp.set_child(content)
        scroll.set_child(clamp)
        self.stack.add_titled_with_icon(scroll, name, title, icon)
        return content

    def make_home(self):
        page = self.page("home", "概览", "go-home-symbolic")
        hero = box(12)
        hero.add_css_class("hero")
        hero.append(label("LOCAL VOICE · 本地语音识别", "eyebrow"))
        self.state_label = label("正在连接…", "hero-title")
        self.engine_label = label("读取识别服务与模型状态", "hero-subtitle", True)
        hero.append(self.state_label)
        hero.append(self.engine_label)
        page.append(hero)

        service = Adw.PreferencesGroup(title="识别服务", description="后台运行即可听写，无需一直打开工作台。")
        self.service_row = Adw.ActionRow(title="服务状态", subtitle="正在检查")
        self.start_button = button("启动服务", lambda _: self.service_action("restart" if self.status_pair and self.status_pair[0].get("ActiveState") == "active" else "start"), True)
        self.stop_button = button("停止", lambda _: self.service_action("stop"))
        self.service_row.add_suffix(self.stop_button)
        self.service_row.add_suffix(self.start_button)
        service.add(self.service_row)
        self.startup_row = Adw.ActionRow(title="登录后自动启动", subtitle="正在读取")
        self.startup_button = button("设置", lambda _: self.toggle_startup())
        self.startup_row.add_suffix(self.startup_button)
        service.add(self.startup_row)
        page.append(service)

        group = Adw.PreferencesGroup(title="开始中文听写", description="本机 Omarchy 已绑定以下快捷键。")
        for index, title, subtitle in [
            ("01", "把光标放进输入框", "切换到聊天、笔记或编辑器，让文字输入到正确的位置。"),
            ("02", "按住 F9 说话，松开后识别", "也可按 Super + Ctrl + X 开始，再按一次结束。"),
            ("03", "等待文字出现", "长录音请使用会议模式；普通听写有单次时长限制。"),
        ]:
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.add_prefix(label(index, "step"))
            group.add(row)
        page.append(group)
        page.append(button("选择适合自己的模型 →", lambda _: self.stack.set_visible_child_name("models")))

    def make_models(self):
        page = self.page("models", "模型", "folder-download-symbolic")
        page.append(label("选一个适合你的声音", "page-title"))
        page.append(label("下载不会改变当前模型。下载完成后，点击“启用”才会切换。", "muted", True))
        self.model_box = box(14)
        page.append(self.model_box)
        page.append(label("这里只展示中文常用模型；其他模型仍可通过原版命令行管理。下载大小为近似值。", "muted", True))
        self.refresh_models_button = button("刷新模型列表", lambda _: self.refresh_snapshot())
        page.append(self.refresh_models_button)

    def render_models(self):
        child = self.model_box.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.model_box.remove(child)
            child = following
        self.model_controls.clear()
        config = self.snapshot_data["config"]
        catalog = self.snapshot_data["models"]["engines"]
        compiled = {e["name"] for e in self.snapshot_data["engines"] if e["compiled"]}
        for engine, model, title, tag, description, size in MODELS:
            entries = catalog.get(engine, {}).get("models", [])
            info = next((m for m in entries if m["name"] == model), None)
            if not info:
                continue
            current = config.get("engine") == engine and config.get(f"{engine}.model") in (model, info["download_arg"])
            card = box(12)
            card.add_css_class("card")
            card.add_css_class("model-card")
            row = box(12, True)
            heading = label(title, "card-title")
            heading.set_hexpand(True)
            row.append(heading)
            row.append(label("当前使用" if current else tag, "pill"))
            card.append(row)
            card.append(label(description, wrap=True))
            footer = box(12, True)
            state = "已下载" if info["installed"] else "尚未下载"
            caption = label(f"{size}  ·  {state}", "muted")
            caption.set_hexpand(True)
            footer.append(caption)
            if current and info["installed"]:
                control = button("使用中", lambda _: None)
                control.set_sensitive(False)
            elif engine not in compiled:
                control = button("需 ONNX 后端", lambda _: self.fail("请在终端执行：sudo voxtype setup onnx --enable\n完成后刷新模型列表。"))
            elif info["installed"]:
                control = button("启用", lambda _, e=engine, m=model: self.activate_model(e, m), True)
            else:
                control = button("下载模型", lambda _, arg=info["download_arg"]: self.download_model(arg), True)
            footer.append(control)
            if not (current and info["installed"]):
                self.model_controls.append(control)
                control.set_sensitive(not self.busy)
            card.append(footer)
            self.model_box.append(card)

    def combo(self, group, title, choices, subtitle=None):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(choices))
        if subtitle:
            row.set_subtitle(subtitle)
        row.connect("notify::selected", self.mark_dirty)
        group.add(row)
        return row

    def make_settings(self):
        page = self.page("settings", "设置", "preferences-system-symbolic")
        page.append(label("按你的习惯听写", "page-title"))
        self.settings_group = box(22)
        page.append(self.settings_group)
        audio = Adw.PreferencesGroup(title="声音输入")
        self.device_combo = self.combo(audio, "麦克风", ["跟随系统默认"], "插拔耳机后，默认项会跟随系统选择。")
        self.device_button = button("查找音频设备", lambda _: self.load_devices())
        audio.add(self.device_button)
        self.duration = Adw.SpinRow.new_with_range(5, 600, 5)
        self.duration.set_title("单次听写上限（秒）")
        self.duration.set_subtitle("长会议建议使用会议模式。")
        self.duration.connect("notify::value", self.mark_dirty)
        audio.add(self.duration)
        self.pause = Adw.SwitchRow(title="录音时暂停媒体播放")
        self.pause.connect("notify::active", self.mark_dirty)
        audio.add(self.pause)
        self.settings_group.append(audio)
        output = Adw.PreferencesGroup(title="文字输出")
        self.output = self.combo(output, "输出方式", ["输入到光标位置", "复制到剪贴板", "复制后自动粘贴"])
        self.language = self.combo(output, "识别语言", ["自动识别", "普通话", "英语", "粤语", "日语", "韩语"], "自动识别适合中英夹杂。可选语言随引擎变化。")
        self.settings_group.append(output)
        self.save_button = button("保存并应用", lambda _: self.save_settings(), True)
        self.save_button.set_halign(Gtk.Align.END)
        page.append(self.save_button)
        page.append(label("应用前自动备份配置；服务运行时会重启以加载设置。录音或转写期间无法应用。", "muted", True))

    def mark_dirty(self, *_):
        if not self.loading:
            self.dirty = True
            self.save_button.set_sensitive(not self.busy)

    def load_settings(self):
        config = self.snapshot_data["config"]
        self.loading = True
        device = config.get("audio.device", "default")
        self.device_names = list(dict.fromkeys(["default", device]))
        self.device_combo.set_model(Gtk.StringList.new(["跟随系统默认" if d == "default" else d for d in self.device_names]))
        self.device_combo.set_selected(self.device_names.index(device))
        duration = config.get("audio.max_duration_secs", 60)
        self.duration.get_adjustment().set_upper(max(600, duration))
        self.duration.set_value(duration)
        self.pause.set_active(config.get("audio.pause_media", False))
        self.output_values = ["type", "clipboard", "paste"]
        output_mode = config.get("output.mode", "type")
        output_labels = ["输入到光标位置", "复制到剪贴板", "复制后自动粘贴"]
        if output_mode not in self.output_values:
            self.output_values.append(output_mode)
            output_labels.append(f"保留当前模式（{output_mode}）")
        self.output.set_model(Gtk.StringList.new(output_labels))
        self.output.set_selected(self.output_values.index(output_mode))
        self.language_engine = config.get("engine")
        self.language_values = ["auto", "zh", "en", "yue", "ja", "ko"] if self.language_engine == "sensevoice" else ["auto", "zh", "en", "ja", "ko"]
        names = {"auto": "自动识别", "zh": "普通话", "en": "英语", "yue": "粤语", "ja": "日语", "ko": "韩语"}
        lang = config.get(f"{self.language_engine}.language", "auto")
        if lang not in self.language_values:
            self.language_values.append(lang)
        self.language.set_model(Gtk.StringList.new([names.get(v, v) for v in self.language_values]))
        self.language.set_selected(self.language_values.index(lang))
        self.language.set_sensitive(self.language_engine in ("whisper", "sensevoice"))
        self.loading = False
        self.dirty = False
        self.settings_group.set_sensitive(not self.busy)
        self.save_button.set_sensitive(False)

    def save_settings(self):
        values = {"audio.device": self.device_names[self.device_combo.get_selected()], "audio.max_duration_secs": int(self.duration.get_value()), "audio.pause_media": self.pause.get_active(), "output.mode": self.output_values[self.output.get_selected()]}
        if self.language_engine in ("whisper", "sensevoice"):
            values[f"{self.language_engine}.language"] = self.language_values[self.language.get_selected()]
        self.task("正在保存设置并应用…", lambda: self.backend.set_values(values), lambda _: self.saved())

    def saved(self):
        self.dirty = False
        self.toast.add_toast(Adw.Toast(title="设置已保存并应用"))
        self.refresh_snapshot()

    def load_devices(self):
        self.task("正在查找音频设备…", lambda: self.backend.data("info", "devices"), self.devices_loaded)

    def devices_loaded(self, devices):
        current = self.device_names[self.device_combo.get_selected()]
        self.device_names = list(dict.fromkeys(["default", current] + [d["name"] for d in devices]))
        self.loading = True
        self.device_combo.set_model(Gtk.StringList.new(["跟随系统默认" if d == "default" else d for d in self.device_names]))
        self.device_combo.set_selected(self.device_names.index(current))
        self.loading = False

    def activate_model(self, engine, model):
        if self.dirty:
            self.fail("设置页有尚未保存的修改，请先保存，再切换模型。")
            return
        self.task("正在切换模型并等待服务就绪…", lambda: self.backend.activate(engine, model), lambda _: self.saved())

    def download_model(self, arg):
        self.is_downloading = True
        self.backend.download_cancel.clear()
        self.cancel_button.set_visible(True)
        self.task("正在下载模型，完成后可点击启用…", lambda: self.backend.download(arg, self.download_progress), lambda _: self.download_done())

    def download_progress(self, event):
        GLib.idle_add(self.progress_event, event)

    def progress_event(self, event):
        percent = event.get("pct")
        if isinstance(percent, (int, float)):
            self.progress.set_fraction(max(0, min(1, percent / 100)))
            self.operation_text.set_text(f"正在下载 {event.get('model', '')} · 当前文件 {percent:.1f}%")
        else:
            self.progress.pulse()
        if event.get("event") == "error":
            self.operation_text.set_text("下载遇到问题，正在等待退出…")
        return False

    def download_done(self):
        self.toast.add_toast(Adw.Toast(title="下载完成，请点击启用以切换模型"))
        self.refresh_snapshot()

    def service_action(self, action):
        self.task("正在更新服务状态…", lambda: self.backend.control(action), lambda _: self.poll())

    def toggle_startup(self):
        enabled = self.status_pair and self.status_pair[0].get("UnitFileState") == "enabled"
        self.service_action("disable" if enabled else "enable")

    def set_busy(self, busy):
        self.busy = busy
        self.operation.set_visible(busy)
        for control in [self.start_button, self.stop_button, self.startup_button, self.refresh_models_button, self.settings_group, *self.model_controls]:
            control.set_sensitive(not busy)
        self.settings_group.set_sensitive(not busy and self.snapshot_data is not None)
        self.save_button.set_sensitive(not busy and self.dirty)
        if not busy:
            self.is_downloading = False
            self.cancel_button.set_visible(False)
            if self.status_pair:
                self.render_status(self.status_pair)

    def task(self, message, work, done=None):
        if self.busy:
            return
        self.error.set_visible(False)
        self.operation_text.set_text(message)
        self.progress.set_fraction(0)
        self.set_busy(True)

        def worker():
            try:
                result = work()
                GLib.idle_add(finish, result, None)
            except Exception as exc:
                GLib.idle_add(finish, None, str(exc))

        def finish(result, error):
            self.set_busy(False)
            if error:
                self.fail(error)
            elif done:
                done(result)
            return False

        threading.Thread(target=worker, daemon=True).start()

    def fail(self, text):
        self.error.set_text(text)
        self.error.set_visible(True)

    def refresh_snapshot(self):
        def ready(snapshot):
            self.snapshot_data = snapshot
            self.render_models()
            if not self.dirty:
                self.load_settings()
            self.poll()
        self.task("正在读取配置和本地模型…", self.backend.snapshot, ready)

    def poll(self):
        if self.polling:
            return True
        self.polling = True

        def worker():
            try:
                pair, error = self.backend.status(), None
            except Exception as exc:
                pair, error = None, str(exc)
            GLib.idle_add(finish, pair, error)

        def finish(pair, error):
            self.polling = False
            if error:
                self.state_label.set_text("暂时无法读取状态")
                self.engine_label.set_text("可打开诊断查看详情，或稍后重试。")
            else:
                self.status_pair = pair
                self.render_status(pair)
            return False
        threading.Thread(target=worker, daemon=True).start()
        return True

    def render_status(self, pair):
        service, status = pair
        active = service.get("ActiveState") == "active"
        phase = status.get("class", "stopped")
        names = {"idle": "准备好了，开始说话吧", "recording": "正在聆听…", "transcribing": "正在识别，请稍候", "streaming": "正在实时识别…", "stopped": "识别服务尚未启动"}
        self.state_label.set_text(names.get(phase, "正在准备识别服务…"))
        self.engine_label.set_text(f"{status.get('model', '当前模型')}  ·  {status.get('backend', '本地识别')}" if active else "启动服务后，即可用快捷键进行语音输入。")
        self.service_row.set_subtitle("运行中" if active else f"未运行 · {service.get('SubState', 'unknown')}")
        enabled = service.get("UnitFileState") == "enabled"
        self.startup_row.set_subtitle("已开启" if enabled else "未开启")
        self.startup_button.set_label("关闭自启" if enabled else "开启自启")
        self.start_button.set_label("重新启动" if active else "启动服务")
        # The click action derives its real action from the latest service state.
        self.stop_button.set_sensitive(active and not self.busy and phase not in ("recording", "transcribing", "streaming"))
        self.start_button.set_sensitive(not self.busy and phase not in ("recording", "transcribing", "streaming"))

    def show_logs(self):
        def show(logs):
            dialog = Adw.Window(transient_for=self, modal=True, title="诊断日志", default_width=780, default_height=520)
            content = box(0)
            content.append(Adw.HeaderBar())
            hint = label("这是最近 65 条日志的快照，不会自动滚动。", "muted")
            hint.set_margin_top(12)
            hint.set_margin_bottom(12)
            hint.set_margin_start(16)
            content.append(hint)
            view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            view.get_buffer().set_text(logs or "暂无服务日志")
            scroll = Gtk.ScrolledWindow(vexpand=True)
            scroll.set_child(view)
            content.append(scroll)
            dialog.set_content(content)
            dialog.present()
        self.task("正在读取诊断日志…", self.backend.logs, show)

    def on_close(self, *_):
        if self.busy:
            self.fail("操作尚未结束，请等待完成；下载可先点击“取消下载”。")
            return True
        if self.dirty:
            dialog = Adw.AlertDialog(heading="放弃未保存的设置？", body="已运行的语音识别服务不受影响。")
            dialog.add_response("cancel", "继续编辑")
            dialog.add_response("discard", "放弃修改")
            dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect("response", lambda _, response: self.discard_close() if response == "discard" else None)
            dialog.present(self)
            return True
        GLib.source_remove(self.timer)
        return False

    def discard_close(self):
        self.dirty = False
        self.close()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.voxtype.ControlCenter", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        page_action = Gio.SimpleAction.new("page", GLib.VariantType.new("s"))
        page_action.connect("activate", self.open_page)
        self.add_action(page_action)

    def open_page(self, _action, parameter):
        name = parameter.get_string()
        if name in ("home", "models", "settings"):
            self.activate()
            self.get_active_window().stack.set_visible_child_name(name)

    def do_activate(self):
        window = self.get_active_window()
        if not window:
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            window = Window(self)
        window.present()


if __name__ == "__main__":
    raise SystemExit(App().run(sys.argv))
