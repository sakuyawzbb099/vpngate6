#!/bin/bash
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}必须以 root 权限运行${NC}"; exit 1
fi

INSTALL_DIR="/opt/aimilivpn"
SERVICE_NAME="aimilivpn"
REPO_OWNER="${1:-sakuyawzbb099}"
REPO_NAME="${2:-vpngate6}"
BRANCH="main"

detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release; OS=$ID
    else
        OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    fi
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
    *)       echo "用法: ml {start|stop|restart|status|logs}" ;;
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
systemctl is-active ${SERVICE_NAME} &>/dev/null && echo -e "${GREEN}服务运行中${NC}" || echo -e "${YELLOW}检查服务状态: systemctl status aimilivpn${NC}"
echo ""
echo -e "  Web UI:    ${CYAN}http://<VPS_IP>:8787/${NC}"
echo -e "  代理端口:  ${CYAN}7928~7933${NC} (tun0~tun5)"
echo -e "  状态:      ${CYAN}ml status${NC}"
echo -e "  日志:      ${CYAN}ml logs${NC}"
