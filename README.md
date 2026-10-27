# 双向代理

Win-Linux Reverse PairProxy：Linux 公网入口 + Windows 出口的 SOCKS5/HTTP 反向代理。

这套架构不是让 Windows 本地使用代理，而是让第三方直接使用公网 Linux 的代理地址。

完整部署、启动停止、日志、超时清理和故障排查看：

```text
DEPLOYMENT_CN.md
```

第三方填写：

```text
代理地址：YOUR_LINUX_PUBLIC_IP
代理端口：27499
代理类型：SOCKS5 或 HTTP
用户名/密码：Linux 安装脚本输出的代理账号
```

实际链路：

```text
第三方程序
  -> YOUR_LINUX_PUBLIC_IP:27499
  -> Linux broker
  -> 加密反向通道
  -> Windows agent
  -> Windows 发起目标连接
  -> 目标站点
```

Linux 是公网入口，Windows 是实际拨号出口。Windows agent 不监听本地 `1080/8080`。安装及停止脚本会清理旧版 PairProxy 的系统代理设置。

## 端口

- `27499/tcp`：第三方使用的公网 SOCKS5/HTTP 代理端口。
- `27500/tcp`：Windows agent 的 TLS 控制端口，不给第三方填写。

## Linux 部署

把整个目录传到 Linux，执行：

```bash
cd shuangxiang-daili
chmod +x linux-pairproxy.sh
sudo ./linux-pairproxy.sh install
```

脚本会自动：

- 创建 Linux broker systemd 服务。
- 监听公网代理端口 `27499`。
- 监听 Windows agent 控制端口 `27500`。
- 生成 agent token。
- 生成第三方代理用户名和密码。
- 生成 agent TLS 证书。
- 设置进程崩溃自动恢复和重启自动启动。

安装完成会输出：

```text
Public proxy port: 27499
Agent control port: 27500
Proxy username: ...
Proxy password: ...
Agent token: ...
Agent TLS fingerprint: ...
```

防火墙至少放行：

```bash
sudo ufw allow 27499/tcp
sudo ufw allow 27500/tcp
```

更安全的做法是把 `27500/tcp` 只允许 Windows 公网出口 IP：

```bash
sudo ufw allow from YOUR_WINDOWS_PUBLIC_IP to any port 27500 proto tcp
```

查看服务：

```bash
sudo ./linux-pairproxy.sh status
journalctl -u pairproxy -f
```

停止 Linux 公网代理：

```bash
sudo ./linux-pairproxy.sh stop
```

## Windows 部署

复制 `win-config.example.bat` 为 `win-config.bat`，在本地配置文件中填写：

```bat
set "SERVER_HOST=YOUR_LINUX_PUBLIC_IP"
set "AGENT_PORT=27500"
set "AGENT_TOKEN=Linux输出的Agent token"
set "TLS_FINGERPRINT=Linux输出的Agent TLS fingerprint"
```

注意：`AGENT_TOKEN` 和 `TLS_FINGERPRINT` 必须使用这次 Linux 安装输出的值。如果重新生成了 Linux 证书或 token，Windows 也要同步修改。

然后在 Windows 执行：

```bat
win-install.bat
```

Windows 会：

- 创建 `PairProxyAgent` 计划任务。
- 启动多个反向 agent worker。
- 主动连接 `YOUR_LINUX_PUBLIC_IP:27500`，使用 TLS + agent token。
- 断线后等待 3 秒并附加 0 到 5 秒随机延迟重连。
- 登录后自动恢复。
- 由隐藏的计划任务启动 PowerShell runner，再隐藏启动 `pythonw.exe`，不显示 CMD 窗口。
- 不打开本地 HTTP/SOCKS 监听。
- 安装脚本会清理旧版 PairProxy 任务和系统代理设置。

停止 Windows agent：

```bat
win-stop.bat
```

也可以直接双击：

```bat
stop.bat
```

## 第三方填写方式

### SOCKS5

```text
类型：SOCKS5
地址：YOUR_LINUX_PUBLIC_IP
端口：27499
用户名：Linux 输出的 Proxy username
密码：Linux 输出的 Proxy password
```

### HTTP

```text
类型：HTTP
地址：YOUR_LINUX_PUBLIC_IP
端口：27499
用户名：Linux 输出的 Proxy username
密码：Linux 输出的 Proxy password
```

命令行测试：

```bash
curl -x http://USERNAME:PASSWORD@YOUR_LINUX_PUBLIC_IP:27499 http://ifconfig.me
curl --proxy socks5h://USERNAME:PASSWORD@YOUR_LINUX_PUBLIC_IP:27499 http://ifconfig.me
```

## 关键逻辑

1. 第三方连接 Linux `27499`。
2. Linux 识别 SOCKS5 或 HTTP 协议，并校验第三方代理账号。
3. Linux 从空闲 Windows agent 通道池中取一条通道。
4. Linux 将目标域名和端口发给 Windows agent。
5. Windows agent 从本地网络连接目标。
6. Linux 与 Windows 之间只转发字节流。
7. 连接结束后该 agent worker 自动重新回到待命池。

## 限制

- 当前是 TCP 代理，不支持 SOCKS5 UDP ASSOCIATE。
- 一个 Windows agent pool 默认 256 条并发通道。
- Linux 每轮最多回收 8 条到期 agent 通道，避免全部 worker 同时重连造成短暂中断。
- Linux 每轮最多探活 8 条通道，Windows 重连带 5 秒随机抖动，避免探活失败时重连风暴。
- 如果 Windows 全部离线，第三方会收到 `502` 或 SOCKS5 连接失败。
- `27500` 是 agent 控制面，必须用 token + TLS，不要把它当代理端口填给第三方。
- 必须保管好 Linux 输出的代理密码和 agent token；泄露后应重新生成并重新部署。

## 运行环境与本地检查

Python 3.9 及以上，仅使用标准库。Linux 安装还需要 Bash、OpenSSL 和 systemd；Windows 需要 PowerShell、任务计划程序及 Python 的 `pythonw.exe`。

```powershell
python -m py_compile reverse_pair_proxy.py
powershell -NoProfile -ExecutionPolicy Bypass -File .\smoke-test.ps1
```

烟测只连接本机回环地址，检查 HTTP 和 SOCKS5 转发；完成后会停止测试进程。

## 本地配置与版本管理

`win-config.bat`、`.env`、日志、证书、私钥和运行产物已通过 `.gitignore` 排除。公开文件只含配置占位符；Linux 安装时为每次新部署生成随机凭据。更新代码时保留本地 `win-config.bat`，然后重新运行 `win-install.bat`。
