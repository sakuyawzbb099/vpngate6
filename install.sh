#!/bin/bash
set -e

# ============================================================
# AimiliVPN 一键源码部署与管理脚本
# ============================================================

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
NC='\033[0m'

# 检查是否为 root 用户
if [ "$(id -u)" != "0" ]; then
    echo -e "${RED}错误: 必须以 root 权限运行此脚本。请使用: sudo bash $0${NC}"
    exit 1
fi

# 默认配置
INSTALL_DIR="/opt/aimilivpn"
SERVICE_NAME="aimilivpn"
DEFAULT_MANAGE_PORT="8787"
DEFAULT_PROXY_PORT="7928"
REPO_OWNER="${1:-baoweise-bot}"
REPO_NAME="${2:-aimili-vpngate}"
BRANCH="main"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}.git"

# 检测发行版
detect_distro() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
        VERSION=$VERSION_ID
    elif type lsb_release >/dev/null 2>&1; then
        OS=$(lsb_release -si | tr '[:upper:]' '[:lower:]')
    else
        OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    fi

    case "$OS" in
        ubuntu|debian|linuxmint|pop|elementary|kali|parrot|raspbian|deepin|uos)
            PKG_MANAGER="apt-get"
            ;;
        alpine)
            PKG_MANAGER="apk"
            OS="alpine"
            ;;
        centos|rhel|rocky|almalinux|fedora|oraclelinux|amazonlinux|openeuler|anolis)
            if command -v dnf &>/dev/null; then
                PKG_MANAGER="dnf"
            else
                PKG_MANAGER="yum"
            fi
            if [ "$OS" = "centos" ] && [ "${VERSION%%.*}" -ge 8 ]; then
                PKG_MANAGER="dnf"
            fi
            OS="centos"
            ;;
        arch|manjaro|endeavouros|arcolinux|garuda)
            PKG_MANAGER="pacman"
            OS="arch"
            ;;
        opensuse*|suse*)
            PKG_MANAGER="zypper"
            OS="opensuse"
            ;;
        *)
            echo -e "${RED}不支持的操作系统: $OS${NC}"
            exit 1
            ;;
    esac
    echo -e "${GREEN}检测到系统: $OS (包管理器: $PKG_MANAGER)${NC}"
}

# 安装系统依赖
install_dependencies() {
    echo -e "${CYAN}[步骤 1/4] 安装系统基础依赖...${NC}"
    
    case "$PKG_MANAGER" in
        apt-get)
            apt-get update -qq
            apt-get install -y -qq openvpn curl git ca-certificates iptables iproute2 psmisc python3 2>/dev/null
            ;;
        apk)
            apk update -q
            apk add openvpn curl git ca-certificates iptables iproute2 psmisc python3 bash 2>/dev/null
            ;;
        dnf|yum)
            if [ "$PKG_MANAGER" = "dnf" ]; then
                dnf install -y epel-release 2>/dev/null || true
                dnf install -y openvpn curl git ca-certificates iptables iproute psmisc python3 2>/dev/null
            else
                yum install -y epel-release 2>/dev/null || true
                yum install -y openvpn curl git ca-certificates iptables iproute psmisc python3 2>/dev/null
            fi
            ;;
        pacman)
            pacman -S --noconfirm openvpn curl git ca-certificates iptables iproute2 psmisc python 2>/dev/null
            ;;
        zypper)
            zypper install -y openvpn curl git ca-certificates iptables iproute2 psmisc python3 2>/dev/null
            ;;
    esac
    
    echo -e "${GREEN}✓ 依赖安装完成${NC}"
}

# 从 GitHub 获取源码
deploy_source() {
    echo -e "${CYAN}[步骤 2/4] 从 GitHub 部署源代码...${NC}"
    
    if [ -f "${INSTALL_DIR}/.local_dev" ]; then
        echo -e "${YELLOW}本地开发模式已启用，跳过 Git 更新。${NC}"
        return 0
    fi
    
    if [ -d "$INSTALL_DIR" ]; then
        echo -e "${YELLOW}检测到已有安装目录，正在更新...${NC}"
        cd "$INSTALL_DIR"
        git fetch --all 2>/dev/null || true
        git reset --hard origin/$BRANCH 2>/dev/null || true
    else
        echo -e "${BLUE}全新安装，克隆仓库到 $INSTALL_DIR ...${NC}"
        git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
    fi
    
    chmod -R 755 "$INSTALL_DIR"
    echo -e "${GREEN}✓ 源码部署完成${NC}"
}

# 配置系统服务
install_service() {
    echo -e "${CYAN}[步骤 3/4] 配置系统服务...${NC}"
    
    if command -v systemctl &>/dev/null; then
        # systemd 系统
        cat > /lib/systemd/system/${SERVICE_NAME}.service << 'EOF'
[Unit]
Description=AimiliVPN OpenVPN Manager with HTTP/SOCKS5 Proxy
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/aimilivpn
ExecStart=/usr/bin/python3 vpngate_manager.py
Restart=always
RestartSec=5
EnvironmentFile=-/etc/default/aimilivpn

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable ${SERVICE_NAME} 2>/dev/null || true
        echo -e "${GREEN}✓ systemd 服务已安装${NC}"
    elif command -v rc-update &>/dev/null; then
        # OpenRC 系统 (Alpine)
        cat > /etc/init.d/${SERVICE_NAME} << 'INITEOF'
#!/sbin/openrc-run

name="AimiliVPN"
description="AimiliVPN OpenVPN Manager with HTTP/SOCKS5 Proxy"
command="/usr/bin/python3"
command_args="/opt/aimilivpn/vpngate_manager.py"
command_background=true
pidfile="/run/${RC_SVCNAME}.pid"

depend() {
    need net
    after firewall
}

start_pre() {
    checkpath -d -m 0755 -o root:root /opt/aimilivpn/vpngate_data
}
INITEOF
        chmod +x /etc/init.d/${SERVICE_NAME}
        rc-update add ${SERVICE_NAME} default 2>/dev/null || true
        echo -e "${GREEN}✓ OpenRC 服务已安装${NC}"
    else
        echo -e "${YELLOW}未检测到 systemd 或 OpenRC，跳过服务安装。${NC}"
    fi
}

# 创建 ml 命令
install_ml_command() {
    echo -e "${CYAN}[步骤 4/4] 创建全局命令快捷接口...${NC}"
    
    cat > /usr/bin/ml << 'MLSCRIPT'
#!/bin/bash
# 转发到实际的管理脚本
exec /opt/aimilivpn/cli.sh "$@"
MLSCRIPT
    chmod +x /usr/bin/ml
    
    echo -e "${GREEN}✓ 快捷命令 'ml' 已安装${NC}"
}

# 配置网络优化
configure_network() {
    echo -e "${CYAN}配置网络优化...${NC}"
    
    # 创建 sysctl 配置
    cat > /etc/sysctl.d/99-${SERVICE_NAME}.conf << EOF
# AimiliVPN network optimization
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
EOF
    
    # 动态应用
    sysctl -w net.ipv4.conf.all.rp_filter=2 2>/dev/null || true
    sysctl -w net.ipv4.conf.default.rp_filter=2 2>/dev/null || true
    
    # 应用到所有接口
    for iface in /proc/sys/net/ipv4/conf/*/rp_filter; do
        echo 2 > "$iface" 2>/dev/null || true
    done
    
    echo -e "${GREEN}✓ 网络优化配置完成${NC}"
}

# 显示完成信息
show_complete_info() {
    local pub_ip=$(curl -s --connect-timeout 5 api64.ipify.org 2>/dev/null || curl -s --connect-timeout 5 api.ipify.org 2>/dev/null || echo "获取失败")
    
    echo ""
    echo -e "${GREEN}==========================================================${NC}"
    echo -e "${GREEN}             AimiliVPN 源码一键部署已完成！${NC}"
    echo -e "${GREEN}==========================================================${NC}"
    echo -e "  管理端口:   ${YELLOW}${DEFAULT_MANAGE_PORT}${NC}"
    echo -e "  代理端口:   ${YELLOW}${DEFAULT_PROXY_PORT}${NC}"
    echo -e "  HTTP/SOCKS5: ${YELLOW}http://127.0.0.1:${DEFAULT_PROXY_PORT}/${NC}"
    echo " --------------------------------------------------------"
    echo -e "  快速状态:   ${CYAN}ml status${NC}"
    echo -e "  查看日志:   ${CYAN}ml logs${NC}"
    echo -e "  重启服务:   ${CYAN}ml restart${NC}"
    echo -e "  停止服务:   ${CYAN}ml stop${NC}"
    echo -e "${GREEN}==========================================================${NC}"
}

# ===== 主执行流程 =====
echo -e "${PURPLE}==========================================================${NC}"
echo -e "${PURPLE}       AimiliVPN 一键源码部署脚本 v2.0${NC}"
echo -e "${PURPLE}==========================================================${NC}"

detect_distro
install_dependencies
deploy_source
install_service
install_ml_command
configure_network
show_complete_info

echo ""
echo -e "${GREEN}部署完成！正在启动服务...${NC}"

# 启动服务
if command -v systemctl &>/dev/null; then
    systemctl start ${SERVICE_NAME} 2>/dev/null || true
elif command -v rc-service &>/dev/null; then
    rc-service ${SERVICE_NAME} start 2>/dev/null || true
fi

echo -e "${GREEN}服务已启动！使用 'ml status' 查看运行状态。${NC}"