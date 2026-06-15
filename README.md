# AimiliVPN 6-Channel

> 基于 VPNGate 的 6 通道独立出口 VPN 网关  
> 每个通道拥有独立的 OpenVPN 隧道、虚拟网卡(tun)、代理端口和出站 IP

---

## 🚀 一键安装

```bash
bash <(curl -Ls https://raw.githubusercontent.com/sakuyawzbb099/vpngate6/main/install.sh)
```

安装后访问 `http://<VPS_IP>:8787/` 进入管理面板。  
默认账号密码从旧的 `ui_auth.json` 读取，可在 `/opt/aimilivpn/vpngate_data/ui_auth.json` 中修改。

---

## 💡 快速使用

### 1. 登录管理面板
浏览器打开 `http://<VPS_IP>:8787/`，输入账号密码登录。

### 2. 获取节点
点击 **获取节点** 按钮拉取 VPNGate 可用节点。

### 3. 连接通道
每个通道(CH0~CH5)可独立选择国家 + IP类型(住宅/机房/移动)：
- 选好国家 + IP类型 → 点击 **连接**
- 或从下方节点表直接点 **切换** → 选目标通道
- 每个通道有独立出口 IP

### 4. 使用代理

| 通道 | tun | 代理端口 | SOCKS5 |
|------|-----|---------|--------|
| CH0 | tun0 | 7928 | socks5://127.0.0.1:7928 |
| CH1 | tun1 | 7929 | socks5://127.0.0.1:7929 |
| CH2 | tun2 | 7930 | socks5://127.0.0.1:7930 |
| CH3 | tun3 | 7931 | socks5://127.0.0.1:7931 |
| CH4 | tun4 | 7932 | socks5://127.0.0.1:7932 |
| CH5 | tun5 | 7933 | socks5://127.0.0.1:7933 |

```bash
# 示例：通过 CH0 (日本) 访问
export http_proxy=http://127.0.0.1:7928
export https_proxy=http://127.0.0.1:7928
curl ifconfig.me  # 显示日本 IP
```

---

## 🎛️ 管理命令

```bash
ml status   # 查看6通道状态
ml restart  # 重启服务
ml logs     # 查看实时日志
ml stop     # 停止服务
ml start    # 启动服务
```

---

## ⚙️ 核心功能

### 6通道独立管理
- 每个通道独立 OpenVPN 连接
- 独立虚拟网卡 (tun0~tun5)
- 独立 HTTP/SOCKS5 代理端口 (7928~7933)
- 独立策略路由表，互不冲突

### 节点管理与筛选
- **全部节点 / 可用节点 / 失效节点** 筛选
- **国家筛选**：日本、韩国、美国、泰国等
- **IP类型筛选**：住宅IP / 机房IP / 移动网络
- 每个节点显示：物理位置、运营主体(ISP)、ASN、IP类型

### IP 信息富集
- 物理位置（国家、地区、城市）
- 运营主体 / ISP（如 KDDI、SoftEther、LG Uplus）
- IP 类型（住宅、机房、移动）
- 数据来源：ip-api.com

### 安全登录
- 账号密码认证
- Cookie 会话管理（24小时有效期）

---

## ⚠️ 常见问题

### Web UI 无法访问
- 检查防火墙：`ufw allow 8787/tcp && ufw allow 7928/tcp`
- 云服务商安全组放行 8787、7928~7933 端口

### 节点列表为空
- 检查 DNS：`echo "nameserver 8.8.8.8" > /etc/resolv.conf`
- 或手动点击 **获取节点** 按钮

### TUN/TAP 设备错误
- LXC/OpenVZ VPS 需要在控制面板启用 TUN/TAP

---

## 📦 文件结构

```
/opt/aimilivpn/
├── vpngate6_multi.py      # 6通道管理器
├── proxy_server_multi.py  # 多通道代理
├── vpn_utils.py           # IP信息富集
├── install.sh             # 部署脚本
├── vpngate_data/
│   ├── login.html         # 登录页面
│   ├── ui_auth.json       # 账号密码配置
│   ├── ip_cache.json      # IP 信息缓存
│   └── nodes.json         # 节点缓存
```

---

## 📢 社区

- Telegram: [@arestemple](https://t.me/arestemple)

---

*基于 baoweise-bot/aimili-vpngate 改造，增加6通道多路出站支持*
