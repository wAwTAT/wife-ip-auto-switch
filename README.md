# 网络 IP 自动切换

> 一个面向 Windows 的小工具：根据当前连接的 Wi-Fi SSID 或有线适配器名称，自动切换对应的固定 IP、网关与 DNS。

[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/python-3.7%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 功能特性

- **按网络名称自动切换**：Wi-Fi 按 SSID 匹配，有线网络按适配器名称匹配。
- **固定 IP / 网关 / DNS**：每条规则可设置 IP、子网掩码、默认网关和 DNS。
- **自动回退 DHCP**：连接未配置规则的网络时，可自动恢复为自动获取 IP。
- **后台监控**：独立后台进程轮询，关闭主界面不影响自动切换。
- **开机自启**：可选创建计划任务，以最高权限、无窗口方式在登录时启动。
- **无终端黑框**：所有 `netsh` / `schtasks` 子进程均以 `CREATE_NO_WINDOW` 运行。
- **纯 Python 标准库**：无第三方依赖，界面使用 tkinter，网络状态通过 Windows IP Helper API 读取。
- **实时状态展示**：显示当前适配器、网络名称、IP、掩码、网关和获取方式。
- **可配置轮询间隔**：默认 5 秒，可在界面中修改。
- **配置可读可版本化**：规则保存在本地 JSON 文件中。

## 运行要求

- Windows 10 / Windows 11
- Python 3.7 及以上（已在 Python 3.9.8 实测）
- 管理员权限（修改网络配置必须提权）
- 系统自带 WLAN AutoConfig 服务（无线网络检测需要）

## 快速开始

1. 克隆或下载本项目到本地目录。
2. 双击 **`启动.vbs`**。
   - 不会弹出终端黑框；
   - 程序会通过 UAC 请求管理员权限，请选择“是”。
3. 在界面中点击 **“添加规则”**：
   - 网络类型选择 `Wi-Fi 无线`；
   - 网络名称填写 SSID（例如 `MyHomeWiFi`）；
   - 获取方式选择 `固定 IP`，填写 IP、子网掩码、默认网关；
   - DNS 可留空（沿用当前网络的 DNS），多个地址用英文逗号分隔。
4. 点击 **“立即应用”** 验证配置，确认网络正常。
5. 点击 **“启动后台监控”**，之后连接不同网络会自动切换。
6. 如需开机自动运行，勾选 **“开机自动启动”**。

> 配置修改后会被保存到 `config.json`，后台监控会在下一轮询周期自动采用新规则，无需重启。

## 规则说明

| 字段 | 说明 |
| --- | --- |
| `type` | `wifi`（Wi-Fi SSID）、`ethernet`（有线适配器）、`any`（任意网络） |
| `match` | Wi-Fi 填 SSID；有线填适配器名称（如“以太网”）；`any` 可匹配两者 |
| `mode` | `static`（固定 IP）或 `dhcp`（自动获取） |
| `ip` / `mask` | 固定 IP 与子网掩码，`mode=static` 时必填 |
| `gateway` | 默认网关，可留空 |
| `dns` | DNS 服务器数组，可留空（沿用切换前当前网络的 DNS） |

匹配优先级：

1. 先匹配与当前网络类型一致的规则（Wi-Fi 规则匹配 SSID，有线规则匹配适配器名称）；
2. 再匹配 `type=any` 的通配规则；
3. 同类型规则按列表从上到下顺序匹配，命中第一条即生效。

未匹配到规则时：

- 如果 `revertToDhcpWhenUnmatched` 为 `true`，且该网络类型下至少配置过一条规则，则自动恢复 DHCP；
- 如果该类型从未配置过规则，则不会改动其网络设置，避免误改其他适配器。

## 配置文件示例

默认配置文件为 `config.json`（首次运行会自动生成，已被 `.gitignore` 忽略，避免提交个人网络信息）：

```json
{
  "pollSeconds": 5,
  "revertToDhcpWhenUnmatched": true,
  "profiles": [
    {
      "type": "wifi",
      "match": "MyHomeWiFi",
      "mode": "static",
      "ip": "192.168.1.100",
      "mask": "255.255.255.0",
      "gateway": "192.168.1.1",
      "dns": ["223.5.5.5", "223.6.6.6"]
    },
    {
      "type": "wifi",
      "match": "OfficeWiFi",
      "mode": "dhcp"
    },
    {
      "type": "ethernet",
      "match": "以太网",
      "mode": "static",
      "ip": "10.0.0.50",
      "mask": "255.255.255.0",
      "gateway": "10.0.0.1",
      "dns": []
    }
  ]
}
```

## 工作原理

```text
┌──────────────────────────────┐
│  tkinter 图形界面             │
│  · 规则管理 / 状态展示        │
│  · 立即应用 / 监控启停        │
│  · 开机自启（计划任务）       │
└──────────────┬───────────────┘
               │ 读写 config.json
┌──────────────▼───────────────┐
│  后台监控进程 (pythonw)       │
│  · 单实例互斥锁              │
│  · 定时轮询当前网络           │
│  · 匹配规则并应用             │
└──────────────┬───────────────┘
               │
┌──────────────▼───────────────┐
│  Windows 网络配置             │
│  · IP Helper API 读取状态     │
│  · netsh wlan 读取 SSID       │
│  · netsh 写入 IP/网关/DNS     │
└──────────────────────────────┘
```

- **状态读取**：使用 `GetAdaptersAddresses` / `GetAdaptersInfo`（ctypes）获取适配器、IPv4、掩码、网关、DNS 与 DHCP 状态，避免每次轮询启动 PowerShell。
- **SSID 读取**：解析 `netsh wlan show interfaces` 输出。
- **配置写入**：调用 `netsh interface ipv4 set address` / `set dnsservers`，切换后执行 `ipconfig /flushdns`。
- **无窗口运行**：所有子进程使用 `CREATE_NO_WINDOW`，配合 `pythonw.exe` 与 `启动.vbs`，不会出现终端黑框。
- **开机自启**：生成任务 XML，通过 `schtasks` 注册 `WifiIpAutoSwitch` 计划任务，运行级别为 `HighestAvailable`，登录时触发。

## 命令行

```powershell
# 只读诊断：输出当前网络、当前 IP 配置和将要执行的操作，不修改任何设置
python wifi_ip_switch.py --diagnose

# 后台监控（通常由界面或计划任务启动，不建议手动运行）
python wifi_ip_switch.py --watch

# 打开图形界面
python wifi_ip_switch.py
```

`--diagnose` 不需要管理员权限，适合排查“为什么没有切换”。

## 开机自动启动

- 在界面勾选 **“开机自动启动”** 即可安装计划任务。
- 任务名称：`WifiIpAutoSwitch`
- 运行程序：当前 Python 的 `pythonw.exe`
- 参数：`"<项目路径>/wifi_ip_switch.py" --watch`
- 运行级别：最高权限（登录时静默运行，不弹出 UAC）
- 取消勾选即可删除计划任务。

> 如果之后移动了项目目录或重装了 Python，需要重新勾选一次以更新任务路径。

## 日志

- 后台监控日志：`logs/watcher.log`
- 日志超过 1 MB 会自动轮换为 `watcher.log.1`
- 界面中有“打开日志目录”按钮

## 常见问题

**双击 `启动.vbs` 没有反应？**

- 确认已安装 Python 3，并且 `pythonw.exe` 在系统 PATH 中：
  ```powershell
  python --version
  where.exe pythonw
  ```
- 也可以直接用命令行运行 `python wifi_ip_switch.py` 查看报错。

**为什么修改网络设置需要管理员权限？**

`netsh interface ipv4` 修改 IP、网关和 DNS 属于系统级操作，必须提权。程序使用 `ShellExecuteW` 的 `runas` 自动请求权限。

**设置固定 IP 后短暂断网正常吗？**

正常。切换 IP 的瞬间网络会短暂中断，通常几秒内恢复。若切换到未知网络，可启用“未匹配网络时恢复 DHCP”自动回退。

**DNS 留空会怎样？**

固定 IP 模式下会沿用切换前当前网络的 DNS（通常是该网络 DHCP 下发的 DNS），避免 Windows 在设置静态 IP 后清空 DNS 导致无法上网。需要自定义 DNS 时直接在规则中填写。

**怎样确认规则是否生效？**

运行 `python wifi_ip_switch.py --diagnose`，或查看界面顶部“当前网络”表格和“运行日志”。

**如何彻底卸载？**

1. 在界面取消勾选“开机自动启动”；
2. 点击“停止后台监控”；
3. 删除本项目目录即可。程序不会写入注册表（计划任务除外）。

**支持 IPv6 吗？**

目前只处理 IPv4 的 IP、网关与 DNS。

## 项目结构

```text
wife-ip-auto-switch/
├── wifi_ip_switch.py   # 主程序：网络检测、规则匹配、应用、GUI、后台监控
├── 启动.vbs            # 无窗口启动器（自动请求管理员权限）
├── config.json         # 本地规则配置（首次运行自动生成，不提交到 Git）
├── logs/               # 运行日志（自动生成，不提交到 Git）
├── LICENSE             # MIT 许可证
└── README.md
```

## 安全与隐私

- 程序不访问互联网，不上传任何数据；仅在本机读取网络配置并修改本机网络设置。
- 所有规则保存在本地 `config.json`，请勿将含个人 SSID / 内网 IP 的配置提交到公开仓库。
- 程序需要管理员权限运行，建议在使用前阅读源码。

## 开发与贡献

欢迎提交 Issue 或 Pull Request。

开发环境：

```powershell
git clone <repo-url>
cd wife-ip-auto-switch
python wifi_ip_switch.py --diagnose   # 先做只读诊断
python wifi_ip_switch.py              # 打开界面
```

提交前请确保：

- `python -m py_compile wifi_ip_switch.py` 通过；
- 不提交 `config.json`、`logs/`、`__pycache__/`；
- 涉及网络切换的改动请在测试网络验证。

## English

A small Windows utility that automatically applies a static IPv4 address, gateway and DNS servers based on the connected Wi-Fi SSID or wired adapter name. Built with Python's standard library (tkinter + ctypes + netsh), it runs a lightweight background watcher and can register a scheduled task for silent startup. No third-party dependencies, no telemetry.

## License

[MIT](LICENSE) © 2026 wife-ip-auto-switch contributors