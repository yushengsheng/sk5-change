from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

SITE_BUY_URL = "http://www.gzsk5.com/#/buymain?cid=24"
SITE_ORDER_URL = "http://www.gzsk5.com/#/orderdetail"
API_BASE_URL = "http://api.gzsk5.com"
CID = 24
LOGIN_WAIT_SECONDS = 300
DEFAULT_TEST_URL = "https://www.usnbweb.red"
DEFAULT_MAX_LATENCY_MS = 1000
DEFAULT_MAX_EXCHANGE_COUNT = 20
DEFAULT_SETTLE_SECONDS = 10
AUTO_BIND_IP_TEXT = "自动"


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
LEGACY_PROFILE_DIR = APP_DIR / "browser_profile"
PROFILE_DIR = LEGACY_PROFILE_DIR
ACCOUNT_PROFILE_ROOT = APP_DIR / "account_profiles"
ACCOUNTS_FILE = APP_DIR / "ip_exchange_accounts.json"
RUN_LOG_FILE = APP_DIR / "run_results.log"
CONFIG_FILE = APP_DIR / "ip_exchange_config.json"

if getattr(sys, "frozen", False):
    bundled_browser_candidates = [
        APP_DIR / "ms-playwright",
        Path(getattr(sys, "_MEIPASS", "")) / "ms-playwright",
    ]
    for bundled_browsers in bundled_browser_candidates:
        if bundled_browsers.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))
            break


@dataclass
class ProxyLine:
    ip: str
    port: str
    username: str
    password: str
    expires_at: str
    region: str = ""
    test_status: str = ""

    @classmethod
    def parse(cls, line: str) -> "ProxyLine":
        parts = [item.strip() for item in line.strip().split("|")]
        if len(parts) != 5 or not all(parts):
            raise ValueError("格式应为：ip|端口|用户名|密码|到期时间")
        if not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", parts[0]):
            raise ValueError("IP 格式不正确")
        if not parts[1].isdigit():
            raise ValueError("端口必须是数字")
        return cls(*parts)

    def as_line(self) -> str:
        return "|".join([self.ip, self.port, self.username, self.password, self.expires_at])

    def display_region(self) -> str:
        return self.region or "-"

    def display_test_status(self) -> str:
        return self.test_status or "未测试"


@dataclass
class ProxyTestResult:
    ok: bool
    latency_ms: int | None
    status: int | None
    message: str


@dataclass
class ProxyWorkItem:
    index: int
    original: ProxyLine
    current: ProxyLine
    changes: int = 0


@dataclass
class AccountProfile:
    id: str
    name: str
    profile_subdir: str
    created_at: str
    updated_at: str
    last_used_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountProfile | None":
        account_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        profile_subdir = str(data.get("profile_subdir") or "").strip()
        if not account_id or not name or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            return None

        if not profile_subdir:
            profile_subdir = str(Path(ACCOUNT_PROFILE_ROOT.name) / account_id)
        profile_path = Path(profile_subdir)
        if profile_path.is_absolute() or ".." in profile_path.parts:
            return None

        created_at = str(data.get("created_at") or current_timestamp())
        updated_at = str(data.get("updated_at") or created_at)
        last_used_at = str(data.get("last_used_at") or "")
        return cls(account_id, name, profile_subdir, created_at, updated_at, last_used_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "name": self.name,
            "profile_subdir": self.profile_subdir,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_used_at": self.last_used_at,
        }

    def profile_dir(self) -> Path:
        return APP_DIR / self.profile_subdir


class StopRequested(Exception):
    pass


def current_timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def append_run_log(kind: str, message: str) -> None:
    timestamp = current_timestamp()
    try:
        with RUN_LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(f"[{timestamp}] [{kind}] {message}\n")
    except Exception:
        pass


class Gzsk5Exchanger:
    def __init__(
        self,
        log: Callable[[str], None],
        profile_dir: Path,
        account_name: str,
    ) -> None:
        self.log = log
        self.profile_dir = profile_dir
        self.account_name = account_name

    @staticmethod
    def _load_sync_playwright() -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "当前 Python 环境未安装 playwright。请运行：pip install -r requirements.txt"
            ) from exc
        return sync_playwright

    def open_login_page(self, stop_event: threading.Event | None = None) -> None:
        sync_playwright = self._load_sync_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, visible=True)
            try:
                page = self._get_page(context)
                self.log(f"已打开账号“{self.account_name}”的登录页。")
                token = self._ensure_login(page, interactive=True, stop_event=stop_event)
                self.log(f"已检测到登录态：{token[:6]}***")
                self.log(f"账号“{self.account_name}”登录态已保存，之后可直接调换。")
            finally:
                context.close()

    def exchange(
        self,
        proxy: ProxyLine,
        mode: str,
        stop_event: threading.Event | None = None,
    ) -> ProxyLine:
        sync_playwright = self._load_sync_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, visible=(mode != "api"))
            try:
                page = self._get_page(context)
                token = self._ensure_login(
                    page,
                    interactive=(mode != "api"),
                    stop_event=stop_event,
                )

                if mode == "api":
                    result = self._exchange_by_api(page, token, proxy)
                else:
                    result = self._exchange_by_page_click(page, token, proxy)
            finally:
                context.close()

        return result

    def exchange_many(
        self,
        proxies: list[ProxyLine],
        mode: str,
        on_result: Callable[[int, ProxyLine, ProxyLine | None, Exception | None], None],
        stop_event: threading.Event | None = None,
        on_status: Callable[[int, str], None] | None = None,
    ) -> None:
        sync_playwright = self._load_sync_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, visible=(mode != "api"))
            try:
                page = self._get_page(context)
                token = self._ensure_login(
                    page,
                    interactive=(mode != "api"),
                    stop_event=stop_event,
                )

                for index, proxy in enumerate(proxies, start=1):
                    self._raise_if_stopped(stop_event)
                    self._emit_status(on_status, index, "更换中")
                    try:
                        if mode == "api":
                            result = self._exchange_by_api(page, token, proxy)
                        else:
                            result = self._exchange_by_page_click(page, token, proxy)
                        self._emit_status(on_status, index, "更换成功")
                        on_result(index, proxy, result, None)
                    except Exception as exc:
                        self._emit_status(on_status, index, "更换失败")
                        on_result(index, proxy, None, exc)
            finally:
                context.close()

    def exchange_many_until_pass(
        self,
        proxies: list[ProxyLine],
        target_url: str,
        max_latency_ms: int,
        max_exchange_count: int,
        on_result: Callable[
            [int, ProxyLine, ProxyLine | None, ProxyTestResult | None, int, Exception | None],
            None,
        ],
        stop_event: threading.Event | None = None,
        local_bind_ip: str | None = None,
        on_status: Callable[[int, str], None] | None = None,
        settle_seconds: int = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        retry_queue: queue.Queue[ProxyWorkItem] = queue.Queue()
        test_threads: list[threading.Thread] = []
        active_tests = 0
        active_tests_lock = threading.Lock()

        def increase_active_tests() -> None:
            nonlocal active_tests
            with active_tests_lock:
                active_tests += 1

        def decrease_active_tests() -> None:
            nonlocal active_tests
            with active_tests_lock:
                active_tests -= 1

        def has_active_tests() -> bool:
            with active_tests_lock:
                return active_tests > 0

        def drain_retry_queue(exchange_queue: list[ProxyWorkItem]) -> None:
            while True:
                try:
                    exchange_queue.append(retry_queue.get_nowait())
                except queue.Empty:
                    break

        def wait_then_test(item: ProxyWorkItem) -> None:
            try:
                for remaining in range(settle_seconds, 0, -1):
                    self._raise_if_stopped(stop_event)
                    self._emit_status(on_status, item.index, f"等待 {remaining}秒")
                    self._sleep_with_stop(1, stop_event)

                self._raise_if_stopped(stop_event)
                self._emit_status(on_status, item.index, "测试中")
                test_result = self._test_proxy_connectivity(
                    playwright=None,
                    proxy=item.current,
                    target_url=target_url,
                    max_latency_ms=max_latency_ms,
                    stop_event=stop_event,
                    local_bind_ip=local_bind_ip,
                )

                if test_result.ok:
                    self._emit_status(on_status, item.index, "测试合格")
                    on_result(
                        item.index,
                        item.original,
                        item.current,
                        test_result,
                        item.changes,
                        None,
                    )
                    return

                reason = self._format_test_result(test_result)
                if item.changes >= max_exchange_count:
                    self._emit_status(on_status, item.index, "测试失败")
                    on_result(
                        item.index,
                        item.original,
                        item.current,
                        test_result,
                        item.changes,
                        RuntimeError(
                            f"达到最大更换次数 {max_exchange_count}，最后一次测试仍不合格：{reason}"
                        ),
                    )
                    return

                self.log(
                    f"[{item.index}/{len(proxies)}] 新 IP 不合格：{item.current.as_line()}，"
                    f"{reason}；进入调换队列排队。"
                )
                self._emit_status(on_status, item.index, "排队更换")
                retry_queue.put(item)
            except StopRequested:
                pass
            except Exception as exc:
                self._emit_status(on_status, item.index, "测试失败")
                on_result(
                    item.index,
                    item.original,
                    item.current,
                    None,
                    item.changes,
                    exc,
                )
            finally:
                decrease_active_tests()

        def start_wait_and_test(item: ProxyWorkItem) -> None:
            increase_active_tests()
            thread = threading.Thread(target=wait_then_test, args=(item,), daemon=True)
            test_threads.append(thread)
            thread.start()

        sync_playwright = self._load_sync_playwright()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright, visible=False)
            try:
                page = self._get_page(context)
                token = self._ensure_login(page, interactive=False, stop_event=stop_event)
                exchange_queue: list[ProxyWorkItem] = []

                for index, original in enumerate(proxies, start=1):
                    self._raise_if_stopped(stop_event)
                    self._emit_status(on_status, index, "查询订单")
                    try:
                        current = self._query_current_proxy(page, token, original)
                    except Exception as exc:
                        self._emit_status(on_status, index, "查询失败")
                        on_result(index, original, None, None, 0, exc)
                        continue

                    if current.as_line() != original.as_line():
                        self.log(
                            f"[{index}/{len(proxies)}] 订单当前代理为：{current.as_line()}"
                        )

                    exchange_queue.append(
                        ProxyWorkItem(index=index, original=original, current=current)
                    )
                    self._emit_status(on_status, index, "待更换")

                self.log(
                    f"流水线模式：调换接口串行执行；每条调换成功后独立等待 "
                    f"{settle_seconds} 秒并并发测试。"
                )

                while exchange_queue or has_active_tests() or not retry_queue.empty():
                    self._raise_if_stopped(stop_event)
                    drain_retry_queue(exchange_queue)

                    if not exchange_queue:
                        self._sleep_with_stop(0.2, stop_event)
                        continue

                    item = exchange_queue.pop(0)
                    if item.changes >= max_exchange_count:
                        self._emit_status(on_status, item.index, "更换失败")
                        on_result(
                            item.index,
                            item.original,
                            item.current if item.changes else None,
                            None,
                            item.changes,
                            RuntimeError(f"达到最大更换次数 {max_exchange_count}，仍未获得可测试的新 IP"),
                        )
                        continue

                    self.log(
                        f"[{item.index}/{len(proxies)}] 第 {item.changes + 1} 次随机调换，基于：{item.current.as_line()}"
                    )
                    self._emit_status(
                        on_status,
                        item.index,
                        f"更换中 {item.changes + 1}/{max_exchange_count}",
                    )
                    try:
                        item.current = self._exchange_by_api(page, token, item.current)
                        item.changes += 1
                        self._emit_status(on_status, item.index, f"等待 {settle_seconds}秒")
                        start_wait_and_test(item)
                    except Exception as exc:
                        self._emit_status(on_status, item.index, "更换失败")
                        on_result(item.index, item.original, None, None, item.changes, exc)
            finally:
                if stop_event is not None and stop_event.is_set():
                    for thread in test_threads:
                        thread.join(timeout=2)
                else:
                    for thread in test_threads:
                        thread.join()
                context.close()

    @staticmethod
    def _emit_status(
        on_status: Callable[[int, str], None] | None,
        index: int,
        status: str,
    ) -> None:
        if on_status is not None:
            on_status(index, status)

    def _test_proxy_connectivity(
        self,
        playwright: Any,
        proxy: ProxyLine,
        target_url: str,
        max_latency_ms: int,
        stop_event: threading.Event | None = None,
        local_bind_ip: str | None = None,
    ) -> ProxyTestResult:
        timeout_ms = max(5_000, max_latency_ms + 2_000)
        curl_path = self._find_curl_exe()
        attempts = self._build_curl_attempts(curl_path, proxy, target_url, timeout_ms, local_bind_ip)
        failures: list[str] = []

        bind_text = f"，绑定本机网卡 {local_bind_ip}" if local_bind_ip else ""
        self.log(f"开始测速：{proxy.ip}:{proxy.port} -> {target_url}{bind_text}")

        for name, command in attempts:
            self._raise_if_stopped(stop_event)
            result = self._run_curl_attempt(command, stop_event, timeout_ms)

            if result.ok:
                self.log(f"测速通道 {name} 成功：{result.message}")
                if result.latency_ms is not None and result.latency_ms > max_latency_ms:
                    return ProxyTestResult(
                        ok=False,
                        latency_ms=result.latency_ms,
                        status=result.status,
                        message=f"延迟 {result.latency_ms}ms 高于阈值 {max_latency_ms}ms（{name}）",
                    )
                return ProxyTestResult(
                    ok=True,
                    latency_ms=result.latency_ms,
                    status=result.status,
                    message=f"{result.message}（{name}）",
                )

            failures.append(f"{name}: {result.message}")
            self.log(f"测速通道 {name} 失败：{result.message}")

        return ProxyTestResult(
            ok=False,
            latency_ms=None,
            status=None,
            message="不连通；" + "；".join(failures[-3:]),
        )

    @staticmethod
    def _raise_if_stopped(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise StopRequested("用户已停止任务")

    def _sleep_with_stop(
        self,
        seconds: int | float,
        stop_event: threading.Event | None,
    ) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self._raise_if_stopped(stop_event)
            time.sleep(min(0.2, max(0, deadline - time.time())))
        self._raise_if_stopped(stop_event)

    def _run_curl_attempt(
        self,
        command: list[str],
        stop_event: threading.Event | None,
        timeout_ms: int,
    ) -> ProxyTestResult:
        started = time.perf_counter()
        process: subprocess.Popen[str] | None = None
        timeout_seconds = max(7, int(timeout_ms / 1000) + 4)

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            while True:
                self._raise_if_stopped(stop_event)
                if process.poll() is not None:
                    break
                if time.perf_counter() - started > timeout_seconds:
                    process.kill()
                    stdout, stderr = process.communicate(timeout=2)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    return ProxyTestResult(
                        ok=False,
                        latency_ms=latency_ms,
                        status=None,
                        message=f"curl 超时：{stderr.strip() or stdout.strip() or timeout_seconds}",
                    )
                time.sleep(0.1)

            stdout, stderr = process.communicate(timeout=2)
            fields = self._parse_curl_metrics(stdout)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            latency_ms = self._curl_time_to_ms(fields.get("time_total"), elapsed_ms)
            status_text = fields.get("http_code") or "000"
            status = int(status_text) if status_text.isdigit() else None

            if process.returncode != 0 or status is None or status == 0:
                error_text = (
                    fields.get("errormsg")
                    or stderr.strip()
                    or stdout.strip()
                    or f"curl 退出码 {process.returncode}"
                )
                return ProxyTestResult(
                    ok=False,
                    latency_ms=latency_ms,
                    status=status,
                    message=f"不连通：{error_text}",
                )

            return ProxyTestResult(
                ok=True,
                latency_ms=latency_ms,
                status=status,
                message=f"连通，HTTP {status}，延迟 {latency_ms}ms",
            )
        except StopRequested:
            if process is not None and process.poll() is None:
                process.kill()
            raise
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            return ProxyTestResult(
                ok=False,
                latency_ms=latency_ms,
                status=None,
                message=f"不连通：{exc}",
            )

    @staticmethod
    def _build_socks_proxy_url(proxy: ProxyLine) -> str:
        username = quote(proxy.username, safe="")
        password = quote(proxy.password, safe="")
        return f"socks5h://{username}:{password}@{proxy.ip}:{proxy.port}"

    @staticmethod
    def _build_socks_proxy_url_local_dns(proxy: ProxyLine) -> str:
        username = quote(proxy.username, safe="")
        password = quote(proxy.password, safe="")
        return f"socks5://{username}:{password}@{proxy.ip}:{proxy.port}"

    @staticmethod
    def _build_proxy_user(proxy: ProxyLine) -> str:
        return f"{proxy.username}:{proxy.password}"

    @staticmethod
    def _find_curl_exe() -> str:
        candidates = [
            shutil.which("curl.exe"),
            r"C:\Windows\System32\curl.exe",
            r"C:\Windows\Sysnative\curl.exe",
            shutil.which("curl"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise RuntimeError("找不到 curl.exe，无法进行代理连通性测试")

    @staticmethod
    def detect_local_network_ipv4() -> tuple[str | None, str]:
        if os.name != "nt":
            return None, "自动检测本机真实网卡目前只支持 Windows"

        script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$vpnPattern = '(?i)vpn|tun|tap|wintun|wireguard|openvpn|clash|mihomo|sing-box|tailscale|zerotier|v2ray|hysteria|nekoray|sstap|virtual|hyper-v|vmware|virtualbox|loopback|docker'
$configs = Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.IPv4Address -and
    $_.NetAdapter.Status -eq 'Up' -and
    $_.InterfaceAlias -notmatch $vpnPattern -and
    $_.NetAdapter.InterfaceDescription -notmatch $vpnPattern
}
$rows = foreach ($cfg in $configs) {
    $metricObj = Get-NetIPInterface -InterfaceIndex $cfg.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1
    $metric = if ($metricObj) { [int]$metricObj.InterfaceMetric } else { 9999 }
    $gateway = ($cfg.IPv4DefaultGateway | Select-Object -First 1).NextHop
    foreach ($addr in $cfg.IPv4Address) {
        if ($addr.IPAddress -notlike '169.254.*') {
            [PSCustomObject]@{
                ip = $addr.IPAddress
                alias = $cfg.InterfaceAlias
                description = $cfg.NetAdapter.InterfaceDescription
                gateway = $gateway
                metric = $metric
                ifIndex = $cfg.InterfaceIndex
            }
            break
        }
    }
}
$rows | Sort-Object metric | Select-Object -First 1 | ConvertTo-Json -Compress
"""
        try:
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return None, f"检测本机真实网卡失败：{exc}"

        output = completed.stdout.strip()
        if completed.returncode != 0 or not output:
            detail = completed.stderr.strip() or "没有找到带默认网关的非 VPN 网卡"
            return None, detail

        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return None, f"网卡检测结果无法解析：{output[:120]}"

        if isinstance(data, list):
            data = data[0] if data else {}

        ip = str(data.get("ip", "")).strip() if isinstance(data, dict) else ""
        alias = str(data.get("alias", "")).strip() if isinstance(data, dict) else ""
        gateway = str(data.get("gateway", "")).strip() if isinstance(data, dict) else ""
        if not ip:
            return None, "没有找到可用的本机 IPv4"

        description = alias or "本机网卡"
        if gateway:
            description = f"{description}，网关 {gateway}"
        return ip, description

    def _build_curl_attempts(
        self,
        curl_path: str,
        proxy: ProxyLine,
        target_url: str,
        timeout_ms: int,
        local_bind_ip: str | None = None,
    ) -> list[tuple[str, list[str]]]:
        common = [
            curl_path,
            "--location",
            "--output",
            os.devnull,
            "--silent",
            "--show-error",
            "--insecure",
            "--write-out",
            "http_code=%{http_code}\ntime_total=%{time_total}\nremote_ip=%{remote_ip}\nerrormsg=%{errormsg}\n",
            "--connect-timeout",
            str(max(3, int(timeout_ms / 1000))),
            "--max-time",
            str(max(5, int(timeout_ms / 1000) + 2)),
        ]
        if local_bind_ip:
            common.extend(["--interface", local_bind_ip])
        proxy_host = f"{proxy.ip}:{proxy.port}"
        proxy_user = self._build_proxy_user(proxy)
        return [
            (
                "socks5h-url",
                common + ["--proxy", self._build_socks_proxy_url(proxy), target_url],
            ),
            (
                "socks5h-user",
                common + ["--socks5-hostname", proxy_host, "--proxy-user", proxy_user, target_url],
            ),
            (
                "socks5-url",
                common + ["--proxy", self._build_socks_proxy_url_local_dns(proxy), target_url],
            ),
            (
                "socks5-user",
                common + ["--socks5", proxy_host, "--proxy-user", proxy_user, target_url],
            ),
        ]

    @staticmethod
    def _parse_curl_metrics(output: str) -> dict[str, str]:
        metrics: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                metrics[key.strip()] = value.strip()
        return metrics

    @staticmethod
    def _curl_time_to_ms(value: str | None, fallback_ms: int) -> int:
        try:
            if value:
                return int(float(value) * 1000)
        except ValueError:
            pass
        return fallback_ms

    @staticmethod
    def _format_test_result(result: ProxyTestResult | None) -> str:
        if result is None:
            return "未获得测试结果"
        parts = [result.message]
        if result.status is not None and "HTTP" not in result.message:
            parts.append(f"HTTP {result.status}")
        if result.latency_ms is not None and "延迟" not in result.message:
            parts.append(f"延迟 {result.latency_ms}ms")
        return "，".join(parts)

    def _launch_context(self, playwright: Any, visible: bool) -> Any:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None

        for channel in ("msedge", "chrome", None):
            try:
                kwargs: dict[str, Any] = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": not visible,
                    "viewport": {"width": 1400, "height": 900},
                    "accept_downloads": True,
                    "ignore_https_errors": True,
                }
                if channel:
                    kwargs["channel"] = channel
                return playwright.chromium.launch_persistent_context(**kwargs)
            except Exception as exc:  # pragma: no cover - depends on local browsers
                last_error = exc

        raise RuntimeError(
            "无法启动浏览器。请先运行：python -m playwright install chromium\n"
            f"最后一次错误：{last_error}"
        )

    @staticmethod
    def _get_page(context: Any) -> Any:
        if context.pages:
            return context.pages[0]
        return context.new_page()

    def _ensure_login(
        self,
        page: Any,
        interactive: bool,
        stop_event: threading.Event | None = None,
    ) -> str:
        self._raise_if_stopped(stop_event)
        page.goto(SITE_BUY_URL, wait_until="domcontentloaded", timeout=60_000)
        token = self._get_local_storage(page, "api_token")
        if token:
            return token

        if not interactive:
            raise RuntimeError(
                "未检测到当前账号登录态。请先点击“更新当前登录态”，"
                "或点击“新增账号登录”完成一次登录。"
            )

        deadline = time.time() + LOGIN_WAIT_SECONDS
        told_user = False

        while time.time() < deadline:
            self._raise_if_stopped(stop_event)
            token = self._get_local_storage(page, "api_token")
            if token:
                return token

            if not told_user:
                self.log("未检测到登录态，请在打开的浏览器中登录网站。程序会自动继续等待。")
                told_user = True
            time.sleep(1)

        raise TimeoutError("等待登录超时，请重新点击“更新当前登录态”完成登录。")

    @staticmethod
    def _get_local_storage(page: Any, key: str) -> str:
        try:
            return page.evaluate("(key) => localStorage.getItem(key) || ''", key)
        except Exception:
            return ""

    def _exchange_by_page_click(self, page: Any, token: str, proxy: ProxyLine) -> ProxyLine:
        self.log(f"打开订单详情页，搜索用户名：{proxy.username}")
        page.goto(SITE_ORDER_URL, wait_until="domcontentloaded", timeout=60_000)

        confirmed = False
        try:
            search_input = page.locator("input[placeholder='请输入用户名']").first
            search_input.wait_for(timeout=30_000)
            search_input.fill(proxy.username)
            page.locator("button:has-text('搜索'):visible").first.click()

            row = self._wait_for_matching_row(page, proxy)
            self.log("已找到订单行，准备勾选。")
            row.locator(".el-checkbox__inner").first.click(force=True)

            self.log("选择“随机地区调换”。")
            page.locator("input[placeholder='调换ip']:visible").last.click()
            page.locator("li.el-select-dropdown__item:visible", has_text="随机地区调换").last.click()

            dialog = page.locator(".el-dialog:visible", has_text="随机地区调换").last
            dialog.wait_for(timeout=20_000)
            confirm_button = dialog.locator("button:has-text('确认调换')").first

            self.log("点击确认调换，等待网站返回新 IP。")
            with page.expect_response(
                lambda response: "/node/exchangeRandom" in response.url
                and response.request.method.upper() == "POST",
                timeout=120_000,
            ) as response_info:
                confirmed = True
                confirm_button.click()

            payload = self._response_json(response_info.value)
            return self._parse_exchange_response(payload, proxy)

        except Exception as exc:
            if confirmed:
                self.log(f"已点击确认，但未能读取调换响应：{exc}")
                self.log("改为查询订单当前状态，避免重复调换。")
                current = self._query_current_proxy(page, token, proxy)
                if current.ip != proxy.ip:
                    return current
                raise RuntimeError("确认调换后没有检测到新 IP，请在网页中核对订单状态。") from exc

            self.log(f"页面点击流程未完成：{exc}")
            self.log("切换为接口流程继续执行。")
            return self._exchange_by_api(page, token, proxy)

    def _wait_for_matching_row(self, page: Any, proxy: ProxyLine) -> Any:
        table_rows = page.locator(".el-table__body-wrapper tbody tr").filter(
            has_text=proxy.username
        )
        table_rows.first.wait_for(timeout=30_000)

        rows_with_ip = table_rows.filter(has_text=proxy.ip)
        if rows_with_ip.count() > 0:
            return rows_with_ip.first
        return table_rows.first

    def _exchange_by_api(self, page: Any, token: str, proxy: ProxyLine) -> ProxyLine:
        self.log(f"通过接口查询订单：{proxy.username}")
        target = self._find_order_detail(page, token, proxy)
        order_id = target.get("id")
        if not order_id:
            raise RuntimeError("订单记录里没有 id，无法调换。")

        self.log(f"找到订单 ID：{order_id}，发起随机地区调换。")
        payload = {
            "api_token": token,
            "ids": [order_id],
            "isrand": "4",
            "p_user": "",
            "p_pass": "",
        }
        response = self._post_api(page, "/node/exchangeRandom", payload)
        return self._parse_exchange_response(response, proxy, fallback=target)

    def _find_order_detail(self, page: Any, token: str, proxy: ProxyLine) -> dict[str, Any]:
        payload = {
            "api_token": token,
            "pageIndex": 1,
            "pageSize": 20,
            "cid": CID,
            "p_user": proxy.username,
        }
        response = self._post_api(page, "/node/orderDetail", payload)
        if response.get("code") != 0:
            raise RuntimeError(response.get("msg") or response.get("text") or str(response))

        rows = response.get("data") or []
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"没有搜索到用户名：{proxy.username}")

        for row in rows:
            if (
                str(row.get("p_user", "")).strip() == proxy.username
                and str(row.get("ip", "")).strip() == proxy.ip
                and str(row.get("port", "")).strip() == proxy.port
            ):
                return row

        for row in rows:
            if str(row.get("p_user", "")).strip() == proxy.username:
                return row

        return rows[0]

    def _query_current_proxy(self, page: Any, token: str, proxy: ProxyLine) -> ProxyLine:
        row = self._find_order_detail(page, token, proxy)
        return ProxyLine(
            ip=str(row.get("ip") or proxy.ip),
            port=str(row.get("port") or proxy.port),
            username=str(row.get("p_user") or proxy.username),
            password=str(row.get("p_pass") or proxy.password),
            expires_at=self._format_expires_at(row.get("stoptime") or proxy.expires_at),
            region=self._extract_region(row),
        )

    def _post_api(self, page: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = page.evaluate(
            """
            async ({ url, payload }) => {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const text = await response.text();
                try {
                    return JSON.parse(text);
                } catch (error) {
                    return { code: -1, msg: text || String(error) };
                }
            }
            """,
            {"url": API_BASE_URL + path, "payload": payload},
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"接口返回异常：{result}")
        return result

    @staticmethod
    def _response_json(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            payload = json.loads(response.text())
        if not isinstance(payload, dict):
            raise RuntimeError(f"网站返回异常：{payload}")
        return payload

    def _parse_exchange_response(
        self,
        payload: dict[str, Any],
        original: ProxyLine,
        fallback: dict[str, Any] | None = None,
    ) -> ProxyLine:
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("msg") or payload.get("text") or str(payload))

        data = payload.get("data") or {}
        rows = data.get("list") if isinstance(data, dict) else None
        if not rows:
            raise RuntimeError(f"调换成功但没有返回新 IP 数据：{payload}")

        row = rows[0]
        fallback = fallback or {}
        result = ProxyLine(
            ip=str(row.get("ip") or fallback.get("ip") or original.ip),
            port=str(row.get("port") or fallback.get("port") or original.port),
            username=str(row.get("p_user") or fallback.get("p_user") or original.username),
            password=str(row.get("p_pass") or fallback.get("p_pass") or original.password),
            expires_at=self._format_expires_at(
                row.get("stoptime") or fallback.get("stoptime") or original.expires_at
            ),
            region=self._extract_region(row, fallback),
        )
        self.log(f"调换成功：{original.as_line()} -> {result.as_line()}")
        return result

    @staticmethod
    def _extract_region(*sources: dict[str, Any]) -> str:
        values: list[str] = []
        exact_keys = [
            "region",
            "region_name",
            "area",
            "area_name",
            "province",
            "province_name",
            "city",
            "city_name",
            "isp",
            "isp_name",
            "node_title",
            "ntitle",
        ]

        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in exact_keys:
                value = source.get(key)
                if value is None:
                    continue
                text = str(value).strip()
                if text and text not in values:
                    values.append(text)

        return " ".join(values)

    @staticmethod
    def _format_expires_at(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) >= 10 and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
            return text[:10]
        return text


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("光子 IP-SK5 随机调换工具")
        self.geometry("980x820")
        self.minsize(860, 760)

        self.message_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.mode_var = tk.StringVar(value="api")
        self.test_url_var = tk.StringVar(value=DEFAULT_TEST_URL)
        self.max_latency_var = tk.StringVar(value=str(DEFAULT_MAX_LATENCY_MS))
        self.max_exchange_count_var = tk.StringVar(value=str(DEFAULT_MAX_EXCHANGE_COUNT))
        self.settle_seconds_var = tk.StringVar(value=str(DEFAULT_SETTLE_SECONDS))
        self.bind_local_network_var = tk.BooleanVar(value=True)
        self.local_bind_ip_var = tk.StringVar(value=AUTO_BIND_IP_TEXT)
        self.account_var = tk.StringVar()
        self.accounts: list[AccountProfile]
        self.accounts, self.active_account_id = self._load_accounts()
        self.account_display_ids: list[str] = []
        self.refreshing_account_selector = False
        self.worker_running = False
        self.stop_event = threading.Event()
        self.result_items: dict[str, ProxyLine] = {}
        self.result_index_items: dict[int, str] = {}
        self.input_statuses: dict[int, str] = {}

        self._load_saved_config()
        self._ensure_active_account()
        self._build_ui()
        self._refresh_account_selector()
        self.after(100, self._drain_messages)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        self._build_account_manager(root)

        ttk.Label(root, text="要调换的 IP，一行一条（右侧显示状态）：").pack(anchor=tk.W)
        self._build_input_editor(root)
        self.input_text.insert(
            tk.END, "175.6.50.122|5262|iuaf13s1|iuaf13s1|2026-07-13"
        )
        self._update_input_line_numbers()
        self.input_text.edit_modified(False)

        ttk.Label(root, text="执行方式：后台接口模式（批量调换时不打开网页窗口）").pack(
            anchor=tk.W, pady=(0, 10)
        )

        settings_frame = ttk.LabelFrame(root, text="连通性测试配置", padding=8)
        settings_frame.pack(fill=tk.X, pady=(0, 10))
        settings_frame.columnconfigure(1, weight=1)

        ttk.Label(settings_frame, text="测试网址").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(settings_frame, textvariable=self.test_url_var).grid(
            row=0, column=1, sticky=tk.EW, padx=(8, 14)
        )
        ttk.Label(settings_frame, text="最大延迟(ms)").grid(row=0, column=2, sticky=tk.W)
        ttk.Entry(settings_frame, textvariable=self.max_latency_var, width=8).grid(
            row=0, column=3, sticky=tk.W, padx=(8, 14)
        )
        ttk.Label(settings_frame, text="最大更换次数").grid(row=0, column=4, sticky=tk.W)
        ttk.Entry(settings_frame, textvariable=self.max_exchange_count_var, width=8).grid(
            row=0, column=5, sticky=tk.W, padx=(8, 0)
        )
        ttk.Label(settings_frame, text="等待测试(s)").grid(
            row=1, column=0, sticky=tk.W, pady=(8, 0)
        )
        ttk.Entry(settings_frame, textvariable=self.settle_seconds_var, width=8).grid(
            row=1, column=1, sticky=tk.W, padx=(8, 14), pady=(8, 0)
        )
        ttk.Checkbutton(
            settings_frame,
            text="测速连接 SOCKS5 时走本机真实网络",
            variable=self.bind_local_network_var,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(8, 0))
        ttk.Label(settings_frame, text="本机网卡IP").grid(
            row=2, column=2, sticky=tk.W, pady=(8, 0)
        )
        ttk.Entry(settings_frame, textvariable=self.local_bind_ip_var, width=16).grid(
            row=2, column=3, sticky=tk.W, padx=(8, 14), pady=(8, 0)
        )
        ttk.Label(settings_frame, text="留空或自动=自动检测").grid(
            row=2, column=4, columnspan=2, sticky=tk.W, pady=(8, 0)
        )

        button_frame = ttk.Frame(root)
        button_frame.pack(fill=tk.X, pady=(0, 10))
        self.exchange_button = ttk.Button(
            button_frame, text="随机调换", command=self._start_exchange
        )
        self.exchange_button.pack(side=tk.LEFT, padx=(10, 0))
        self.qualified_exchange_button = ttk.Button(
            button_frame, text="调换+测试", command=self._start_exchange_until_pass
        )
        self.qualified_exchange_button.pack(side=tk.LEFT, padx=(10, 0))
        self.stop_button = ttk.Button(
            button_frame, text="停止", command=self._request_stop, state=tk.DISABLED
        )
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))
        self.clear_input_button = ttk.Button(
            button_frame, text="清空导入", command=self._clear_import_form
        )
        self.clear_input_button.pack(side=tk.LEFT, padx=(10, 0))
        self.save_config_button = ttk.Button(
            button_frame, text="保存配置", command=self._save_config
        )
        self.save_config_button.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(button_frame, text="复制结果", command=self._copy_results).pack(
            side=tk.LEFT, padx=(10, 0)
        )
        ttk.Button(button_frame, text="清空日志", command=self._clear_log).pack(
            side=tk.LEFT, padx=(10, 0)
        )

        self._build_result_table(root)

        ttk.Label(root, text="运行日志：").pack(anchor=tk.W)
        self.log_text = scrolledtext.ScrolledText(root, height=9, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def _build_account_manager(self, parent: ttk.Frame) -> None:
        account_frame = ttk.LabelFrame(parent, text="账号管理", padding=8)
        account_frame.pack(fill=tk.X, pady=(0, 10))
        account_frame.columnconfigure(1, weight=1)

        ttk.Label(account_frame, text="当前使用账号").grid(row=0, column=0, sticky=tk.W)
        self.account_combo = ttk.Combobox(
            account_frame,
            textvariable=self.account_var,
            state="readonly",
            width=24,
        )
        self.account_combo.grid(row=0, column=1, sticky=tk.EW, padx=(8, 10))
        self.account_combo.bind("<<ComboboxSelected>>", self._on_account_selected)

        self.switch_account_button = ttk.Button(
            account_frame, text="新增账号登录", command=self._start_new_account_login
        )
        self.switch_account_button.grid(row=0, column=2, sticky=tk.W, padx=(0, 8))
        self.login_button = ttk.Button(
            account_frame, text="更新当前登录态", command=self._start_login
        )
        self.login_button.grid(row=0, column=3, sticky=tk.W, padx=(0, 8))
        self.rename_account_button = ttk.Button(
            account_frame, text="重命名", command=self._rename_active_account
        )
        self.rename_account_button.grid(row=0, column=4, sticky=tk.W, padx=(0, 8))
        self.delete_account_button = ttk.Button(
            account_frame, text="删除", command=self._delete_active_account
        )
        self.delete_account_button.grid(row=0, column=5, sticky=tk.W)

    def _load_accounts(self) -> tuple[list[AccountProfile], str]:
        accounts: list[AccountProfile] = []
        active_account_id = ""

        if ACCOUNTS_FILE.exists():
            try:
                data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}

            if isinstance(data, dict):
                active_account_id = str(data.get("active_account_id") or "")
                raw_accounts = data.get("accounts") or []
                if isinstance(raw_accounts, list):
                    seen_ids: set[str] = set()
                    seen_names: set[str] = set()
                    for item in raw_accounts:
                        if not isinstance(item, dict):
                            continue
                        account = AccountProfile.from_dict(item)
                        if account is None:
                            continue
                        if account.id in seen_ids or account.name in seen_names:
                            continue
                        seen_ids.add(account.id)
                        seen_names.add(account.name)
                        accounts.append(account)

        if not accounts:
            accounts = [self._make_default_account()]
            active_account_id = accounts[0].id
            self._write_accounts_file(accounts, active_account_id)

        if active_account_id not in {account.id for account in accounts}:
            active_account_id = accounts[0].id
            self._write_accounts_file(accounts, active_account_id)

        return accounts, active_account_id

    @staticmethod
    def _make_default_account() -> AccountProfile:
        now = current_timestamp()
        if LEGACY_PROFILE_DIR.exists():
            return AccountProfile(
                id="legacy",
                name="默认账号",
                profile_subdir=LEGACY_PROFILE_DIR.name,
                created_at=now,
                updated_at=now,
                last_used_at=now,
            )

        account_id = uuid.uuid4().hex[:12]
        return AccountProfile(
            id=account_id,
            name="默认账号",
            profile_subdir=str(Path(ACCOUNT_PROFILE_ROOT.name) / account_id),
            created_at=now,
            updated_at=now,
        )

    def _make_new_account(self, name: str) -> AccountProfile:
        now = current_timestamp()
        account_id = uuid.uuid4().hex[:12]
        return AccountProfile(
            id=account_id,
            name=name,
            profile_subdir=str(Path(ACCOUNT_PROFILE_ROOT.name) / account_id),
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _write_accounts_file(accounts: list[AccountProfile], active_account_id: str) -> None:
        payload = {
            "version": 1,
            "active_account_id": active_account_id,
            "accounts": [account.to_dict() for account in accounts],
        }
        ACCOUNTS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save_accounts(self) -> bool:
        try:
            self._write_accounts_file(self.accounts, self.active_account_id)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入账号配置：{exc}")
            return False
        return True

    def _ensure_active_account(self) -> None:
        if not self.accounts:
            self.accounts = [self._make_default_account()]
        if self.active_account_id not in {account.id for account in self.accounts}:
            self.active_account_id = self.accounts[0].id
            self._save_accounts()

    def _refresh_account_selector(self) -> None:
        if not hasattr(self, "account_combo"):
            return

        self._ensure_active_account()
        self.refreshing_account_selector = True
        try:
            self.account_display_ids = [account.id for account in self.accounts]
            values = [account.name for account in self.accounts]
            self.account_combo.configure(values=values)
            try:
                index = self.account_display_ids.index(self.active_account_id)
            except ValueError:
                index = 0
                self.active_account_id = self.account_display_ids[0]
            self.account_combo.current(index)
            self.account_var.set(values[index])
        finally:
            self.refreshing_account_selector = False

    def _on_account_selected(self, _event: Any = None) -> None:
        if self.refreshing_account_selector:
            return
        index = self.account_combo.current()
        if index < 0 or index >= len(self.account_display_ids):
            return

        account_id = self.account_display_ids[index]
        if account_id == self.active_account_id:
            return

        old_active_account_id = self.active_account_id
        self.active_account_id = account_id
        if not self._save_accounts():
            self.active_account_id = old_active_account_id
            self._refresh_account_selector()
            return
        account = self._get_active_account()
        if account is not None:
            self._send_log(f"已切换当前使用账号：{account.name}")

    def _get_active_account(self) -> AccountProfile | None:
        for account in self.accounts:
            if account.id == self.active_account_id:
                return account
        return self.accounts[0] if self.accounts else None

    def _require_active_account(self) -> AccountProfile | None:
        account = self._get_active_account()
        if account is None:
            messagebox.showerror("账号错误", "请先新增一个账号并完成登录。")
            return None
        return account

    def _validate_account_name(self, name: str, current_id: str | None = None) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("账号名称不能为空。")
        if len(cleaned) > 32:
            raise ValueError("账号名称不能超过 32 个字符。")
        for account in self.accounts:
            if account.id != current_id and account.name == cleaned:
                raise ValueError("账号名称已存在，请换一个名称。")
        return cleaned

    def _next_account_name(self) -> str:
        existing = {account.name for account in self.accounts}
        number = len(self.accounts) + 1
        while True:
            name = f"账号 {number}"
            if name not in existing:
                return name
            number += 1

    def _touch_account(self, account_id: str, mark_used: bool = False) -> None:
        now = current_timestamp()
        for account in self.accounts:
            if account.id == account_id:
                account.updated_at = now
                if mark_used:
                    account.last_used_at = now
                self._save_accounts()
                return

    def _start_new_account_login(self) -> None:
        if self.worker_running:
            messagebox.showinfo("正在运行", "当前任务还没结束，请稍等。")
            return

        default_name = self._next_account_name()
        name = simpledialog.askstring(
            "新增账号",
            "请输入账号名称：",
            initialvalue=default_name,
            parent=self,
        )
        if name is None:
            return

        try:
            account_name = self._validate_account_name(name)
        except ValueError as exc:
            messagebox.showerror("账号名称错误", str(exc))
            return

        account = self._make_new_account(account_name)
        self.accounts.append(account)
        self.active_account_id = account.id
        if not self._save_accounts():
            self.accounts.remove(account)
            self._ensure_active_account()
            return

        self._refresh_account_selector()
        self._send_log(f"已新增账号：{account.name}")
        self._run_worker(lambda exchanger: exchanger.open_login_page(self.stop_event))

    def _rename_active_account(self) -> None:
        account = self._require_active_account()
        if account is None:
            return

        name = simpledialog.askstring(
            "重命名账号",
            "请输入新的账号名称：",
            initialvalue=account.name,
            parent=self,
        )
        if name is None:
            return

        try:
            new_name = self._validate_account_name(name, current_id=account.id)
        except ValueError as exc:
            messagebox.showerror("账号名称错误", str(exc))
            return

        if new_name == account.name:
            return

        old_name = account.name
        account.name = new_name
        account.updated_at = current_timestamp()
        if not self._save_accounts():
            account.name = old_name
            return

        self._refresh_account_selector()
        self._send_log(f"账号已重命名：{old_name} -> {new_name}")

    def _delete_active_account(self) -> None:
        account = self._require_active_account()
        if account is None:
            return

        confirmed = messagebox.askyesno(
            "删除账号",
            f"确定删除账号“{account.name}”吗？\n\n该账号保存的登录状态和浏览器缓存也会一起删除。",
        )
        if not confirmed:
            return

        old_accounts = list(self.accounts)
        old_active = self.active_account_id
        self.accounts = [item for item in self.accounts if item.id != account.id]
        self._remove_account_profile(account)

        if not self.accounts:
            self.accounts = [self._make_default_account()]
        self.active_account_id = self.accounts[0].id

        if not self._save_accounts():
            self.accounts = old_accounts
            self.active_account_id = old_active
            self._refresh_account_selector()
            return

        self._refresh_account_selector()
        self._send_log(f"账号已删除：{account.name}")

    def _remove_account_profile(self, account: AccountProfile) -> None:
        profile_dir = account.profile_dir()
        if not profile_dir.exists():
            return

        try:
            resolved_profile = profile_dir.resolve()
            resolved_root = ACCOUNT_PROFILE_ROOT.resolve()
            resolved_legacy = LEGACY_PROFILE_DIR.resolve()
            allowed = resolved_profile == resolved_legacy or (
                resolved_profile != resolved_root
                and resolved_root in resolved_profile.parents
            )
            if not allowed:
                raise RuntimeError(f"账号目录不在允许范围内：{resolved_profile}")
            shutil.rmtree(resolved_profile)
        except Exception as exc:
            messagebox.showwarning(
                "账号缓存未完全删除",
                f"账号记录已删除，但登录缓存目录删除失败：{exc}",
            )

    def _build_input_editor(self, parent: ttk.Frame) -> None:
        input_frame = ttk.Frame(parent)
        input_frame.pack(fill=tk.BOTH, expand=False, pady=(6, 10))
        input_frame.columnconfigure(1, weight=1)
        input_frame.columnconfigure(2, weight=0)

        self.input_line_numbers = tk.Text(
            input_frame,
            width=5,
            height=10,
            padx=4,
            takefocus=False,
            wrap=tk.NONE,
            state=tk.DISABLED,
            relief=tk.FLAT,
            background="#f2f4f7",
            foreground="#667085",
        )
        self.input_line_numbers.grid(row=0, column=0, sticky="ns")

        self.input_text = tk.Text(
            input_frame,
            height=10,
            wrap=tk.NONE,
            undo=True,
            yscrollcommand=lambda first, last: self._sync_input_scrollbar(
                input_scrollbar, first, last
            ),
        )
        self.input_text.grid(row=0, column=1, sticky="nsew")
        self.input_line_numbers.configure(font=self.input_text.cget("font"))

        self.input_status_text = tk.Text(
            input_frame,
            width=18,
            height=10,
            padx=6,
            takefocus=False,
            wrap=tk.NONE,
            state=tk.DISABLED,
            relief=tk.FLAT,
            background="#f8fafc",
            foreground="#344054",
        )
        self.input_status_text.grid(row=0, column=2, sticky="ns")
        self.input_status_text.configure(font=self.input_text.cget("font"))

        input_scrollbar = ttk.Scrollbar(input_frame, orient=tk.VERTICAL)
        input_scrollbar.grid(row=0, column=3, sticky="ns")
        input_scrollbar.configure(command=self._scroll_input_editor)

        self.input_text.bind("<<Modified>>", self._on_input_modified)
        self.input_line_numbers.bind("<MouseWheel>", self._on_input_line_number_mousewheel)
        self.input_status_text.bind("<MouseWheel>", self._on_input_line_number_mousewheel)

    def _scroll_input_editor(self, *args: Any) -> None:
        self.input_text.yview(*args)
        self.input_line_numbers.yview(*args)
        self.input_status_text.yview(*args)

    def _sync_input_scrollbar(
        self,
        scrollbar: ttk.Scrollbar,
        first: str,
        last: str,
    ) -> None:
        scrollbar.set(first, last)
        self.input_line_numbers.yview_moveto(first)
        self.input_status_text.yview_moveto(first)

    def _on_input_modified(self, _event: Any = None) -> None:
        if self.input_text.edit_modified():
            if not self.worker_running and self.input_statuses:
                self.input_statuses.clear()
            self._update_input_line_numbers()
            self.input_text.edit_modified(False)

    def _on_input_line_number_mousewheel(self, event: Any) -> str:
        self.input_text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.input_line_numbers.yview_moveto(self.input_text.yview()[0])
        self.input_status_text.yview_moveto(self.input_text.yview()[0])
        return "break"

    def _update_input_line_numbers(self) -> None:
        text = self.input_text.get("1.0", "end-1c")
        lines = text.split("\n") if text else [""]
        numbers: list[str] = []
        statuses: list[str] = []
        proxy_number = 1

        for line in lines:
            if line.strip():
                numbers.append(str(proxy_number))
                statuses.append(self.input_statuses.get(proxy_number, ""))
                proxy_number += 1
            else:
                numbers.append("")
                statuses.append("")

        self.input_line_numbers.configure(state=tk.NORMAL)
        self.input_line_numbers.delete("1.0", tk.END)
        self.input_line_numbers.insert("1.0", "\n".join(numbers))
        self.input_line_numbers.configure(state=tk.DISABLED)

        self.input_status_text.configure(state=tk.NORMAL)
        self.input_status_text.delete("1.0", tk.END)
        self.input_status_text.insert("1.0", "\n".join(statuses))
        self.input_status_text.configure(state=tk.DISABLED)

    def _set_input_status(self, row_number: int, status: str) -> None:
        self.input_statuses[row_number] = status
        self._update_input_line_numbers()

    def _set_all_input_statuses(self, count: int, status: str) -> None:
        self.input_statuses = {index: status for index in range(1, count + 1)}
        self._update_input_line_numbers()

    def _clear_input_statuses(self) -> None:
        self.input_statuses.clear()
        self._update_input_line_numbers()

    def _clear_import_form(self) -> None:
        if self.worker_running:
            return
        self.input_text.delete("1.0", tk.END)
        self.input_text.edit_modified(False)
        self._clear_input_statuses()
        self.input_text.focus_set()

    def _build_result_table(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="更换后的新 IP：").pack(anchor=tk.W)

        table_frame = ttk.Frame(parent)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 10))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("index", "proxy", "region", "test")
        self.result_table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            height=10,
            selectmode="extended",
        )
        self.result_table.heading("index", text="序号")
        self.result_table.heading("proxy", text="代理信息")
        self.result_table.heading("region", text="地区")
        self.result_table.heading("test", text="测试结果")
        self.result_table.column("index", width=56, stretch=False, anchor=tk.CENTER)
        self.result_table.column("proxy", width=500, stretch=True, anchor=tk.W)
        self.result_table.column("region", width=180, stretch=False, anchor=tk.W)
        self.result_table.column("test", width=220, stretch=False, anchor=tk.W)

        y_scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.result_table.yview)
        x_scrollbar = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.result_table.xview)
        self.result_table.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)
        self.result_table.grid(row=0, column=0, sticky="nsew")
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar.grid(row=1, column=0, sticky="ew")

        self.result_menu = tk.Menu(self, tearoff=0)
        self.result_menu.add_command(label="复制选中单条代理", command=self._copy_selected_single_result)
        self.result_menu.add_command(label="调换+测试", command=self._exchange_selected_until_pass)
        self.result_table.bind("<Button-3>", self._show_result_menu)

    def _show_result_menu(self, event: Any) -> None:
        row_id = self.result_table.identify_row(event.y)
        if row_id and row_id not in self.result_table.selection():
            self.result_table.selection_set(row_id)
        try:
            self.result_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.result_menu.grab_release()

    def _add_result_item(self, proxy: ProxyLine, row_number: int | None = None) -> None:
        if row_number is None:
            row_number = self._next_result_row_number()

        existing_item_id = self.result_index_items.get(row_number)
        if existing_item_id:
            self._replace_result_item(existing_item_id, proxy, row_number=row_number)
            return

        insert_position = self._result_insert_position(row_number)
        item_id = self.result_table.insert(
            "",
            insert_position,
            values=(
                row_number,
                proxy.as_line(),
                proxy.display_region(),
                proxy.display_test_status(),
            ),
        )
        self.result_items[item_id] = proxy
        self.result_index_items[row_number] = item_id
        self.result_table.selection_set(item_id)
        self.result_table.see(item_id)

    def _replace_result_item(
        self,
        item_id: str,
        proxy: ProxyLine,
        row_number: int | None = None,
    ) -> None:
        if item_id not in self.result_items:
            self._add_result_item(proxy, row_number=row_number)
            return

        current_values = self.result_table.item(item_id, "values")
        if row_number is None:
            row_number = int(current_values[0]) if current_values else self._next_result_row_number()
        old_row_number = int(current_values[0]) if current_values else row_number
        if old_row_number != row_number:
            self.result_index_items.pop(old_row_number, None)
        self.result_index_items[row_number] = item_id
        self.result_items[item_id] = proxy
        self.result_table.item(
            item_id,
            values=(
                row_number,
                proxy.as_line(),
                proxy.display_region(),
                proxy.display_test_status(),
            ),
        )
        self.result_table.selection_set(item_id)
        self.result_table.see(item_id)

    def _next_result_row_number(self) -> int:
        existing_numbers = [
            int(self.result_table.item(item_id, "values")[0])
            for item_id in self.result_table.get_children()
            if self.result_table.item(item_id, "values")
        ]
        return max(existing_numbers, default=0) + 1

    def _result_insert_position(self, row_number: int) -> int | str:
        for position, item_id in enumerate(self.result_table.get_children()):
            values = self.result_table.item(item_id, "values")
            if values and int(values[0]) > row_number:
                return position
        return tk.END

    def _selected_result_entries(self) -> list[tuple[str, ProxyLine]]:
        entries: list[tuple[str, ProxyLine]] = []
        for item_id in self.result_table.selection():
            proxy = self.result_items.get(item_id)
            if proxy is not None:
                entries.append((item_id, proxy))
        return entries

    def _selected_result_proxies(self) -> list[ProxyLine]:
        return [proxy for _, proxy in self._selected_result_entries()]

    def _clear_results_table(self) -> None:
        for item_id in self.result_table.get_children():
            self.result_table.delete(item_id)
        self.result_items.clear()
        self.result_index_items.clear()

    def _copy_selected_single_result(self) -> None:
        proxies = self._selected_result_proxies()
        if len(proxies) != 1:
            messagebox.showinfo("请选择单条", "请在新 IP 列表中只选中一条。")
            return
        text = proxies[0].as_line()
        self.clipboard_clear()
        self.clipboard_append(text)
        self._send_log("选中单条代理已复制到剪贴板（不含地区和测试结果）。")

    def _exchange_selected_until_pass(self) -> None:
        entries = self._selected_result_entries()
        if len(entries) != 1:
            messagebox.showinfo("请选择单条", "请在新 IP 列表中只选中一条。")
            return

        try:
            target_url = self._normalize_target_url(self.test_url_var.get())
            max_latency_ms = self._parse_positive_int(self.max_latency_var.get(), "最大延迟")
            max_exchange_count = self._parse_positive_int(
                self.max_exchange_count_var.get(), "最大更换次数"
            )
            settle_seconds = self._parse_positive_int(self.settle_seconds_var.get(), "等待测试时间")
            local_bind_ip = self._resolve_local_bind_ip()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        selected_item_id, proxy = entries[0]
        selected_values = self.result_table.item(selected_item_id, "values")
        selected_row_number = int(selected_values[0]) if selected_values else 1
        self._set_input_status(selected_row_number, "待处理")

        def task(exchanger: Gzsk5Exchanger) -> None:
            self._send_log(f"选中单条重新更换测试：{proxy.as_line()}")

            def handle_result(
                index: int,
                original: ProxyLine,
                result: ProxyLine | None,
                test_result: ProxyTestResult | None,
                changes: int,
                error: Exception | None,
            ) -> None:
                if result is not None and error is None:
                    result.test_status = Gzsk5Exchanger._format_test_result(test_result)
                    self._send_replace_result_proxy(selected_item_id, result)
                    test_text = Gzsk5Exchanger._format_test_result(test_result)
                    self._send_log(
                        f"选中单条合格：{result.as_line()}，更换 {changes} 次，{test_text}"
                    )
                    return

                test_text = Gzsk5Exchanger._format_test_result(test_result)
                if result is not None:
                    result.test_status = f"失败：更换 {changes} 次后仍不合格，{test_text}"
                    self._send_replace_result_proxy(selected_item_id, result)
                    self._send_log(
                        f"选中单条失败但保留最后新 IP：{result.as_line()}，"
                        f"更换 {changes} 次，{test_text}，{error}"
                    )
                    return

                self._send_result(f"失败：{original.as_line()} -> {error}")
                self._send_log(
                    f"选中单条失败：{original.as_line()}，更换 {changes} 次，{test_text}，{error}"
                )

            exchanger.exchange_many_until_pass(
                proxies=[proxy],
                target_url=target_url,
                max_latency_ms=max_latency_ms,
                max_exchange_count=max_exchange_count,
                on_result=handle_result,
                stop_event=self.stop_event,
                local_bind_ip=local_bind_ip,
                on_status=lambda _index, status: self._send_input_status(
                    selected_row_number,
                    status,
                ),
                settle_seconds=settle_seconds,
            )

        self._run_worker(task)

    def _start_login(self) -> None:
        self._run_worker(lambda exchanger: exchanger.open_login_page(self.stop_event))

    def _parse_input_proxies(self) -> list[ProxyLine] | None:
        raw_lines = [
            line.strip()
            for line in self.input_text.get("1.0", tk.END).splitlines()
            if line.strip()
        ]
        proxies: list[ProxyLine] = []
        input_errors: list[str] = []
        for line_number, line in enumerate(raw_lines, start=1):
            try:
                proxies.append(ProxyLine.parse(line))
            except ValueError as exc:
                input_errors.append(f"第 {line_number} 行：{exc}")

        if input_errors:
            messagebox.showerror("输入错误", "\n".join(input_errors))
            return

        if not proxies:
            messagebox.showerror("输入错误", "请至少输入一条 IP。")
            return None

        return proxies

    def _start_exchange(self) -> None:
        proxies = self._parse_input_proxies()
        if proxies is None:
            return

        mode = "api"
        self._clear_results_table()
        self._set_all_input_statuses(len(proxies), "待处理")

        def task(exchanger: Gzsk5Exchanger) -> None:
            self._send_log(f"本次共 {len(proxies)} 条，开始批量随机调换。")

            def handle_result(
                index: int,
                proxy: ProxyLine,
                result: ProxyLine | None,
                error: Exception | None,
            ) -> None:
                if result is not None:
                    result.test_status = "未测试"
                    self._send_result_proxy(result, index)
                    self._send_log(f"[{index}/{len(proxies)}] 成功：{result.as_line()}")
                    return

                self._send_result(f"失败：{proxy.as_line()} -> {error}")
                self._send_log(f"[{index}/{len(proxies)}] 失败：{proxy.as_line()} -> {error}")

            exchanger.exchange_many(
                proxies,
                mode,
                handle_result,
                stop_event=self.stop_event,
                on_status=self._send_input_status,
            )

        self._run_worker(task)

    def _start_exchange_until_pass(self) -> None:
        proxies = self._parse_input_proxies()
        if proxies is None:
            return

        try:
            target_url = self._normalize_target_url(self.test_url_var.get())
            max_latency_ms = self._parse_positive_int(self.max_latency_var.get(), "最大延迟")
            max_exchange_count = self._parse_positive_int(
                self.max_exchange_count_var.get(), "最大更换次数"
            )
            settle_seconds = self._parse_positive_int(self.settle_seconds_var.get(), "等待测试时间")
            local_bind_ip = self._resolve_local_bind_ip()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self._clear_results_table()
        self._set_all_input_statuses(len(proxies), "待处理")

        def task(exchanger: Gzsk5Exchanger) -> None:
            bind_text = (
                f"；测速连接 SOCKS5 绑定本机网卡 {local_bind_ip}"
                if local_bind_ip
                else "；测速连接 SOCKS5 使用系统默认路由"
            )
            self._send_log(
                f"本次共 {len(proxies)} 条，目标 {target_url}，"
                f"延迟阈值 {max_latency_ms}ms；调换串行执行，"
                f"每条调换成功后独立倒计时 {settle_seconds} 秒并发测速；"
                f"最多更换 {max_exchange_count} 次"
                f"{bind_text}。"
            )

            def handle_result(
                index: int,
                original: ProxyLine,
                result: ProxyLine | None,
                test_result: ProxyTestResult | None,
                changes: int,
                error: Exception | None,
            ) -> None:
                if result is not None and error is None:
                    result.test_status = Gzsk5Exchanger._format_test_result(test_result)
                    self._send_result_proxy(result, index)
                    test_text = Gzsk5Exchanger._format_test_result(test_result)
                    self._send_log(
                        f"[{index}/{len(proxies)}] 合格：{result.as_line()}，"
                        f"更换 {changes} 次，{test_text}"
                    )
                    return

                if result is not None:
                    test_text = Gzsk5Exchanger._format_test_result(test_result)
                    result.test_status = f"失败：更换 {changes} 次后仍不合格，{test_text}"
                    self._send_result_proxy(result, index)
                    self._send_log(
                        f"[{index}/{len(proxies)}] 失败但保留最后新 IP：{result.as_line()}，"
                        f"更换 {changes} 次，{test_text}，{error}"
                    )
                    return

                test_text = Gzsk5Exchanger._format_test_result(test_result)
                self._send_result(f"失败：{original.as_line()} -> {error}")
                self._send_log(
                    f"[{index}/{len(proxies)}] 失败：{original.as_line()}，"
                    f"更换 {changes} 次，{test_text}，{error}"
                )

            exchanger.exchange_many_until_pass(
                proxies=proxies,
                target_url=target_url,
                max_latency_ms=max_latency_ms,
                max_exchange_count=max_exchange_count,
                on_result=handle_result,
                stop_event=self.stop_event,
                local_bind_ip=local_bind_ip,
                on_status=self._send_input_status,
                settle_seconds=settle_seconds,
            )

        self._run_worker(task)

    @staticmethod
    def _normalize_target_url(value: str) -> str:
        url = value.strip()
        if not url:
            raise ValueError("测试网址不能为空。")
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            url = "https://" + url
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("测试网址格式不正确。")
        return url

    @staticmethod
    def _parse_positive_int(value: str, label: str, allow_zero: bool = False) -> int:
        text = value.strip()
        if not text.isdigit():
            raise ValueError(f"{label} 必须是数字。")
        number = int(text)
        if allow_zero:
            if number < 0:
                raise ValueError(f"{label} 不能小于 0。")
        elif number <= 0:
                raise ValueError(f"{label} 必须大于 0。")
        return number

    def _load_saved_config(self) -> None:
        if not CONFIG_FILE.exists():
            return

        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return

        if not isinstance(data, dict):
            return

        self.test_url_var.set(str(data.get("test_url") or DEFAULT_TEST_URL))
        self.max_latency_var.set(str(data.get("max_latency_ms") or DEFAULT_MAX_LATENCY_MS))
        self.max_exchange_count_var.set(
            str(data.get("max_exchange_count") or DEFAULT_MAX_EXCHANGE_COUNT)
        )
        self.settle_seconds_var.set(str(data.get("settle_seconds") or DEFAULT_SETTLE_SECONDS))
        self.bind_local_network_var.set(bool(data.get("bind_local_network", True)))
        self.local_bind_ip_var.set(str(data.get("local_bind_ip") or AUTO_BIND_IP_TEXT))

    def _collect_config(self) -> dict[str, Any]:
        test_url = self._normalize_target_url(self.test_url_var.get())
        max_latency_ms = self._parse_positive_int(self.max_latency_var.get(), "最大延迟")
        max_exchange_count = self._parse_positive_int(
            self.max_exchange_count_var.get(), "最大更换次数"
        )
        settle_seconds = self._parse_positive_int(self.settle_seconds_var.get(), "等待测试时间")
        local_bind_ip = self.local_bind_ip_var.get().strip() or AUTO_BIND_IP_TEXT

        if self.bind_local_network_var.get() and local_bind_ip != AUTO_BIND_IP_TEXT:
            self._validate_bind_ip(local_bind_ip)

        return {
            "test_url": test_url,
            "max_latency_ms": max_latency_ms,
            "max_exchange_count": max_exchange_count,
            "settle_seconds": settle_seconds,
            "bind_local_network": bool(self.bind_local_network_var.get()),
            "local_bind_ip": local_bind_ip,
        }

    def _save_config(self) -> None:
        try:
            config = self._collect_config()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        try:
            CONFIG_FILE.write_text(
                json.dumps(config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入配置文件：{exc}")
            return

        self.test_url_var.set(config["test_url"])
        self.max_latency_var.set(str(config["max_latency_ms"]))
        self.max_exchange_count_var.set(str(config["max_exchange_count"]))
        self.settle_seconds_var.set(str(config["settle_seconds"]))
        self.local_bind_ip_var.set(config["local_bind_ip"])
        self._send_log(f"配置已保存：{CONFIG_FILE.name}")
        messagebox.showinfo("保存成功", "配置已保存，下次打开会自动加载。")

    def _resolve_local_bind_ip(self) -> str | None:
        if not self.bind_local_network_var.get():
            return None

        value = self.local_bind_ip_var.get().strip()
        if value and value != AUTO_BIND_IP_TEXT:
            self._validate_bind_ip(value)
            return value

        detected_ip, description = Gzsk5Exchanger.detect_local_network_ipv4()
        if not detected_ip:
            raise ValueError(
                "无法自动检测本机真实网卡 IP。"
                f"原因：{description}。请手动填写 Wi-Fi/以太网的 IPv4，或取消勾选本机真实网络。"
            )

        self._validate_bind_ip(detected_ip)
        self.local_bind_ip_var.set(detected_ip)
        self._send_log(f"已自动选择本机真实网络网卡：{description}，绑定 IP {detected_ip}")
        return detected_ip

    @staticmethod
    def _validate_bind_ip(value: str) -> None:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError("本机网卡IP格式不正确，请填写本机 Wi-Fi/以太网 IPv4。") from exc

        if address.version != 4 or address.is_loopback or address.is_unspecified:
            raise ValueError("本机网卡IP必须是可用的 IPv4，不能是 127.0.0.1 或 0.0.0.0。")

    def _run_worker(self, task: Callable[[Gzsk5Exchanger], None]) -> None:
        if self.worker_running:
            messagebox.showinfo("正在运行", "当前任务还没结束，请稍等。")
            return

        account = self._require_active_account()
        if account is None:
            return

        self.worker_running = True
        self.stop_event.clear()
        self._touch_account(account.id, mark_used=True)
        self._set_buttons(False)

        def runner() -> None:
            exchanger = Gzsk5Exchanger(
                self._send_log,
                account.profile_dir(),
                account.name,
            )
            try:
                self._send_log(f"当前使用账号：{account.name}")
                task(exchanger)
                self._send_log("任务完成。")
            except StopRequested:
                self._send_log("任务已停止。")
            except Exception as exc:
                self._send_log(f"任务失败：{exc}")
            finally:
                self.message_queue.put(("done", ""))

        threading.Thread(target=runner, daemon=True).start()

    def _request_stop(self) -> None:
        if not self.worker_running:
            return
        self.stop_event.set()
        self._send_log("已请求停止，当前网络请求结束后会中断任务。")
        self.stop_button.config(state=tk.DISABLED)

    def _send_log(self, message: str) -> None:
        append_run_log("LOG", message)
        self.message_queue.put(("log", message))

    def _send_result(self, message: str) -> None:
        append_run_log("RESULT", message)
        self.message_queue.put(("result", message))

    def _send_result_proxy(self, proxy: ProxyLine, row_number: int | None = None) -> None:
        append_run_log("RESULT", f"{proxy.as_line()} 地区={proxy.display_region()}")
        self.message_queue.put(("result_proxy", (row_number, proxy)))

    def _send_replace_result_proxy(self, item_id: str, proxy: ProxyLine) -> None:
        append_run_log("RESULT", f"替换原行 {proxy.as_line()} 地区={proxy.display_region()}")
        self.message_queue.put(("replace_result_proxy", (item_id, proxy)))

    def _send_input_status(self, row_number: int, status: str) -> None:
        self.message_queue.put(("input_status", (row_number, status)))

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, message = self.message_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                timestamp = time.strftime("%H:%M:%S")
                self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
                self.log_text.see(tk.END)
            elif kind == "result":
                self._send_log(message)
            elif kind == "result_proxy":
                row_number, proxy = message
                self._add_result_item(proxy, row_number=row_number)
            elif kind == "replace_result_proxy":
                item_id, proxy = message
                self._replace_result_item(item_id, proxy)
            elif kind == "input_status":
                row_number, status = message
                self._set_input_status(row_number, status)
            elif kind == "done":
                self.worker_running = False
                self._set_buttons(True)

        self.after(100, self._drain_messages)

    def _set_buttons(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        self.login_button.config(state=state)
        self.switch_account_button.config(state=state)
        self.rename_account_button.config(state=state)
        self.delete_account_button.config(state=state)
        self.account_combo.config(state="readonly" if enabled else tk.DISABLED)
        self.exchange_button.config(state=state)
        self.qualified_exchange_button.config(state=state)
        self.clear_input_button.config(state=state)
        self.save_config_button.config(state=state)
        self.input_text.config(state=tk.NORMAL if enabled else tk.DISABLED)
        self.stop_button.config(state=tk.DISABLED if enabled else tk.NORMAL)

    def _copy_results(self) -> None:
        proxies = [
            self.result_items[item_id]
            for item_id in self.result_table.get_children()
            if item_id in self.result_items
        ]
        text = "\n".join(proxy.as_line() for proxy in proxies)
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._send_log("全部代理结果已复制到剪贴板（不含地区和测试结果）。")

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)


if __name__ == "__main__":
    App().mainloop()
