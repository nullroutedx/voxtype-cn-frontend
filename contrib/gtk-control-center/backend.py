"""Small, testable adapter to Voxtype's public CLI (no GTK dependency)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlsplit


class BackendError(RuntimeError):
    pass


class Backend:
    def __init__(self, binary="voxtype", config_path=None):
        self.binary = binary
        self.config_path = Path(config_path or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxtype/config.toml")
        self.lock = threading.Lock()
        self.download_cancel = threading.Event()
        self.gui_config = self.config_path.with_name("gui.json")
        self.download_log = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "voxtype-gui/download.log"

    def get_download_proxy(self):
        if not self.gui_config.exists():
            return ""
        try:
            value = json.loads(self.gui_config.read_text()).get("download_proxy", "")
            return self.validate_proxy(value)
        except (ValueError, OSError, AttributeError) as exc:
            raise BackendError("无法读取下载代理设置，请检查 gui.json。") from exc

    @staticmethod
    def validate_proxy(value):
        if not isinstance(value, str):
            raise BackendError("下载代理必须是一个地址")
        value = value.strip()
        if not value:
            return ""
        try:
            parsed = urlsplit(value)
            valid = (parsed.scheme in ("http", "https", "socks5", "socks5h")
                     and parsed.hostname and parsed.port
                     and not parsed.username and not parsed.password
                     and parsed.path in ("", "/") and not parsed.query and not parsed.fragment
                     and not any(c.isspace() for c in value))
        except ValueError:
            valid = False
        if not valid:
            raise BackendError("请输入带端口的代理地址，例如 http://127.0.0.1:7897；暂不支持带账号密码的地址。")
        return value

    def set_download_proxy(self, value):
        value = self.validate_proxy(value)
        with self.lock:
            try:
                config = json.loads(self.gui_config.read_text()) if self.gui_config.exists() else {}
                config["download_proxy"] = value
                self.gui_config.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.gui_config.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
                temporary.chmod(0o600)
                temporary.replace(self.gui_config)
            except (OSError, ValueError, TypeError) as exc:
                raise BackendError("保存下载代理设置失败") from exc

    def download_environment(self):
        env = os.environ.copy()
        proxy = self.get_download_proxy()
        if proxy:
            for key in list(env):
                if key.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                    del env[key]
            env.update(http_proxy=proxy, https_proxy=proxy, all_proxy=proxy)
        return env

    def run(self, args, timeout=15):
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendError(f"命令未完成：{args[0]}\n{e}") from e
        if p.returncode:
            raise BackendError((p.stderr or p.stdout or f"退出代码 {p.returncode}").strip()[-4000:])
        return p.stdout

    def vox(self, *args, timeout=15):
        return self.run([self.binary, "--config", str(self.config_path), *args], timeout)

    def data(self, *args):
        try:
            return json.loads(self.vox(*args, "--json"))
        except json.JSONDecodeError as e:
            raise BackendError("Voxtype 返回了无法解析的数据，请查看诊断日志。") from e

    def service(self):
        raw = self.run(["systemctl", "--user", "show", "voxtype.service", "-p", "ActiveState", "-p", "SubState", "-p", "UnitFileState"])
        return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)

    def status(self):
        service = self.service()
        status = {"class": "stopped"}
        if service.get("ActiveState") == "active":
            try:
                status = json.loads(self.vox("status", "--format", "json", "--extended"))
            except json.JSONDecodeError as e:
                raise BackendError("无法解析服务状态") from e
        return service, status

    def snapshot(self):
        return {"config": self.data("config", "get"), "models": self.data("info", "models"), "engines": self.data("info", "engines"), "download_proxy": self.get_download_proxy()}

    def require_idle(self):
        service, status = self.status()
        if status.get("class") in ("recording", "transcribing", "streaming"):
            raise BackendError("请先结束当前录音或转写，再修改设置。")
        # Meeting recording may leave the normal dictation state at idle.
        if service.get("ActiveState") == "active":
            meeting = self.vox("meeting", "status")
            if not any(s in meeting.lower() for s in ("no active meeting", "no meeting currently in progress")):
                raise BackendError("请先结束会议录音，再修改设置。\n" + meeting)
        return service.get("ActiveState") == "active"

    def wait_ready(self):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            service, status = self.status()
            if service.get("ActiveState") == "active" and status.get("class") == "idle":
                return
            if service.get("ActiveState") == "failed":
                break
            time.sleep(0.5)
        raise BackendError("服务未能进入待命状态。请打开诊断日志查看原因。")

    def set_values(self, changes):
        """Apply one UI transaction; preserve comments and restore on failure."""
        with self.lock:
            running = self.require_idle()
            previous = self.config_path.read_bytes() if self.config_path.exists() else None
            backup = None
            if previous is not None:
                backup = self.config_path.with_name(f"config.toml.bak.gui-{time.time_ns()}")
                shutil.copy2(self.config_path, backup)
            try:
                for key, value in changes.items():
                    rendered = str(value).lower() if isinstance(value, bool) else str(value)
                    self.vox("config", "set", key, rendered)
                if running:
                    self.run(["systemctl", "--user", "restart", "voxtype.service"])
                    self.wait_ready()
            except Exception as original:
                if previous is None:
                    self.config_path.unlink(missing_ok=True)
                else:
                    self.config_path.write_bytes(previous)
                if running:
                    try:
                        self.run(["systemctl", "--user", "restart", "voxtype.service"])
                    except BackendError as restore_error:
                        raise BackendError(f"保存失败，配置已还原；服务恢复失败：{restore_error}\n原始错误：{original}") from original
                raise BackendError(f"保存失败，已还原原配置。\n{original}") from original
            return backup

    def activate(self, engine, model):
        snapshot = self.snapshot()
        compiled = {e["name"] for e in snapshot["engines"] if e["compiled"]}
        if engine not in compiled:
            raise BackendError("当前程序不支持这个引擎。请先在终端启用 ONNX 后端：sudo voxtype setup onnx --enable")
        models = snapshot["models"]["engines"].get(engine, {}).get("models", [])
        match = next((m for m in models if m["name"] == model), None)
        if not match or not match.get("installed"):
            raise BackendError("模型尚未完整下载，请先下载模型。")
        # SenseVoice's catalog uses 'small', while its config uses 'sensevoice-small'.
        config_name = match["download_arg"] if engine == "sensevoice" else match["name"]
        changes = {f"{engine}.model": config_name, "engine": engine}
        if engine == "whisper":
            changes.update({"whisper.language": "en" if model.endswith(".en") else "auto", "whisper.translate": False})
        elif engine == "sensevoice":
            changes["sensevoice.language"] = "auto"
        return self.set_values(changes)

    def control(self, action):
        if action not in ("start", "stop", "restart", "enable", "disable"):
            raise BackendError("不支持的服务操作")
        with self.lock:
            if action in ("stop", "restart"):
                self.require_idle()
            self.run(["systemctl", "--user", action, "voxtype.service"])
            if action in ("start", "restart"):
                self.wait_ready()

    def logs(self):
        try:
            service_log = self.run(["journalctl", "--user", "-u", "voxtype", "-n", "65", "--no-pager", "-o", "short"])
        except BackendError as exc:
            service_log = str(exc)
        download_log = self.download_log.read_text() if self.download_log.exists() else "暂无下载记录"
        return "=== 最近一次模型下载 ===\n" + download_log[-16000:] + "\n\n=== 识别服务 ===\n" + service_log

    @staticmethod
    def transient_download_error(message):
        # curl normally doesn't retry TLS EOF (35), which caused the observed failure.
        codes = re.findall(r"(?:exit(?:ed)?(?: with)? code|curl:)\s*\(?([0-9]+)", message)
        return bool(set(codes) & {"5", "6", "7", "18", "28", "35", "52", "55", "56"})

    @staticmethod
    def explain_download_error(message):
        if "35" in message and ("TLS" in message or "SSL" in message):
            return "与模型服务器的加密连接中断。可在模型页填写本机代理地址，然后重新下载。"
        if "Could not resolve" in message:
            return "无法解析模型服务器地址。请检查网络或下载代理。"
        if "timed out" in message.lower() or "Timeout" in message:
            return "模型下载连接超时，请检查网络或下载代理。"
        if "404" in message:
            return "模型服务器没有这个文件（404）。请稍后重试或检查 Voxtype 版本。"
        return "模型下载未完成，请打开诊断查看具体原因。"

    def download(self, download_arg, progress):
        """Download only. Activation is an explicit, separate operation."""
        with self.lock:
            allowed = {m["download_arg"] for v in self.data("info", "models")["engines"].values() for m in v["models"] if m.get("downloadable")}
            if download_arg not in allowed:
                raise BackendError("模型不在当前版本的下载列表里")
            env = self.download_environment()
            self.download_log.parent.mkdir(parents=True, exist_ok=True)
            self.download_log.write_text(f"模型：{download_arg}\n")
            self.download_log.chmod(0o600)
            for attempt in range(2):
                if self.download_cancel.is_set():
                    raise BackendError("下载已取消，当前使用的模型未改变。")
                code, detail = self._download_once(download_arg, progress, env)
                if self.download_cancel.is_set():
                    raise BackendError("下载已取消，当前使用的模型未改变。")
                if code == 0:
                    return
                if attempt == 0 and self.transient_download_error(detail):
                    progress({"event": "retry", "model": download_arg})
                    if self.download_cancel.wait(2):
                        raise BackendError("下载已取消，当前使用的模型未改变。")
                    continue
                raise BackendError(self.explain_download_error(detail) + "\n详细错误已保存在“诊断”中。")

    def _download_once(self, download_arg, progress, env):
        with subprocess.Popen([self.binary, "--config", str(self.config_path), "setup", "--download", "--model", download_arg, "--progress-format", "json"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True, env=env) as proc:
            finished = threading.Event()

            def cancel_watch():
                while not finished.wait(0.2):
                    if self.download_cancel.is_set():
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                            if not finished.wait(2):
                                os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return

            threading.Thread(target=cancel_watch, daemon=True).start()
            tail = []
            last_error = ""
            try:
                for line in proc.stdout:
                    tail.append(line.strip())
                    tail = tail[-15:]
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = None
                    if isinstance(event, dict):
                        if event.get("event") == "error":
                            last_error = str(event.get("message", ""))
                        progress(event)
                    # Progress is displayed live; keep diagnostics small and free of JSON noise.
                    if not isinstance(event, dict) or event.get("event") in ("error", "done"):
                        with self.download_log.open("a") as log:
                            log.write((str(event.get("message", line.strip())) if event else line.strip()) + "\n")
                code = proc.wait()
            finally:
                finished.set()
            return code, last_error or "\n".join(tail)
