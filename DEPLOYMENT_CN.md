# PairProxy 部署与运维教程

## 1. 架构说明

第三方只连接公网 Linux：

```text
第三方程序
  -> YOUR_LINUX_PUBLIC_IP:27499
  -> Linux pairproxy broker
  -> TLS 控制通道 27500
  -> Windows agent
  -> Windows 本地网络访问目标
```

端口用途：

```text
27499/tcp  第三方 SOCKS5/HTTP 代理端口
27500/tcp  Windows agent 回连 Linux 的 TLS 控制端口
```

Windows agent 不监听 `127.0.0.1:1080/8080`。安装及停止脚本会清理旧版 PairProxy 的系统代理设置。

## 2. Linux 首次部署

把整个 `shuangxiang-daili` 目录上传到 Linux，例如 `/root/shuangxiang-daili`。

```bash
cd /root/shuangxiang-daili
chmod +x linux-pairproxy.sh
sudo ./linux-pairproxy.sh install
```

安装输出里保存这些值：

```text
Public proxy port: 27499
Agent control port: 27500
Proxy username: ...
Proxy password: ...
Agent token: ...
Agent TLS fingerprint: ...
```

防火墙/云安全组：

```bash
sudo ufw allow 27499/tcp
sudo ufw allow 27500/tcp
```

更安全的做法是 `27500/tcp` 只允许 Windows 公网 IP：

```bash
sudo ufw allow from YOUR_WINDOWS_PUBLIC_IP to any port 27500 proto tcp
```

## 3. Linux 常用命令

启动：

```bash
sudo systemctl start pairproxy
```

停止：

```bash
sudo systemctl stop pairproxy
```

重启：

```bash
sudo systemctl restart pairproxy
```

状态：

```bash
sudo systemctl status pairproxy --no-pager
sudo systemctl is-active pairproxy
```

实时日志：

```bash
sudo journalctl -u pairproxy -f
```

最近日志：

```bash
sudo journalctl -u pairproxy -n 100 --no-pager
```

监听端口：

```bash
sudo ss -lntp | grep -E '27499|27500'
```

查看配置：

```bash
sudo cat /etc/pairproxy.env
```

只看第三方代理账号：

```bash
sudo grep -E '^PAIRPROXY_PROXY_(USER|PASS)=' /etc/pairproxy.env
```

## 4. Linux 升级同步

本地改了 `reverse_pair_proxy.py` 或脚本后，把整个目录重新传到 Linux，然后执行：

```bash
cd /root/shuangxiang-daili
chmod +x linux-pairproxy.sh
sudo ./linux-pairproxy.sh install
```

`install` 会：

- 保留 `/etc/pairproxy.env` 里已有 token、代理账号和端口。
- 覆盖 `/opt/pairproxy/reverse_pair_proxy.py`。
- 重新生成 systemd unit。
- 自动重启并启用开机自启。

如果只想重启：

```bash
sudo systemctl restart pairproxy
```

## 5. Windows 安装 agent

复制 `win-config.example.bat` 为 `win-config.bat`，将 Linux 安装输出填入本地配置文件：

```bat
set "SERVER_HOST=YOUR_LINUX_PUBLIC_IP"
set "AGENT_PORT=27500"
set "AGENT_TOKEN=Linux 输出的 Agent token"
set "TLS_FINGERPRINT=Linux 输出的 Agent TLS fingerprint"
set "POOL_SIZE=256"
```

运行：

```bat
win-install.bat
```

它会：

- 复制 `reverse_pair_proxy.py` 到 `C:\ProgramData\PairProxy`
- 创建计划任务 `PairProxyAgent`
- 启动 `POOL_SIZE` 条 agent 通道
- 断线后自动重连
- 当前用户登录后自动恢复
- 计划任务启动隐藏 PowerShell runner，再隐藏启动 `pythonw.exe`，不弹出 CMD 窗口
- 清理旧版本地代理任务 `PairProxyClient`
- 关闭 Windows 系统代理，避免本机误走本地代理

## 6. Windows 常用命令

查看计划任务：

```powershell
schtasks /Query /TN PairProxyAgent /FO LIST
```

查看 agent 日志：

```powershell
Get-Content C:\ProgramData\PairProxy\pairproxy-agent.log -Tail 80
```

新版会把 Windows agent 的回连失败以 `WARNING` 写入日志。重点看：

```text
timed out                         网络/防火墙/27500 不通
TLS fingerprint mismatch         Linux 证书指纹和 BAT 不一致
auth failed                       Agent token 不一致
no usable Windows agent ...       Linux 池为空或 256 条通道都在使用
```

查看 agent 到 Linux 的通道数量：

```powershell
(Get-NetTCPConnection -RemoteAddress YOUR_LINUX_PUBLIC_IP -RemotePort 27500 -State Established).Count
```

测试 Linux 控制端口：

```powershell
Test-NetConnection YOUR_LINUX_PUBLIC_IP -Port 27500
```

停止 Windows agent：

```bat
win-stop.bat
```

也可以直接双击目录里的：

```bat
stop.bat
```

重新安装/重启 Windows agent：

```bat
win-install.bat
```

## 7. 第三方填写方式

SOCKS5：

```text
类型：SOCKS5
地址：YOUR_LINUX_PUBLIC_IP
端口：27499
用户名：Proxy username
密码：Proxy password
```

HTTP：

```text
类型：HTTP
地址：YOUR_LINUX_PUBLIC_IP
端口：27499
用户名：Proxy username
密码：Proxy password
```

命令行测试 SOCKS5：

```bash
curl --socks5-hostname YOUR_LINUX_PUBLIC_IP:27499 \
  --proxy-user 'USERNAME:PASSWORD' \
  https://ifconfig.me
```

命令行测试 HTTP/HTTPS 代理：

```bash
curl -x 'http://USERNAME:PASSWORD@YOUR_LINUX_PUBLIC_IP:27499' https://ifconfig.me
```

## 8. 超时与清理参数

Linux 配置文件：

```bash
sudo nano /etc/pairproxy.env
```

核心参数：

```ini
PAIRPROXY_AGENT_WAIT_TIMEOUT=15
PAIRPROXY_AGENT_HANDSHAKE_TIMEOUT=10
PAIRPROXY_AGENT_PROBE_INTERVAL=30
PAIRPROXY_AGENT_PROBE_TIMEOUT=3
PAIRPROXY_AGENT_PROBE_PER_CYCLE=8
PAIRPROXY_AGENT_MAX_IDLE_LIFETIME=1800
PAIRPROXY_AGENT_RECYCLE_PER_CYCLE=8
PAIRPROXY_PUBLIC_HANDSHAKE_TIMEOUT=15
PAIRPROXY_CONNECT_TIMEOUT=10
PAIRPROXY_RELAY_IDLE_TIMEOUT=300
PAIRPROXY_KEEPALIVE_IDLE=60
PAIRPROXY_KEEPALIVE_INTERVAL=15
PAIRPROXY_KEEPALIVE_COUNT=4
PAIRPROXY_MAX_IDLE_AGENTS=2048
```

含义：

```text
AGENT_WAIT_TIMEOUT          第三方请求等待可用 Windows agent 的最长时间
AGENT_PROBE_INTERVAL        Linux 定期 ping 空闲 agent 的间隔
AGENT_PROBE_TIMEOUT         单次 ping agent 的超时
AGENT_PROBE_PER_CYCLE       每轮最多探活多少条，避免探活超时拖空整个池
AGENT_MAX_IDLE_LIFETIME     空闲 agent 通道最长寿命，超时后强制回收重连
AGENT_RECYCLE_PER_CYCLE     每轮最多回收多少条到期通道，避免 256 条同时断线
PUBLIC_HANDSHAKE_TIMEOUT    第三方 SOCKS5/HTTP 握手超时
CONNECT_TIMEOUT             Windows 连接目标站点的超时
RELAY_IDLE_TIMEOUT          已建立连接无数据活动多久后关闭
KEEPALIVE_*                 TCP keepalive 探测半死连接
MAX_IDLE_AGENTS             Linux broker 最多保留多少条空闲 agent
```

修改后重启：

```bash
sudo systemctl restart pairproxy
```

Windows 侧并发通道数在本地 `win-config.bat`：

```bat
set "POOL_SIZE=256"
set "RECONNECT_JITTER=5"
```

改完后重新运行：

```bat
win-install.bat
```

## 9. 故障排查

### no usable Windows agent available

含义：Linux 没拿到可用 Windows agent。

如果日志里同时出现 `recycled=256` 或短时间内大量 agent 断开，说明旧版本一次性回收了整池通道。新版每轮最多回收 8 条，避免形成整池 outage。

检查：

```bash
sudo journalctl -u pairproxy -n 100 --no-pager
```

Windows 检查：

```powershell
schtasks /Query /TN PairProxyAgent /FO LIST
(Get-NetTCPConnection -RemoteAddress YOUR_LINUX_PUBLIC_IP -RemotePort 27500 -State Established).Count
Get-Content C:\ProgramData\PairProxy\pairproxy-agent.log -Tail 80
```

处理：

```bash
sudo systemctl restart pairproxy
```

Windows：

```bat
win-install.bat
```

### SOCKS5 认证失败

确认第三方填写的是：

```text
Proxy username
Proxy password
```

不是：

```text
Agent token
TLS fingerprint
```

Linux 查看账号：

```bash
sudo grep -E '^PAIRPROXY_PROXY_(USER|PASS)=' /etc/pairproxy.env
```

### 27500 不通

Windows 测：

```powershell
Test-NetConnection YOUR_LINUX_PUBLIC_IP -Port 27500
```

Linux 放行：

```bash
sudo ufw allow 27500/tcp
```

云服务器安全组也要放行 TCP `27500`。

### 27499 不通

Windows 或第三方测：

```powershell
Test-NetConnection YOUR_LINUX_PUBLIC_IP -Port 27499
```

Linux 放行：

```bash
sudo ufw allow 27499/tcp
```

### TLS fingerprint mismatch

Linux 证书变了，Windows 的本地 `win-config.bat` 里 `TLS_FINGERPRINT` 需要更新。

Linux 查看新指纹：

```bash
sudo ./linux-pairproxy.sh fingerprint
```

然后修改 Windows：

```bat
set "TLS_FINGERPRINT=新的指纹"
```

重新运行：

```bat
win-install.bat
```

### 长连接太多导致排队

现象：

```text
no usable Windows agent available
```

处理：

1. 增大 Windows `POOL_SIZE`，例如 `512`。
2. 降低 Linux `PAIRPROXY_RELAY_IDLE_TIMEOUT`，例如 `180`。
3. 重启 Linux broker 和 Windows agent。

Linux：

```bash
sudo systemctl restart pairproxy
```

Windows：

```bat
win-install.bat
```

## 10. 当前推荐默认值

```text
Windows POOL_SIZE=256
Linux RELAY_IDLE_TIMEOUT=300
Linux AGENT_PROBE_INTERVAL=30
Linux AGENT_PROBE_TIMEOUT=3
Linux AGENT_MAX_IDLE_LIFETIME=1800
```

如果第三方主要是短连接，可以把 `RELAY_IDLE_TIMEOUT` 调到 `120` 到 `180`。

如果第三方主要是 IM、微信、长轮询一类长连接，保持 `300` 或调到 `600`。
