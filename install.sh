#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

if [ "$(id -u)" != "0" ]; then echo -e "${RED}必须以 root 权限运行${NC}"; exit 1; fi

INSTALL_DIR="/opt/aimilivpn"
SERVICE_NAME="aimilivpn"
REPO_OWNER="${1:-sakuyawzbb099}"
REPO_NAME="${2:-vpngate6}"
BRANCH="main"

# === 卸载功能 ===
if [ "${1:-}" = "uninstall" ] || [ "${1:-}" = "卸载" ]; then
    echo -e "${YELLOW}正在卸载 AimiliVPN...${NC}"
    systemctl stop ${SERVICE_NAME} 2>/dev/null || true
    systemctl disable ${SERVICE_NAME} 2>/dev/null || true
    rm -f /lib/systemd/system/${SERVICE_NAME}.service
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    rm -f /usr/bin/ml
    rm -f /etc/sysctl.d/99-${SERVICE_NAME}.conf
    pkill -f "vpngate6_multi\|proxy_server_multi" 2>/dev/null || true
    echo -e "${GREEN}卸载完成！${NC}"
    exit 0
fi

detect_distro() {
    if [ -f /etc/os-release ]; then . /etc/os-release; OS=$ID; else OS=$(uname -s | tr '[:upper:]' '[:lower:]'); fi
    case "$OS" in
        ubuntu|debian|linuxmint|pop|kali|raspbian) PKG_MANAGER="apt-get" ;;
        alpine) PKG_MANAGER="apk"; OS="alpine" ;;
        centos|rhel|rocky|almalinux|fedora|oraclelinux)
            command -v dnf &>/dev/null && PKG_MANAGER="dnf" || PKG_MANAGER="yum"; OS="centos" ;;
        arch|manjaro) PKG_MANAGER="pacman"; OS="arch" ;;
        opensuse*|suse*) PKG_MANAGER="zypper"; OS="opensuse" ;;
        *) echo -e "${RED}不支持: $OS${NC}"; exit 1 ;;
    esac
    echo -e "${GREEN}系统: $OS${NC}"
}

install_deps() {
    echo -e "${CYAN}[1/4] 安装依赖...${NC}"
    case "$PKG_MANAGER" in
        apt-get) apt-get update -qq && apt-get install -y -qq openvpn curl git ca-certificates iptables iproute2 psmisc python3 2>/dev/null ;;
        apk) apk update -q && apk add openvpn curl git ca-certificates iptables iproute2 psmisc python3 bash 2>/dev/null ;;
        dnf|yum) $PKG_MANAGER install -y epel-release 2>/dev/null || true; $PKG_MANAGER install -y openvpn curl git ca-certificates iptables iproute psmisc python3 2>/dev/null ;;
        pacman) pacman -S --noconfirm openvpn curl git ca-certificates iptables iproute2 psmisc python 2>/dev/null ;;
        zypper) zypper install -y openvpn curl git ca-certificates iptables iproute2 psmisc python3 2>/dev/null ;;
    esac
    echo -e "${GREEN}  OK${NC}"
}

deploy_code() {
    echo -e "${CYAN}[2/4] 部署代码...${NC}"
    if [ -d "$INSTALL_DIR" ]; then
        cd "$INSTALL_DIR" && git fetch --all 2>/dev/null || true && git reset --hard origin/$BRANCH 2>/dev/null || true
    else
        git clone --depth 1 -b "$BRANCH" "https://github.com/${REPO_OWNER}/${REPO_NAME}.git" "$INSTALL_DIR"
    fi

    # Generate login page
    mkdir -p "$INSTALL_DIR/vpngate_data"
    if [ ! -f "$INSTALL_DIR/vpngate_data/login.html" ]; then
        cat > "$INSTALL_DIR/vpngate_data/login.html" << 'LOGINEOF'
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AimiliVPN 登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f0f13;color:#e0e0e0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#1a1a24;border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:40px;width:360px}
.logo{font-size:24px;font-weight:700;text-align:center;margin-bottom:8px;color:#f0f0f5}
.sub{text-align:center;color:#6b7280;font-size:13px;margin-bottom:28px}
.fg{margin-bottom:16px}
.fg label{display:block;font-size:12px;color:#9ca3af;margin-bottom:6px}
.fg input{width:100%;padding:10px 14px;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:10px;color:#e0e0e0;font-size:14px;outline:none}
.fg input:focus{border-color:#818cf8}
.btn{width:100%;padding:10px;background:#818cf8;border:none;border-radius:10px;color:#fff;font-size:14px;font-weight:500;cursor:pointer}
.btn:hover{background:#6d79e8}
.err{color:#ef4444;font-size:13px;text-align:center;margin-top:10px}
</style>
</head>
<body>
<div class="card">
<div class="logo">AimiliVPN</div>
<div class="sub">6通道 VPN 管理面板</div>
<form method="post" action="/api/login">
<div class="fg"><label>管理账号</label><input type="text" name="username" required></div>
<div class="fg"><label>安全密码</label><input type="password" name="password" required></div>
<button class="btn" type="submit">登录</button>
<div class="err">账号或密码错误</div>
</form>
</div>
</body>
</html>
LOGINEOF
    fi

    # Generate default auth if missing
    if [ ! -f "$INSTALL_DIR/vpngate_data/ui_auth.json" ]; then
        echo '{"username":"admin","password": "***"}' > "$INSTALL_DIR/vpngate_data/ui_auth.json"
    fi

    chmod -R 755 "$INSTALL_DIR"
    echo -e "${GREEN}  OK${NC}"
}

install_service() {
    echo -e "${CYAN}[3/4] 配置服务...${NC}"
    cat > /lib/systemd/system/${SERVICE_NAME}.service << 'SERVICEEOF'
[Unit]
Description=AimiliVPN 6-Channel VPN Gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/aimilivpn
ExecStart=/usr/bin/python3 /opt/aimilivpn/vpngate6_multi.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICEEOF
    systemctl daemon-reload
    systemctl enable ${SERVICE_NAME} 2>/dev/null || true
    echo -e "${GREEN}  OK${NC}"
}

install_ml() {
    echo -e "${CYAN}[4/4] 创建 ml 命令...${NC}"
    cat > /usr/bin/ml << 'MLEOF'
#!/bin/bash
case "${1:-status}" in
    start)   systemctl start aimilivpn 2>/dev/null ;;
    stop)    systemctl stop aimilivpn 2>/dev/null ;;
    restart) systemctl restart aimilivpn 2>/dev/null ;;
    uninstall|卸载)
        echo -e "\033[0;33m正在卸载 AimiliVPN...\033[0m"
        systemctl stop aimilivpn 2>/dev/null || true
        systemctl disable aimilivpn 2>/dev/null || true
        rm -f /lib/systemd/system/aimilivpn.service
        systemctl daemon-reload
        rm -rf /opt/aimilivpn
        rm -f /usr/bin/ml
        rm -f /etc/sysctl.d/99-aimilivpn.conf
        pkill -f "vpngate6_multi\|proxy_server_multi" 2>/dev/null || true
        echo -e "\033[0;32m卸载完成！\033[0m" ;;
    status)
        echo "=== AimiliVPN 6-Channel ==="
        curl -s http://localhost:8787/api/status | python3 -c "
import sys,json
d=json.load(sys.stdin)
for c in d['channels']:
    s=c['state']; co=c.get('node_country') or '-'; ip=c.get('node_ip') or '-'
    print(f'CH{c[\"index\"]}: {s:15s} {co:20s} IP={ip:16s} :{c[\"proxy_port\"]}')
print(f'--- {d[\"node_count\"]} nodes ---')" 2>/dev/null || systemctl status aimilivpn --no-pager ;;
    logs)    journalctl -u aimilivpn --no-pager -n 50 -f ;;
    *)       echo "用法: ml {start|stop|restart|status|logs|uninstall}" ;;
esac
MLEOF
    chmod +x /usr/bin/ml
    echo -e "${GREEN}  OK${NC}"
}

configure_network() {
    echo -e "${CYAN}配置网络...${NC}"
    cat > /etc/sysctl.d/99-${SERVICE_NAME}.conf << 'SYSCTLEOF'
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
SYSCTLEOF
    for f in /proc/sys/net/ipv4/conf/*/rp_filter; do echo 2 > "$f" 2>/dev/null || true; done
    echo -e "${GREEN}  OK${NC}"
}

echo -e "${CYAN}============================"
echo "  AimiliVPN 6-Channel 部署"
echo "============================${NC}"
detect_distro
install_deps
deploy_code
install_service
install_ml
configure_network

echo ""
echo -e "${GREEN}部署完成！启动服务...${NC}"
systemctl start ${SERVICE_NAME} 2>/dev/null || true
sleep 3
systemctl is-active ${SERVICE_NAME} &>/dev/null && echo -e "${GREEN}服务运行中${NC}" || echo -e "${YELLOW}检查: systemctl status aimilivpn${NC}"
PUBLIC_IP=$(curl -s --connect-timeout 5 api64.ipify.org 2>/dev/null || curl -s --connect-timeout 5 api.ipify.org 2>/dev/null || echo "<VPS_IP>")
echo ""
echo -e "  Web UI:    ${CYAN}http://${PUBLIC_IP}:8787/${NC}"
echo -e "  默认账号:  ${CYAN}admin${NC}"
echo -e "  默认密码:  ${CYAN}admin${NC}"
echo -e "  代理端口:  ${CYAN}7928~7933${NC} (tun0~tun5)"
echo -e "  状态:      ${CYAN}ml status${NC}"
echo -e "  日志:      ${CYAN}ml logs${NC}"
echo -e "  卸载:      ${CYAN}ml uninstall${NC}"
