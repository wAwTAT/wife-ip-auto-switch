#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按当前连接的网络名称（Wi-Fi SSID / 有线适配器）自动设置 IPv4、网关与 DNS。

用法：
    双击“启动.vbs”打开图形界面，程序会自动请求管理员权限。
    python wifi_ip_switch.py --watch     后台监控（供开机自启使用）
    python wifi_ip_switch.py --diagnose  只读诊断，输出当前网络与匹配结果
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple
from xml.sax.saxutils import escape

import tkinter as tk
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = Path(__file__).resolve()
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
WATCHER_LOG = LOG_DIR / "watcher.log"
PID_FILE = LOG_DIR / "watcher.pid"

TASK_NAME = "WifiIpAutoSwitch"
MUTEX_NAME = "Global\\WifiIpAutoSwitchWatcher"
APP_TITLE = "网络 IP 自动切换"

TYPE_LABELS = {"wifi": "Wi-Fi 无线", "ethernet": "有线网络", "any": "任意网络"}
TYPE_KEYS = {label: key for key, label in TYPE_LABELS.items()}
MODE_LABELS = {"static": "固定 IP", "dhcp": "自动获取 (DHCP)"}
MODE_KEYS = {label: key for key, label in MODE_LABELS.items()}

DEFAULT_CONFIG = {
    "pollSeconds": 5,
    "revertToDhcpWhenUnmatched": True,
    "profiles": [],
}

UI_FONT = ("Microsoft YaHei UI", 9)
UI_FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def pythonw_executable() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def relaunch_as_admin() -> bool:
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    rc = shell32.ShellExecuteW(
        None, "runas", str(pythonw_executable()),
        '"%s"' % SCRIPT_PATH, str(BASE_DIR), 1,
    )
    return bool(rc) and rc > 32


def run_command(cmd: List[str], timeout: int = 25) -> Tuple[int, bytes]:
    # 以 pythonw 运行时没有控制台，netsh/schtasks 等控制台程序默认会弹出黑框；
    # CREATE_NO_WINDOW 可以让这些子进程完全在后台运行。
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=False, creationflags=no_window,
        )
    except FileNotFoundError:
        raise RuntimeError("找不到命令：%s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise RuntimeError("命令执行超时：%s" % " ".join(map(str, cmd)))
    return proc.returncode, (proc.stdout or b"") + (proc.stderr or b"")


def decode_native(data: bytes) -> str:
    """兼容 UTF-8 / 系统 ANSI 编码的命令行输出。"""
    if not data:
        return ""
    for encoding in ("utf-8", "mbcs", "oem"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("ascii", "replace")


def current_user_name() -> str:
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    return "%s\\%s" % (domain, user) if domain else user


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def normalize_config(raw) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["profiles"] = []
    if isinstance(raw, dict):
        try:
            poll = int(raw.get("pollSeconds", 5))
        except (TypeError, ValueError):
            poll = 5
        cfg["pollSeconds"] = min(3600, max(2, poll))
        cfg["revertToDhcpWhenUnmatched"] = bool(raw.get("revertToDhcpWhenUnmatched", True))
        profiles = raw.get("profiles")
        if isinstance(profiles, list):
            cfg["profiles"] = [item for item in profiles if isinstance(item, dict)]
    return cfg


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        cfg = normalize_config({})
        save_config(cfg)
        return cfg
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    return normalize_config(raw)


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    cfg = normalize_config(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# IPv4 辅助
# ---------------------------------------------------------------------------

def prefix_to_mask(prefix: int) -> str:
    prefix = max(0, min(32, int(prefix)))
    if prefix == 0:
        return "0.0.0.0"
    value = ((1 << prefix) - 1) << (32 - prefix)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def mask_to_prefix(mask: str) -> Optional[int]:
    try:
        parts = [int(part) for part in mask.split(".")]
    except (ValueError, AttributeError):
        return None
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        return None
    bits = "".join(format(part, "08b") for part in parts)
    first_zero = bits.find("0")
    if first_zero == -1:
        return 32
    if "1" in bits[first_zero:]:
        return None
    return first_zero


def is_valid_ipv4(value: str) -> bool:
    try:
        parts = [int(part) for part in value.split(".")]
    except (ValueError, AttributeError):
        return False
    return len(parts) == 4 and all(0 <= part <= 255 for part in parts)


# ---------------------------------------------------------------------------
# Windows 网络状态（IP Helper API，避免启动 PowerShell 带来的延迟）
# ---------------------------------------------------------------------------

AF_INET = 2
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
IF_TYPE_ETHERNET = 6
IF_TYPE_IEEE80211 = 71
IF_OPER_STATUS_UP = 1

VIRTUAL_KEYWORDS = (
    "virtual", "hyper-v", "vmware", "virtualbox", "loopback", "vpn",
    "tap-windows", "tunnel", "miniport", "pseudo", "wi-fi direct",
)


class SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", ctypes.c_ushort),
        ("sin_port", ctypes.c_ushort),
        ("sin_addr", ctypes.c_ubyte * 4),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


class SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]


class IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("Address", SOCKET_ADDRESS),
        ("PrefixOrigin", ctypes.c_int),
        ("SuffixOrigin", ctypes.c_int),
        ("DadState", ctypes.c_int),
        ("ValidLifetime", ctypes.c_ulong),
        ("LeaseLifetime", ctypes.c_ulong),
        ("OnLinkPrefixLength", ctypes.c_ubyte),
    ]


class IP_ADAPTER_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("Address", SOCKET_ADDRESS),
    ]


class IP_ADAPTER_ADDRESSES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("IfIndex", ctypes.c_ulong),
        ("Next", ctypes.c_void_p),
        ("AdapterName", ctypes.c_char_p),
        ("FirstUnicastAddress", ctypes.c_void_p),
        ("FirstAnycastAddress", ctypes.c_void_p),
        ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p),
        ("FirstDnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p),
        ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("Mtu", ctypes.c_ulong),
        ("IfType", ctypes.c_ulong),
        ("OperStatus", ctypes.c_ulong),
        ("Ipv6IfIndex", ctypes.c_ulong),
        ("ZoneIndices", ctypes.c_ulong * 16),
        ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_ulonglong),
        ("ReceiveLinkSpeed", ctypes.c_ulonglong),
        ("FirstWinsServerAddress", ctypes.c_void_p),
        ("FirstGatewayAddress", ctypes.c_void_p),
        ("Ipv4Metric", ctypes.c_ulong),
        ("Ipv6Metric", ctypes.c_ulong),
        ("Luid", ctypes.c_ulonglong),
        ("Dhcpv4Server", SOCKET_ADDRESS),
        ("CompartmentId", ctypes.c_ulong),
        ("NetworkGuid", ctypes.c_ubyte * 16),
        ("Dhcpv6Server", SOCKET_ADDRESS),
        ("Dhcpv6ClientDuid", ctypes.c_ubyte * 130),
        ("Dhcpv6ClientDuidLength", ctypes.c_ulong),
        ("Dhcpv6Iaid", ctypes.c_ulong),
        ("FirstDnsSuffixEx", ctypes.c_void_p),
    ]


class IP_ADDR_STRING(ctypes.Structure):
    pass


IP_ADDR_STRING._fields_ = [
    ("Next", ctypes.POINTER(IP_ADDR_STRING)),
    ("IpAddress", ctypes.c_char * 16),
    ("IpMask", ctypes.c_char * 16),
    ("Context", ctypes.c_ulong),
]


class IP_ADAPTER_INFO(ctypes.Structure):
    _fields_ = [
        ("Next", ctypes.c_void_p),
        ("ComboIndex", ctypes.c_ulong),
        ("AdapterName", ctypes.c_char * 260),
        ("Description", ctypes.c_char * 132),
        ("AddressLength", ctypes.c_ulong),
        ("Address", ctypes.c_ubyte * 8),
        ("Index", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("DhcpEnabled", ctypes.c_ulong),
        ("CurrentIpAddress", ctypes.c_void_p),
        ("IpAddressList", IP_ADDR_STRING),
        ("GatewayList", IP_ADDR_STRING),
        ("DhcpServer", IP_ADDR_STRING),
    ]


def _normalize_guid(value: str) -> str:
    return value.strip().strip("{}").lower()


def _decode_c_char(field) -> str:
    return bytes(field).split(b"\x00", 1)[0].decode("ascii", "ignore")


def _iter_linked(pointer, struct_type):
    while pointer:
        item = ctypes.cast(pointer, ctypes.POINTER(struct_type)).contents
        yield item
        pointer = item.Next


def _ipv4_from_socket_address(socket_address: SOCKET_ADDRESS) -> str:
    if not socket_address.lpSockaddr:
        return ""
    sockaddr = ctypes.cast(
        socket_address.lpSockaddr, ctypes.POINTER(SOCKADDR_IN)).contents
    if sockaddr.sin_family != AF_INET:
        return ""
    return ".".join(str(octet) for octet in sockaddr.sin_addr)


def _get_gaa_adapters() -> List[dict]:
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.GetAdaptersAddresses.argtypes = [
        ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
    ]
    iphlpapi.GetAdaptersAddresses.restype = ctypes.c_ulong
    flags = (GAA_FLAG_INCLUDE_GATEWAYS | GAA_FLAG_SKIP_ANYCAST
             | GAA_FLAG_SKIP_MULTICAST)
    size = ctypes.c_ulong(0)
    code = iphlpapi.GetAdaptersAddresses(
        AF_INET, flags, None, None, ctypes.byref(size))
    if code not in (0, 111):
        raise RuntimeError("查询网络适配器失败（错误码 %s）" % code)
    if not size.value:
        return []
    buffer = ctypes.create_string_buffer(size.value)
    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong)).contents.value = \
        ctypes.sizeof(IP_ADAPTER_ADDRESSES)
    code = iphlpapi.GetAdaptersAddresses(
        AF_INET, flags, None, buffer, ctypes.byref(size))
    if code != 0:
        raise RuntimeError("读取网络适配器失败（错误码 %s）" % code)

    result = []
    node = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_ADDRESSES))
    while node:
        adapter = node.contents
        guid = (adapter.AdapterName or b"").decode("ascii", "ignore")
        unicast = [
            _ipv4_from_socket_address(item.Address)
            for item in _iter_linked(adapter.FirstUnicastAddress, IP_ADAPTER_UNICAST_ADDRESS)
        ]
        unicast = [ip for ip in unicast if ip]
        gateways = [
            _ipv4_from_socket_address(item.Address)
            for item in _iter_linked(adapter.FirstGatewayAddress, IP_ADAPTER_ADDRESS)
        ]
        gateways = [ip for ip in gateways if ip]
        dns = [
            _ipv4_from_socket_address(item.Address)
            for item in _iter_linked(adapter.FirstDnsServerAddress, IP_ADAPTER_ADDRESS)
        ]
        dns = [ip for ip in dns if ip]
        result.append({
            "guid": guid,
            "guid_key": _normalize_guid(guid),
            "friendly": adapter.FriendlyName or "",
            "description": adapter.Description or "",
            "iftype": int(adapter.IfType),
            "oper": int(adapter.OperStatus),
            "ip": unicast[0] if unicast else "",
            "gateway": gateways[0] if gateways else "",
            "dns": tuple(dns),
        })
        node = ctypes.cast(adapter.Next, ctypes.POINTER(IP_ADAPTER_ADDRESSES)) \
            if adapter.Next else None
    return result


def _get_adapter_info_map() -> dict:
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.GetAdaptersInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    iphlpapi.GetAdaptersInfo.restype = ctypes.c_ulong
    size = ctypes.c_ulong(0)
    code = iphlpapi.GetAdaptersInfo(None, ctypes.byref(size))
    if code not in (0, 111):
        return {}
    if not size.value:
        return {}
    buffer = ctypes.create_string_buffer(size.value)
    code = iphlpapi.GetAdaptersInfo(buffer, ctypes.byref(size))
    if code != 0:
        return {}
    result = {}
    node = ctypes.cast(buffer, ctypes.POINTER(IP_ADAPTER_INFO))
    while node:
        adapter = node.contents
        guid = _decode_c_char(adapter.AdapterName)
        ip = _decode_c_char(adapter.IpAddressList.IpAddress)
        mask = _decode_c_char(adapter.IpAddressList.IpMask)
        gateway = _decode_c_char(adapter.GatewayList.IpAddress)
        result[_normalize_guid(guid)] = {
            "ip": ip if is_valid_ipv4(ip) and ip != "0.0.0.0" else "",
            "mask": mask if is_valid_ipv4(mask) else "",
            "gateway": gateway if is_valid_ipv4(gateway) and gateway != "0.0.0.0" else "",
            "dhcp": bool(adapter.DhcpEnabled),
        }
        node = ctypes.cast(adapter.Next, ctypes.POINTER(IP_ADAPTER_INFO)) \
            if adapter.Next else None
    return result


def get_wifi_ssids() -> dict:
    """返回 {适配器名小写: SSID}，读取失败时返回空字典。"""
    try:
        code, output = run_command(["netsh", "wlan", "show", "interfaces"], timeout=15)
    except RuntimeError:
        return {}
    if code != 0:
        return {}
    text = decode_native(output)
    result = {}
    for block in re.split(r"(?:\r?\n){2,}", text):
        data = {}
        for line in block.splitlines():
            match = re.match(r"^\s*(.*?)\s*:\s*(.*?)\s*$", line)
            if match and match.group(1).strip():
                data[match.group(1).strip().lower()] = match.group(2).strip()
        alias = data.get("name") or data.get("名称") or ""
        ssid = data.get("ssid") or ""
        if alias and ssid:
            result[alias.strip().lower()] = ssid.strip()
    return result


def _is_virtual_adapter(description: str) -> bool:
    lowered = description.lower()
    return any(keyword in lowered for keyword in VIRTUAL_KEYWORDS)


@dataclass
class Connection:
    adapter: str
    index: int
    kind: str          # wifi / ethernet
    name: str          # SSID 或适配器名称
    ip: str
    prefix: int
    gateway: str
    dhcp: bool
    dns: Tuple[str, ...]
    description: str = ""


def get_connections() -> List[Connection]:
    adapters = _get_gaa_adapters()
    info_map = _get_adapter_info_map()
    ssid_map = get_wifi_ssids()

    connections = []
    for adapter in adapters:
        if adapter["oper"] != IF_OPER_STATUS_UP:
            continue
        if adapter["iftype"] not in (IF_TYPE_ETHERNET, IF_TYPE_IEEE80211):
            continue
        if not adapter["ip"]:
            continue
        if _is_virtual_adapter(adapter["description"]):
            continue

        info = info_map.get(adapter["guid_key"], {})
        mask = info.get("mask") or ""
        prefix = mask_to_prefix(mask) if mask else None
        if prefix is None:
            prefix = 0
            mask = "0.0.0.0"
        gateway = adapter["gateway"] or info.get("gateway") or ""

        is_wifi = adapter["iftype"] == IF_TYPE_IEEE80211
        if is_wifi:
            kind = "wifi"
            name = ssid_map.get(adapter["friendly"].lower()) \
                or adapter["friendly"] or info.get("name", "")
        else:
            kind = "ethernet"
            name = adapter["friendly"]
        connections.append(Connection(
            adapter=adapter["friendly"],
            index=0,
            kind=kind,
            name=name,
            ip=adapter["ip"],
            prefix=prefix,
            gateway=gateway,
            dhcp=bool(info.get("dhcp", False)),
            dns=tuple(adapter["dns"]),
            description=adapter["description"],
        ))
    connections.sort(key=lambda item: (0 if item.kind == "wifi" else 1, item.adapter.lower()))
    return connections


# ---------------------------------------------------------------------------
# 规则匹配与应用
# ---------------------------------------------------------------------------

@dataclass
class Action:
    conn: Connection
    mode: str                     # static / dhcp
    ip: str = ""
    mask: str = ""
    gateway: str = ""
    dns: Tuple[str, ...] = ()
    matched: str = ""

    def describe(self) -> str:
        prefix = "[规则 %s] " % self.matched if self.matched else ""
        if self.mode == "dhcp":
            return "%s%s（%s）→ 自动获取 IP（DHCP）" % (
                prefix, self.conn.adapter, self.conn.name)
        text = "%s%s（%s）→ 固定 IP %s / %s" % (
            prefix, self.conn.adapter, self.conn.name, self.ip, self.mask)
        if self.gateway:
            text += "，网关 %s" % self.gateway
        if self.dns:
            text += "，DNS %s" % ", ".join(self.dns)
        return text


def rule_matches(rule: dict, conn: Connection) -> bool:
    rule_type = str(rule.get("type") or "wifi")
    match_name = str(rule.get("match") or "").strip().casefold()
    if not match_name:
        return False
    if rule_type == "wifi":
        names = [conn.name] if conn.kind == "wifi" else []
    elif rule_type == "ethernet":
        names = [conn.name, conn.adapter] if conn.kind == "ethernet" else []
    else:
        names = [conn.name, conn.adapter]
    return any(name.strip().casefold() == match_name for name in names if name)


def find_rule(profiles: List[dict], conn: Connection) -> Optional[dict]:
    exact = [p for p in profiles if str(p.get("type") or "wifi") == conn.kind]
    wildcard = [p for p in profiles if str(p.get("type") or "wifi") == "any"]
    for rule in exact + wildcard:
        if rule_matches(rule, conn):
            return rule
    return None


def type_is_covered(profiles: List[dict], conn: Connection) -> bool:
    return any(
        str(p.get("type") or "wifi") in (conn.kind, "any") for p in profiles
    )


def plan_action(config: dict, conn: Connection, rule: Optional[dict],
                profiles: List[dict]) -> Optional[Action]:
    if rule is None:
        if not config.get("revertToDhcpWhenUnmatched", True):
            return None
        if not type_is_covered(profiles, conn):
            return None
        if conn.dhcp:
            return None
        return Action(conn=conn, mode="dhcp")

    mode = str(rule.get("mode") or "static")
    if mode == "dhcp":
        if conn.dhcp:
            return None
        return Action(conn=conn, mode="dhcp")

    ip = str(rule.get("ip") or "").strip()
    mask = str(rule.get("mask") or "").strip() or "255.255.255.0"
    gateway = str(rule.get("gateway") or "").strip()
    raw_dns = rule.get("dns") or []
    dns = tuple(str(item).strip() for item in raw_dns if str(item).strip()) \
        if isinstance(raw_dns, list) else ()
    netmask_prefix = mask_to_prefix(mask)
    if not is_valid_ipv4(ip) or netmask_prefix is None:
        return None
    if gateway and not is_valid_ipv4(gateway):
        return None
    if not conn.dhcp and conn.ip == ip and conn.prefix == netmask_prefix \
            and (not gateway or (conn.gateway or "") == gateway) \
            and (not dns or tuple(conn.dns) == dns):
        return None
    return Action(
        conn=conn, mode="static", ip=ip, mask=mask,
        gateway=gateway, dns=dns, matched=str(rule.get("match") or ""),
    )


def _netsh(args: List[str], ignore_if_already: bool = False) -> None:
    code, output = run_command(["netsh"] + args, timeout=25)
    if code != 0:
        text = decode_native(output).strip()
        if ignore_if_already and (
                "already enabled" in text.lower()
                or "already configured" in text.lower()
                or "已启用" in text
                or "已配置" in text):
            return
        raise RuntimeError(text or "netsh 执行失败（代码 %s）" % code)


def execute_action(action: Action) -> None:
    alias = action.conn.adapter
    name_arg = 'name="%s"' % alias
    if action.mode == "dhcp":
        _netsh(["interface", "ipv4", "set", "address", name_arg, "source=dhcp"],
               ignore_if_already=True)
        _netsh(["interface", "ipv4", "set", "dnsservers", name_arg, "source=dhcp"],
               ignore_if_already=True)
    else:
        args = ["interface", "ipv4", "set", "address", name_arg,
                "static", action.ip, action.mask]
        if action.gateway:
            args.append(action.gateway)
        _netsh(args)
        # 规则未填写 DNS 时，沿用切换前的 DNS（通常是当前网络的 DHCP DNS），
        # 避免设置固定 IP 后 Windows 清空 DNS 导致无法上网。
        dns_servers = action.dns or tuple(action.conn.dns)
        if dns_servers:
            _netsh(["interface", "ipv4", "set", "dnsservers",
                    name_arg, "static", dns_servers[0], "primary"])
            for index, server in enumerate(dns_servers[1:], start=2):
                _netsh(["interface", "ipv4", "add", "dnsservers",
                        name_arg, server, "index=%d" % index])
    try:
        run_command(["ipconfig", "/flushdns"], timeout=10)
    except RuntimeError:
        pass


def switch_once(config: dict, execute: bool = True) -> Tuple[List[str], List[str]]:
    messages, errors = [], []
    try:
        connections = get_connections()
    except Exception as exc:
        return [], ["读取网络状态失败：%s" % exc]
    profiles = config.get("profiles") or []
    for conn in connections:
        rule = find_rule(profiles, conn)
        action = plan_action(config, conn, rule, profiles)
        if action is None:
            continue
        description = action.describe()
        if not execute:
            messages.append("[预览] " + description)
            continue
        try:
            execute_action(action)
            messages.append(description)
        except Exception as exc:
            errors.append("%s 失败：%s" % (description, exc))
    return messages, errors


# ---------------------------------------------------------------------------
# 后台监控进程
# ---------------------------------------------------------------------------

def _append_log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if WATCHER_LOG.exists() and WATCHER_LOG.stat().st_size > 1_000_000:
            backup = WATCHER_LOG.with_name("watcher.log.1")
            try:
                os.replace(str(WATCHER_LOG), str(backup))
            except OSError:
                pass
        line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
        with open(WATCHER_LOG, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _create_watcher_mutex():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    return handle, already


def watcher_running() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def read_watcher_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def run_watcher() -> int:
    if not is_admin():
        _append_log("后台监控需要管理员权限，已退出")
        return 2
    handle, already = _create_watcher_mutex()
    if already or not handle:
        _append_log("检测到已有后台监控在运行，本次启动退出")
        return 0
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
        _append_log("后台监控已启动（PID %s）" % os.getpid())
        last_signature = None
        last_error = None
        while True:
            interval = 5
            try:
                config = load_config()
                interval = max(2, int(config.get("pollSeconds", 5)))
                connections = get_connections()
                signature = tuple(
                    (item.adapter, item.name, item.ip, item.dhcp, item.gateway)
                    for item in connections
                )
                if signature != last_signature:
                    names = ", ".join(
                        "%s/%s" % (item.adapter, item.name) for item in connections
                    ) or "未连接网络"
                    _append_log("当前网络：" + names)
                    last_signature = signature
                profiles = config.get("profiles") or []
                for conn in connections:
                    rule = find_rule(profiles, conn)
                    action = plan_action(config, conn, rule, profiles)
                    if action is None:
                        continue
                    execute_action(action)
                    _append_log("已应用：" + action.describe())
                last_error = None
            except Exception as exc:
                message = "执行失败：%s" % exc
                if message != last_error:
                    _append_log(message)
                    last_error = message
            time.sleep(interval)
    finally:
        try:
            if read_watcher_pid() == os.getpid():
                PID_FILE.unlink()
        except OSError:
            pass
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass
        _append_log("后台监控已停止")
    return 0


def start_watcher_process() -> subprocess.Popen:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(
        [str(pythonw_executable()), str(SCRIPT_PATH), "--watch"],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )


def stop_watcher_process() -> bool:
    pid = read_watcher_pid()
    if pid:
        run_command(["taskkill", "/F", "/PID", str(pid)], timeout=15)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    time.sleep(0.3)
    return not watcher_running()


# ---------------------------------------------------------------------------
# 开机自启动（计划任务，最高权限、无窗口）
# ---------------------------------------------------------------------------

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>按网络名称自动切换 IP、网关与 DNS</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def autostart_installed() -> bool:
    code, _ = run_command(["schtasks", "/Query", "/TN", TASK_NAME], timeout=15)
    return code == 0


def install_autostart() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    arguments = '"%s" --watch' % SCRIPT_PATH
    xml = TASK_XML.format(
        user=escape(current_user_name()),
        command=escape(str(pythonw_executable())),
        arguments=escape(arguments, {'"': "&quot;"}),
        workdir=escape(str(BASE_DIR)),
    )
    fd, xml_path = tempfile.mkstemp(
        prefix="wifi_ip_task_", suffix=".xml", dir=str(LOG_DIR))
    os.close(fd)
    try:
        Path(xml_path).write_text(xml, encoding="utf-16")
        code, output = run_command(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
            timeout=30,
        )
        if code != 0:
            raise RuntimeError(decode_native(output).strip() or "创建计划任务失败")
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass


def uninstall_autostart() -> None:
    if not autostart_installed():
        return
    code, output = run_command(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], timeout=20)
    if code != 0:
        raise RuntimeError(decode_native(output).strip() or "删除计划任务失败")


# ---------------------------------------------------------------------------
# 图形界面
# ---------------------------------------------------------------------------

class ProfileDialog(tk.Toplevel):
    """添加 / 编辑一条网络规则。"""

    def __init__(self, parent, profile: Optional[dict] = None,
                 defaults: Optional[dict] = None):
        super().__init__(parent)
        self.result = None
        source = profile if profile is not None else (defaults or {})
        self.title("编辑规则" if profile is not None else "添加规则")
        self.resizable(False, False)
        self.transient(parent)

        self.type_var = tk.StringVar(
            value=TYPE_LABELS.get(str(source.get("type") or "wifi"), TYPE_LABELS["wifi"]))
        self.match_var = tk.StringVar(value=str(source.get("match") or ""))
        self.mode_var = tk.StringVar(
            value=MODE_LABELS.get(str(source.get("mode") or "static"), MODE_LABELS["static"]))
        self.ip_var = tk.StringVar(value=str(source.get("ip") or "192.168.1.100"))
        self.mask_var = tk.StringVar(value=str(source.get("mask") or "255.255.255.0"))
        self.gateway_var = tk.StringVar(value=str(source.get("gateway") or "192.168.1.1"))
        dns_value = source.get("dns") or []
        self.dns_var = tk.StringVar(
            value=", ".join(dns_value) if isinstance(dns_value, list) else str(dns_value))

        body = ttk.Frame(self, padding=(16, 14, 16, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text="网络类型").grid(row=0, column=0, sticky="w", pady=4, padx=(0, 10))
        ttk.Combobox(
            body, textvariable=self.type_var, state="readonly", width=26,
            values=[TYPE_LABELS["wifi"], TYPE_LABELS["ethernet"], TYPE_LABELS["any"]],
        ).grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(body, text="网络名称").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        self.match_entry = ttk.Entry(body, textvariable=self.match_var, width=30)
        self.match_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(
            body, text="Wi-Fi 填 SSID；有线网络填适配器名称（如“以太网”）",
            foreground="#777777", font=("Microsoft YaHei UI", 8),
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))

        ttk.Label(body, text="获取方式").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 10))
        mode_box = ttk.Combobox(
            body, textvariable=self.mode_var, state="readonly", width=26,
            values=[MODE_LABELS["static"], MODE_LABELS["dhcp"]],
        )
        mode_box.grid(row=3, column=1, sticky="ew", pady=4)
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._sync_mode())

        self.ip_label = ttk.Label(body, text="IP 地址")
        self.ip_label.grid(row=4, column=0, sticky="w", pady=4, padx=(0, 10))
        self.ip_entry = ttk.Entry(body, textvariable=self.ip_var, width=30)
        self.ip_entry.grid(row=4, column=1, sticky="ew", pady=4)

        self.mask_label = ttk.Label(body, text="子网掩码")
        self.mask_label.grid(row=5, column=0, sticky="w", pady=4, padx=(0, 10))
        self.mask_entry = ttk.Entry(body, textvariable=self.mask_var, width=30)
        self.mask_entry.grid(row=5, column=1, sticky="ew", pady=4)

        self.gateway_label = ttk.Label(body, text="默认网关")
        self.gateway_label.grid(row=6, column=0, sticky="w", pady=4, padx=(0, 10))
        self.gateway_entry = ttk.Entry(body, textvariable=self.gateway_var, width=30)
        self.gateway_entry.grid(row=6, column=1, sticky="ew", pady=4)

        self.dns_label = ttk.Label(body, text="DNS 服务器")
        self.dns_label.grid(row=7, column=0, sticky="w", pady=4, padx=(0, 10))
        self.dns_entry = ttk.Entry(body, textvariable=self.dns_var, width=30)
        self.dns_entry.grid(row=7, column=1, sticky="ew", pady=4)
        ttk.Label(
            body, text="可留空（沿用当前网络的 DNS）；多个地址用英文逗号分隔",
            foreground="#777777", font=("Microsoft YaHei UI", 8),
        ).grid(row=8, column=1, sticky="w")

        buttons = ttk.Frame(self, padding=(16, 0, 16, 14))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", width=10, command=self._cancel).pack(side="right")
        ttk.Button(buttons, text="确定", width=10, command=self._ok).pack(side="right", padx=(0, 8))

        self._sync_mode()
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.update_idletasks()
        width, height = self.winfo_width(), self.winfo_height()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.geometry("+%d+%d" % (x, y))
        self.grab_set()
        self.match_entry.focus_set()
        self.wait_window(self)

    def _sync_mode(self) -> None:
        normal = MODE_KEYS.get(self.mode_var.get()) == "static"
        state = "normal" if normal else "disabled"
        for widget in (self.ip_entry, self.mask_entry, self.gateway_entry, self.dns_entry):
            widget.configure(state=state)
        for widget in (self.ip_label, self.mask_label, self.gateway_label, self.dns_label):
            widget.configure(foreground="#000000" if normal else "#888888")

    def _ok(self) -> None:
        match_name = self.match_var.get().strip()
        if not match_name:
            messagebox.showerror("提示", "请填写网络名称。", parent=self)
            return
        rule_type = TYPE_KEYS.get(self.type_var.get(), "wifi")
        mode = MODE_KEYS.get(self.mode_var.get(), "static")
        result = {"type": rule_type, "match": match_name, "mode": mode}
        if mode == "static":
            ip = self.ip_var.get().strip()
            mask = self.mask_var.get().strip()
            gateway = self.gateway_var.get().strip()
            if not is_valid_ipv4(ip):
                messagebox.showerror("提示", "IP 地址格式不正确。", parent=self)
                return
            if mask_to_prefix(mask) is None:
                messagebox.showerror("提示", "子网掩码格式不正确。", parent=self)
                return
            if gateway and not is_valid_ipv4(gateway):
                messagebox.showerror("提示", "默认网关格式不正确。", parent=self)
                return
            dns = [part.strip() for part in re.split(r"[,，;；]", self.dns_var.get())
                   if part.strip()]
            for server in dns:
                if not is_valid_ipv4(server):
                    messagebox.showerror("提示", "DNS 地址格式不正确：%s" % server, parent=self)
                    return
            result.update({"ip": ip, "mask": mask, "gateway": gateway, "dns": dns})
        self.result = result
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class App(tk.Tk):
    def __init__(self, config: dict):
        super().__init__()
        self.config_data = config
        self.connections: List[Connection] = []
        self.closing = False
        self.refreshing = False
        self.busy = False
        self.last_status_error = ""
        self.ui_queue = queue.Queue()
        self._drain_id = None
        self._periodic_id = None
        self._verify_id = None

        self.title("%s - 按网络名称自动切换 IP" % APP_TITLE)
        self.geometry("1000x800")
        self.minsize(940, 700)
        self.option_add("*Font", UI_FONT)
        try:
            style = ttk.Style(self)
            style.theme_use("vista")
            style.configure("Treeview", rowheight=26, font=UI_FONT)
            style.configure("Treeview.Heading", font=UI_FONT_BOLD)
            style.configure("TLabel", font=UI_FONT)
            style.configure("TButton", font=UI_FONT)
            style.configure("TLabelframe.Label", font=UI_FONT_BOLD)
        except tk.TclError:
            pass

        self.poll_var = tk.StringVar(value=str(self.config_data.get("pollSeconds", 5)))
        self.revert_var = tk.BooleanVar(
            value=bool(self.config_data.get("revertToDhcpWhenUnmatched", True)))
        self.autostart_var = tk.BooleanVar(value=False)
        self.monitor_text = tk.StringVar(value="后台监控：检测中…")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_id = self.after(80, self._drain_queue)
        self.after(200, self.refresh_status)
        self._periodic_id = self.after(4000, self._periodic_refresh)
        threading.Thread(target=self._load_autostart_state, daemon=True).start()

    # ---- 布局 --------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(12, 10, 12, 8))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 8))
        ttk.Label(
            header, text=APP_TITLE, font=("Microsoft YaHei UI", 16, "bold"),
        ).pack(side="left")
        ttk.Label(
            header,
            text="按连接到的 Wi-Fi SSID / 有线适配器，自动应用对应的 IP、网关与 DNS",
            foreground="#666666",
        ).pack(side="left", padx=(14, 0), pady=(4, 0))

        status_frame = ttk.LabelFrame(outer, text=" 当前网络 ", padding=(10, 8))
        status_frame.pack(fill="x")

        status_columns = ("adapter", "type", "name", "ip", "mask", "gateway", "mode")
        self.status_tree = ttk.Treeview(
            status_frame, columns=status_columns, show="headings", height=2,
            selectmode="none",
        )
        status_specs = [
            ("adapter", "适配器", 110, "w"),
            ("type", "类型", 80, "center"),
            ("name", "网络名称", 210, "w"),
            ("ip", "当前 IP", 130, "center"),
            ("mask", "子网掩码", 120, "center"),
            ("gateway", "网关", 130, "center"),
            ("mode", "获取方式", 100, "center"),
        ]
        for key, title, width, anchor in status_specs:
            self.status_tree.heading(key, text=title)
            self.status_tree.column(key, width=width, anchor=anchor,
                                    stretch=(key == "name"))
        self.status_tree.pack(fill="x")

        status_bottom = ttk.Frame(status_frame)
        status_bottom.pack(fill="x", pady=(8, 0))
        self.monitor_label = ttk.Label(
            status_bottom, textvariable=self.monitor_text, foreground="#666666")
        self.monitor_label.pack(side="left")
        self.apply_button = ttk.Button(
            status_bottom, text="立即应用", width=12, command=self.apply_now)
        self.apply_button.pack(side="right")
        ttk.Button(
            status_bottom, text="刷新状态", width=10, command=self.refresh_status
        ).pack(side="right", padx=(0, 8))

        rules_frame = ttk.LabelFrame(outer, text=" 切换规则 ", padding=(10, 8))
        rules_frame.pack(fill="both", expand=True, pady=(10, 0))

        toolbar = ttk.Frame(rules_frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Label(
            toolbar, text="同一网络按从上到下顺序匹配；Wi-Fi 规则优先于“任意网络”规则",
            foreground="#666666",
        ).pack(side="left")
        ttk.Button(toolbar, text="添加规则", width=10, command=self.add_rule).pack(side="right")
        ttk.Button(toolbar, text="编辑规则", width=10, command=self.edit_rule).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="删除规则", width=10, command=self.delete_rule).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="下移", width=6, command=lambda: self.move_rule(1)).pack(side="right", padx=(0, 6))
        ttk.Button(toolbar, text="上移", width=6, command=lambda: self.move_rule(-1)).pack(side="right", padx=(0, 6))

        rule_columns = ("type", "match", "mode", "ip", "mask", "gateway", "dns")
        self.rules_tree = ttk.Treeview(
            rules_frame, columns=rule_columns, show="headings", height=7,
        )
        rule_specs = [
            ("type", "类型", 100, "center"),
            ("match", "网络名称", 210, "w"),
            ("mode", "获取方式", 120, "center"),
            ("ip", "IP 地址", 140, "center"),
            ("mask", "子网掩码", 130, "center"),
            ("gateway", "网关", 140, "center"),
            ("dns", "DNS", 180, "w"),
        ]
        for key, title, width, anchor in rule_specs:
            self.rules_tree.heading(key, text=title)
            self.rules_tree.column(key, width=width, anchor=anchor,
                                   stretch=(key in ("match", "dns")))
        self.rules_tree.pack(fill="both", expand=True)
        self.rules_tree.bind("<Double-1>", lambda _event: self.edit_rule())
        self.rules_tree.tag_configure("odd", background="#f5f8fc")

        options = ttk.LabelFrame(outer, text=" 选项 ", padding=(10, 8))
        options.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(options)
        row1.pack(fill="x")
        ttk.Label(row1, text="轮询间隔").pack(side="left")
        ttk.Spinbox(row1, from_=2, to=3600, width=5,
                    textvariable=self.poll_var).pack(side="left", padx=(6, 4))
        ttk.Label(row1, text="秒").pack(side="left")

        self.revert_check = ttk.Checkbutton(
            row1, text="未匹配网络时恢复自动获取（DHCP）",
            variable=self.revert_var)
        self.revert_check.pack(side="left", padx=(22, 0))
        ttk.Label(row1, text="仅对已配置规则的网络类型生效",
                  foreground="#777777").pack(side="left", padx=(6, 0))

        self.autostart_check = ttk.Checkbutton(
            row1, text="开机自动启动", variable=self.autostart_var,
            command=self.on_autostart_toggle)
        self.autostart_check.pack(side="left", padx=(22, 0))

        ttk.Button(row1, text="保存设置", width=10,
                   command=self.save_settings).pack(side="right")

        row2 = ttk.Frame(options)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="启动后台监控", width=14,
                   command=self.start_monitor).pack(side="left")
        ttk.Button(row2, text="停止后台监控", width=14,
                   command=self.stop_monitor).pack(side="left", padx=(8, 0))
        ttk.Button(row2, text="打开日志目录", width=12,
                   command=self.open_logs).pack(side="left", padx=(8, 0))
        ttk.Label(
            row2, text="后台监控独立运行，关闭窗口不影响自动切换",
            foreground="#777777",
        ).pack(side="left", padx=(14, 0))

        log_frame = ttk.LabelFrame(outer, text=" 运行日志 ", padding=(8, 6))
        log_frame.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(
            log_frame, height=7, wrap="word", state="disabled",
            relief="solid", borderwidth=1, font=UI_FONT, background="#fcfcfc",
        )
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        ttk.Label(
            outer,
            text="配置文件：%s    提示：修改规则后，后台监控会在下一轮询周期自动生效" % CONFIG_PATH,
            foreground="#777777", font=("Microsoft YaHei UI", 8),
        ).pack(fill="x", pady=(6, 0))

        self.render_rules()
        self.status_tree.insert(
            "", "end", values=("—", "—", "正在检测网络…", "—", "—", "—", "—"))
        self.log("程序已启动，以管理员身份运行：%s" % ("是" if is_admin() else "否"))
        if not (self.config_data.get("profiles") or []):
            self.log("提示：请先点击“添加规则”配置网络与固定 IP，再启动后台监控。")

    # ---- UI 线程队列 -------------------------------------------------------

    def _post(self, func, *args) -> None:
        self.ui_queue.put((func, args))

    def _drain_queue(self) -> None:
        while True:
            try:
                func, args = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except Exception:
                pass
        if not self.closing:
            self._drain_id = self.after(80, self._drain_queue)

    # ---- 日志 --------------------------------------------------------------

    def log(self, message: str) -> None:
        if self.closing:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "[%s] %s\n" % (stamp, message))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- 状态刷新 ----------------------------------------------------------

    def _periodic_refresh(self) -> None:
        if self.closing:
            return
        self.refresh_status()
        self._periodic_id = self.after(4000, self._periodic_refresh)

    def refresh_status(self) -> None:
        if self.closing or self.refreshing:
            return
        self.refreshing = True
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self) -> None:
        connections, error = [], ""
        try:
            connections = get_connections()
        except Exception as exc:
            error = str(exc)
        running = watcher_running()
        self._post(self._apply_status, connections, error, running)

    def _apply_status(self, connections: List[Connection], error: str, running: bool) -> None:
        self.refreshing = False
        if self.closing:
            return
        if error:
            if error != self.last_status_error:
                self.log("状态查询失败：%s" % error)
                self.last_status_error = error
        else:
            self.last_status_error = ""
            self.connections = connections
            self.render_connections()
        self._update_monitor_label(running)

    def render_connections(self) -> None:
        self.status_tree.delete(*self.status_tree.get_children())
        if not self.connections:
            self.status_tree.insert("", "end",
                                    values=("—", "—", "尚未连接到网络", "—", "—", "—", "—"))
            return
        for conn in self.connections:
            mode = "DHCP" if conn.dhcp else "静态"
            self.status_tree.insert("", "end", values=(
                conn.adapter,
                "Wi-Fi" if conn.kind == "wifi" else "有线",
                conn.name,
                conn.ip or "—",
                prefix_to_mask(conn.prefix) if conn.ip else "—",
                conn.gateway or "—",
                mode,
            ))

    def _update_monitor_label(self, running: bool) -> None:
        if running:
            self.monitor_text.set("后台监控：运行中")
            self.monitor_label.configure(foreground="#18794e")
        else:
            self.monitor_text.set("后台监控：已停止")
            self.monitor_label.configure(foreground="#b42318")

    # ---- 规则管理 ----------------------------------------------------------

    def render_rules(self, select_index: Optional[int] = None) -> None:
        self.rules_tree.delete(*self.rules_tree.get_children())
        profiles = self.config_data.get("profiles") or []
        for index, rule in enumerate(profiles):
            is_dhcp = str(rule.get("mode") or "static") == "dhcp"
            dns_value = rule.get("dns") or []
            dns_text = ", ".join(dns_value) if isinstance(dns_value, list) else str(dns_value)
            values = (
                TYPE_LABELS.get(str(rule.get("type") or "wifi"), TYPE_LABELS["wifi"]),
                rule.get("match") or "",
                MODE_LABELS.get(str(rule.get("mode") or "static"), MODE_LABELS["static"]),
                "—" if is_dhcp else (rule.get("ip") or "—"),
                "—" if is_dhcp else (rule.get("mask") or "—"),
                "—" if is_dhcp else (rule.get("gateway") or "—"),
                "—" if is_dhcp else (dns_text or "—"),
            )
            tag = "odd" if index % 2 else ""
            self.rules_tree.insert("", "end", iid=str(index), values=values,
                                   tags=(tag,) if tag else ())
        if select_index is not None and 0 <= select_index < len(profiles):
            self.rules_tree.selection_set(str(select_index))
            self.rules_tree.focus(str(select_index))

    def _selected_rule_index(self) -> Optional[int]:
        selection = self.rules_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return None

    def _default_rule(self) -> dict:
        wifi = next((c for c in self.connections if c.kind == "wifi"), None)
        if wifi:
            return {"type": "wifi", "match": wifi.name}
        wired = next((c for c in self.connections if c.kind == "ethernet"), None)
        if wired:
            return {"type": "ethernet", "match": wired.adapter}
        return {"type": "wifi", "match": ""}

    def add_rule(self) -> None:
        dialog = ProfileDialog(self, None, self._default_rule())
        if not dialog.result:
            return
        self.config_data.setdefault("profiles", []).append(dialog.result)
        try:
            save_config(self.config_data)
        except Exception as exc:
            messagebox.showerror("保存失败", "写入配置文件失败：%s" % exc, parent=self)
            return
        self.render_rules(len(self.config_data["profiles"]) - 1)
        self.log("已添加规则：%s → %s" % (
            dialog.result.get("match"),
            "DHCP" if dialog.result.get("mode") == "dhcp" else dialog.result.get("ip")))
        self.refresh_status()

    def edit_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先在列表中选择一条规则。", parent=self)
            return
        profiles = self.config_data.get("profiles") or []
        if not (0 <= index < len(profiles)):
            return
        dialog = ProfileDialog(self, profiles[index])
        if not dialog.result:
            return
        profiles[index] = dialog.result
        try:
            save_config(self.config_data)
        except Exception as exc:
            messagebox.showerror("保存失败", "写入配置文件失败：%s" % exc, parent=self)
            return
        self.render_rules(index)
        self.log("已修改规则：%s" % dialog.result.get("match"))
        self.refresh_status()

    def delete_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先在列表中选择一条规则。", parent=self)
            return
        profiles = self.config_data.get("profiles") or []
        if not (0 <= index < len(profiles)):
            return
        name = profiles[index].get("match") or ""
        if not messagebox.askyesno("删除规则", "确定删除规则“%s”吗？" % name, parent=self):
            return
        profiles.pop(index)
        try:
            save_config(self.config_data)
        except Exception as exc:
            messagebox.showerror("保存失败", "写入配置文件失败：%s" % exc, parent=self)
            return
        self.render_rules(min(index, len(profiles) - 1))
        self.log("已删除规则：%s" % name)

    def move_rule(self, offset: int) -> None:
        index = self._selected_rule_index()
        if index is None:
            messagebox.showinfo("提示", "请先在列表中选择一条规则。", parent=self)
            return
        profiles = self.config_data.get("profiles") or []
        target = index + offset
        if not (0 <= target < len(profiles)):
            return
        profiles[index], profiles[target] = profiles[target], profiles[index]
        try:
            save_config(self.config_data)
        except Exception as exc:
            messagebox.showerror("保存失败", "写入配置文件失败：%s" % exc, parent=self)
            return
        self.render_rules(target)

    # ---- 设置 --------------------------------------------------------------

    def save_settings(self) -> None:
        try:
            poll = int(str(self.poll_var.get()).strip())
        except ValueError:
            messagebox.showerror("提示", "轮询间隔必须是 2-3600 之间的整数。", parent=self)
            return
        if not 2 <= poll <= 3600:
            messagebox.showerror("提示", "轮询间隔必须是 2-3600 之间的整数。", parent=self)
            return
        self.config_data["pollSeconds"] = poll
        self.config_data["revertToDhcpWhenUnmatched"] = bool(self.revert_var.get())
        try:
            save_config(self.config_data)
        except Exception as exc:
            messagebox.showerror("保存失败", "写入配置文件失败：%s" % exc, parent=self)
            return
        self.log("设置已保存：轮询间隔 %s 秒，未匹配网络%s恢复 DHCP" % (
            poll, "时" if self.config_data["revertToDhcpWhenUnmatched"] else "时不"))
        self.refresh_status()

    def _load_autostart_state(self) -> None:
        installed = autostart_installed()
        self._post(self._set_autostart_state, installed)

    def _set_autostart_state(self, installed: bool) -> None:
        if not self.closing:
            self.autostart_var.set(installed)

    def on_autostart_toggle(self) -> None:
        want = bool(self.autostart_var.get())
        try:
            if want:
                install_autostart()
            else:
                uninstall_autostart()
        except Exception as exc:
            self.autostart_var.set(not want)
            messagebox.showerror("设置失败", str(exc), parent=self)
            self.log("开机自启设置失败：%s" % exc)
            return
        self.log("已开启开机自动启动" if want else "已取消开机自动启动")

    # ---- 监控控制 ----------------------------------------------------------

    def start_monitor(self) -> None:
        if watcher_running():
            messagebox.showinfo("提示", "后台监控已经在运行。", parent=self)
            return
        if not is_admin():
            messagebox.showerror("权限不足",
                                 "启动后台监控需要管理员权限，请重新以管理员身份运行。",
                                 parent=self)
            return
        try:
            start_watcher_process()
        except Exception as exc:
            messagebox.showerror("启动失败", "无法启动后台监控：%s" % exc, parent=self)
            return
        self.log("正在启动后台监控…")
        self._verify_id = self.after(500, self._verify_watcher_started)

    def _verify_watcher_started(self, attempts: int = 0) -> None:
        if self.closing:
            return
        if watcher_running():
            self._update_monitor_label(True)
            self.log("后台监控已启动")
            self.refresh_status()
            return
        if attempts >= 20:
            self._update_monitor_label(False)
            self.log("后台监控启动失败，请查看 logs\\watcher.log")
            messagebox.showerror("启动失败",
                                 "后台监控未能启动，请查看 logs\\watcher.log。",
                                 parent=self)
            return
        self._verify_id = self.after(
            500, lambda: self._verify_watcher_started(attempts + 1))

    def stop_monitor(self) -> None:
        if not watcher_running():
            messagebox.showinfo("提示", "后台监控当前没有运行。", parent=self)
            return
        self.log("正在停止后台监控…")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self) -> None:
        ok = stop_watcher_process()
        self._post(self._after_stop, ok)

    def _after_stop(self, ok: bool) -> None:
        if ok:
            self.log("后台监控已停止")
            self._update_monitor_label(False)
        else:
            running = watcher_running()
            self._update_monitor_label(running)
            self.log("停止后台监控失败")
            messagebox.showerror("停止失败", "无法停止后台监控进程。", parent=self)

    def open_logs(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(LOG_DIR))
        except Exception as exc:
            messagebox.showerror("提示", "无法打开日志目录：%s" % exc, parent=self)

    # ---- 立即应用 ----------------------------------------------------------

    def apply_now(self) -> None:
        if self.busy:
            return
        if not is_admin():
            messagebox.showerror("权限不足",
                                 "修改网络设置需要管理员权限，请重新以管理员身份运行。",
                                 parent=self)
            return
        self.busy = True
        self.apply_button.configure(state="disabled")
        self.log("正在按当前规则应用网络设置…")
        threading.Thread(target=self._apply_worker, daemon=True).start()

    def _apply_worker(self) -> None:
        messages, errors = switch_once(self.config_data, execute=True)
        self._post(self._apply_done, messages, errors)

    def _apply_done(self, messages: List[str], errors: List[str]) -> None:
        self.busy = False
        if self.closing:
            return
        self.apply_button.configure(state="normal")
        if not messages and not errors:
            self.log("当前网络已符合规则，无需修改")
        for message in messages:
            self.log("已应用：" + message)
        for message in errors:
            self.log("失败：" + message)
        if errors:
            messagebox.showwarning("部分操作失败", "\n".join(errors), parent=self)
        self.refresh_status()

    # ---- 退出 --------------------------------------------------------------

    def _on_close(self) -> None:
        self.closing = True
        for after_id in (self._drain_id, self._periodic_id, self._verify_id):
            if after_id:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        self.destroy()


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------

def diagnose() -> int:
    print("配置文件：%s" % CONFIG_PATH)
    try:
        cfg = load_config()
    except Exception as exc:
        print("读取配置失败：%s" % exc)
        return 1
    print("未匹配时恢复 DHCP：%s；规则数量：%s" % (
        cfg.get("revertToDhcpWhenUnmatched"), len(cfg.get("profiles") or [])))
    try:
        connections = get_connections()
    except Exception as exc:
        print("读取网络状态失败：%s" % exc)
        return 1
    if not connections:
        print("当前没有已连接的物理网络")
        return 0
    profiles = cfg.get("profiles") or []
    for conn in connections:
        rule = find_rule(profiles, conn)
        action = plan_action(cfg, conn, rule, profiles)
        print("适配器：%s（%s）" % (conn.adapter, "Wi-Fi" if conn.kind == "wifi" else "有线"))
        print("  网络名称：%s" % conn.name)
        print("  当前配置：%s/%s 网关=%s 方式=%s DNS=%s" % (
            conn.ip or "—", prefix_to_mask(conn.prefix) if conn.ip else "—",
            conn.gateway or "—", "DHCP" if conn.dhcp else "静态",
            ", ".join(conn.dns) or "—"))
        print("  匹配规则：%s" % (rule.get("match") if rule else "无"))
        print("  需要执行：%s" % (action.describe() if action else "无需修改"))
    return 0


def main() -> int:
    if "--watch" in sys.argv:
        return run_watcher()
    if "--diagnose" in sys.argv:
        return diagnose()

    if not is_admin():
        if relaunch_as_admin():
            return 0
        ctypes.windll.user32.MessageBoxW(
            None, "需要管理员权限才能修改网络设置。", APP_TITLE, 0x10)
        return 1

    try:
        config = load_config()
    except Exception as exc:
        ctypes.windll.user32.MessageBoxW(
            None, "配置文件读取失败，将使用默认配置：\n%s" % exc, APP_TITLE, 0x30)
        config = normalize_config({})

    app = App(config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())