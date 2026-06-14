#!/usr/bin/env python3
"""
vpngate6_multi.py - Multi-Channel (6) VPN Gateway Manager
Each channel: independent OpenVPN + tunN + proxy port 7928+N
"""
from __future__ import annotations
import base64
import csv
import io
import json
import os
import random
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# === Constants ===
NUM_CHANNELS = 6
PROXY_BASE_PORT = 7928
UI_PORT = 8787
UI_HOST = "::"
LOCAL_PROXY_HOST = "127.0.0.1"
API_URL = "https://www.vpngate.net/api/iphone/"
FETCH_INTERVAL = int(os.environ.get("FETCH_INTERVAL", "600"))

ROOT_DIR = Path("/opt/aimilivpn")
DATA_DIR = ROOT_DIR / "vpngate_data"
CONFIG_DIR = DATA_DIR / "configs"
NODES_FILE = DATA_DIR / "nodes.json"
AUTH_FILE = DATA_DIR / "vpngate_auth.txt"
CHANNELS_FILE = DATA_DIR / "channels.json"

DATA_DIR.mkdir(exist_ok=True, parents=True)
CONFIG_DIR.mkdir(exist_ok=True, parents=True)
if not AUTH_FILE.exists():
    AUTH_FILE.write_text(f"vpn\nvpn\n")
    AUTH_FILE.chmod(0o600)

# === Channel State ===
class Channel:
    def __init__(self, index: int):
        self.index = index
        self.tun = f"tun{index}"
        self.proxy_port = PROXY_BASE_PORT + index
        self.force_country = ""
        self.state = "disconnected"
        self.node_id = ""
        self.node_name = ""
        self.node_ip = ""
        self.node_country = ""
        self.node_latency = 0
        self.process: subprocess.Popen[str] | None = None
        self.error = ""
        self.last_heartbeat = 0.0
        self.lock = threading.Lock()

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "tun": self.tun,
            "proxy_port": self.proxy_port,
            "force_country": self.force_country,
            "state": self.state,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "node_ip": self.node_ip,
            "node_country": self.node_country,
            "node_latency": self.node_latency,
            "error": self.error,
        }

channels: list[Channel] = [Channel(i) for i in range(NUM_CHANNELS)]
nodes_cache: list[dict[str, Any]] = []
nodes_cache_lock = threading.Lock()

# === Helpers ===
def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def write_json(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_openvpn_version() -> float:
    try:
        r = subprocess.run(["openvpn", "--version"], capture_output=True, text=True, timeout=2)
        m = re.search(r"(\d+\.\d+)", r.stdout)
        if m:
            return float(m.group(1))
    except:
        pass
    return 2.4

def stop_process(proc: subprocess.Popen[str] | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except:
            pass

def cleanup_policy_routing(table: int):
    try:
        subprocess.run(["ip", "rule", "del", "table", str(table)], capture_output=True, timeout=2)
    except:
        pass
    try:
        subprocess.run(["ip", "route", "flush", "table", str(table)], capture_output=True, timeout=2)
    except:
        pass

def setup_policy_routing(tun_dev: str, table: int):
    cleanup_policy_routing(table)
    try:
        subprocess.run(["ip", "route", "add", "default", "dev", tun_dev, "table", str(table)], check=True, timeout=2)
        subprocess.run(["ip", "rule", "add", "oif", tun_dev, "table", str(table)], check=True, timeout=2)
        for p in ["all", "default", tun_dev]:
            try:
                subprocess.run(["sysctl", "-w", f"net.ipv4.conf.{p}.rp_filter=2"], capture_output=True, timeout=2)
            except:
                pass
        log(f"[route {tun_dev}] table {table} OK")
    except Exception as e:
        log(f"[route {tun_dev}] Failed: {e}")

# === OpenVPN ===
def openvpn_cmd(config_file: str, tun_dev: str) -> list[str]:
    cmd = ["openvpn", "--config", config_file, "--dev", tun_dev, "--dev-type", "tun",
           "--pull-filter", "ignore", "route-ipv6", "--pull-filter", "ignore", "ifconfig-ipv6",
           "--route-delay", "2", "--connect-retry-max", "1", "--connect-timeout", "15",
           "--auth-user-pass", str(AUTH_FILE), "--auth-nocache", "--verb", "3",
           "--data-ciphers", "AES-128-CBC:AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
           "--route-nopull"]
    if Path("/etc/ssl/certs").exists():
        cmd.extend(["--capath", "/etc/ssl/certs"])
    return cmd

def connect_channel(ch: Channel, node: dict) -> bool:
    with ch.lock:
        if ch.process:
            stop_process(ch.process)
            ch.process = None
        cleanup_policy_routing(100 + ch.index)
        ch.state = "connecting"
        ch.node_id = node.get("id", "")
        ch.node_name = node.get("hostname", node.get("ip", ""))
        ch.node_ip = node.get("ip", node.get("remote_host", ""))
        ch.node_country = node.get("country_long", node.get("country", ""))
        ch.error = ""

    config_text = node.get("config_text", "")
    config_path = CONFIG_DIR / f"ch{ch.index}.ovpn"
    config_path.write_text(config_text)

    cmd = openvpn_cmd(str(config_path), ch.tun)
    log(f"[CH{ch.index}] Starting {ch.tun}...")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        ch.state = "error"
        ch.error = str(e)
        return False

    ok = False
    deadline = time.time() + 35
    tail = []
    while time.time() < deadline:
        try:
            line = proc.stdout.readline()
        except:
            break
        if not line:
            break
        line = line.strip()
        tail.append(line)
        tail = tail[-20:]
        if "initialization sequence completed" in line.lower():
            ok = True
            break
        if "auth_failed" in line.lower() or "authentication failed" in line.lower():
            ch.state = "error"
            ch.error = "AUTH_FAILED"
            stop_process(proc)
            return False

    if not ok:
        stop_process(proc)
        ch.state = "error"
        ch.error = tail[-1][:200] if tail else "timeout"
        log(f"[CH{ch.index}] Failed: {ch.error}")
        return False

    with ch.lock:
        ch.process = proc
        ch.state = "connected"
        ch.last_heartbeat = time.time()

    setup_policy_routing(ch.tun, 100 + ch.index)
    log(f"[CH{ch.index}] Connected! {ch.tun} :{ch.proxy_port}")
    return True

def disconnect_channel(ch: Channel):
    with ch.lock:
        stop_process(ch.process)
        ch.process = None
        cleanup_policy_routing(100 + ch.index)
        ch.state = "disconnected"
        ch.node_id = ""
        ch.node_name = ""
        ch.node_ip = ""
        ch.node_country = ""
        ch.node_latency = 0
        ch.error = ""
    try:
        (CONFIG_DIR / f"ch{ch.index}.ovpn").unlink(missing_ok=True)
    except:
        pass
    log(f"[CH{ch.index}] Disconnected")

# === Proxy ===
def start_all_proxies():
    import proxy_server_multi as proxy
    for ch in channels:
        threading.Thread(target=proxy.start_proxy_server,
                         args=(LOCAL_PROXY_HOST, ch.proxy_port, ch.tun),
                         daemon=True).start()
        time.sleep(0.1)

# === Node Fetching ===
def fetch_nodes() -> list[dict[str, Any]]:
    log("[fetch] Fetching nodes...")
    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log(f"[fetch] Failed: {e}")
        return []

    lines = raw.strip().split("\n")
    start = 0
    if lines and lines[0].startswith("*"):
        start = 1
    if len(lines) <= start + 1:
        return []

    csv_text = "\n".join(lines[start:])
    reader = csv.DictReader(io.StringIO(csv_text))
    nodes = []
    seen = set()
    for row in reader:
        node_id = row.get("#HostName", row.get("HostName", "")).strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        ip = row.get("IP", "").strip()
        port = row.get("Port", "443").strip()
        country = row.get("CountryLong", "").strip()
        config_b64 = row.get("OpenVPN_ConfigData_Base64", "").strip()
        if not config_b64:
            continue
        try:
            config_text = base64.b64decode(config_b64).decode("utf-8", errors="replace")
        except:
            continue
        try:
            ping_val = int(row.get("Ping", "0").strip())
        except:
            ping_val = 0
        speed = row.get("Speed", "0").strip()
        try:
            speed_val = int(speed) if speed else 0
        except:
            speed_val = 0
        nodes.append({
            "id": node_id, "ip": ip, "port": port,
            "country_long": country, "ping": ping_val,
            "speed": speed_val, "config_text": config_text,
        })

    def score(n):
        p = max(1, n["ping"]) if n["ping"] > 0 else 999
        s = n["speed"] if n["speed"] > 0 else 999
        return p + (s if s < 100 else s * 2)

    nodes.sort(key=score)
    nodes = nodes[:200]

    stripped = [{k: v for k, v in n.items() if k != "config_text"} for n in nodes]
    write_json(NODES_FILE, stripped)
    log(f"[fetch] {len(nodes)} nodes")
    return nodes

def collector_loop():
    global nodes_cache
    while True:
        try:
            nodes = fetch_nodes()
            if nodes:
                with nodes_cache_lock:
                    nodes_cache = nodes
        except Exception as e:
            log(f"[collector] Error: {e}")
        time.sleep(FETCH_INTERVAL)

# === Channel Manager ===
def get_best_node_for_country(country: str) -> dict | None:
    with nodes_cache_lock:
        candidates = list(nodes_cache)
    if not candidates:
        return None
    if country:
        filtered = [n for n in candidates if country.lower() in n.get("country_long", "").lower()]
        if not filtered:
            return None
        candidates = filtered
    pool = candidates[:min(30, len(candidates))]
    return random.choice(pool) if pool else None

# === Web UI ===
CHANNELS_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AimiliVPN 多通道</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f13;color:#e0e0e0}
.header{padding:20px 24px;border-bottom:1px solid rgba(255,255,255,0.06);display:flex;align-items:center;gap:12px}
.header h1{font-size:20px;font-weight:600}
.badge{background:#22c55e20;color:#22c55e;font-size:12px;padding:2px 10px;border-radius:12px;border:1px solid #22c55e30}
#node_count{margin-left:auto;color:#6b7280;font-size:13px}
.grid{padding:20px 24px;display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
.card{background:#1a1a24;border:1px solid rgba(255,255,255,0.06);border-radius:16px;padding:20px}
.card.ok{border-color:#22c55e40;background:#1a2a1e}
.card.busy{border-color:#f59e0b40;background:#2a2418}
.card.fail{border-color:#ef444440;background:#2a1818}
.flex{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.ct{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px}
.cn{background:#818cf820;color:#818cf8;font-size:11px;padding:2px 8px;border-radius:6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.g{background:#22c55e;box-shadow:0 0 8px #22c55e60}
.dot.y{background:#f59e0b;box-shadow:0 0 8px #f59e0b60}
.dot.r{background:#ef4444;box-shadow:0 0 8px #ef444460}
.dot.g2{background:#6b7280}
.body{font-size:13px;color:#9ca3af;line-height:1.8}
.l{color:#6b7280;font-size:11px}
.act{display:flex;gap:8px;margin-top:14px}
select{flex:1;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;padding:8px 10px;font-size:13px;outline:none}
select:focus{border-color:#818cf8}
.btn{background:#818cf8;border:none;color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;cursor:pointer;font-weight:500}
.btn:hover{background:#6d79e8}
.btn.d{background:#ef4444}
.btn.d:hover{background:#dc2626}
.btn:disabled{opacity:.4;cursor:not-allowed}
.i{display:flex;justify-content:space-between;padding:2px 0}
</style>
</head>
<body>
<div class="header">
  <h1>AimiliVPN</h1>
  <span class="badge">6通道</span>
  <span id="node_count">节点: 加载中...</span>
</div>
<div class="grid" id="grid"></div>
<script>
const CH={channels_json};
function r(d){
  const g=document.getElementById('grid');
  let h='';
  for(let i=0;i<CH.length;i++){
    const c=d.channels[i];
    const cls=c.state==='connected'?'card ok':c.state==='connecting'?'card busy':c.state==='error'?'card fail':'card';
    const dot=c.state==='connected'?'g':c.state==='connecting'?'y':c.state==='error'?'r':'g2';
    const st={connected:'已连接',connecting:'连接中',disconnected:'未连接',error:'错误'}[c.state]||c.state;
    h+='<div class="'+cls+'"><div class="flex"><div class="ct"><span class="cn">CH'+c.index+'</span><span class="dot '+dot+'"></span>'+st+'</div><span style="font-size:11px;color:#6b7280">'+c.tun+'</span></div><div class="body">';
    h+='<div class="i"><span class="l">出站IP</span><span>'+ (c.node_ip||'-') +'</span></div>';
    h+='<div class="i"><span class="l">国家</span><span>'+ (c.node_country||'-') +'</span></div>';
    h+='<div class="i"><span class="l">延迟</span><span>'+ (c.node_latency>0?c.node_latency+'ms':'-') +'</span></div>';
    h+='<div class="i"><span class="l">代理端口</span><span>'+c.proxy_port+'</span></div>';
    h+='<div class="i"><span class="l">节点</span><span style="font-size:11px;color:#6b7280;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+ (c.node_name||'-') +'</span></div>';
    if(c.error) h+='<div class="i"><span class="l">错误</span><span style="color:#ef4444">'+c.error+'</span></div>';
    h+='</div><div class="act"><select id="c_'+c.index+'"'+(c.state==='connecting'?' disabled':'')+'>';
    h+='<option value="">自动选择</option>';
    for(const co of (d.countries||[])) h+='<option value="'+co+'"'+(c.force_country===co?' selected':'')+'>'+co+'</option>';
    h+='</select>';
    if(c.state==='connected') h+='<button class="btn d" onclick="dc('+c.index+')">断开</button>';
    else h+='<button class="btn" onclick="con('+c.index+')"'+(c.state==='connecting'?' disabled':'')+'>连接</button>';
    h+='</div></div>';
  }
  g.innerHTML=h;
  document.getElementById('node_count').textContent='节点: '+(d.node_count||0);
}
async function rf(){try{const r=await fetch('/api/status');const d=await r.json();r(d)}catch(e){}}
async function con(i){const s=document.getElementById('c_'+i);await fetch('/api/channel/'+i+'/connect?country='+encodeURIComponent(s?s.value:''),{method:'POST'});setTimeout(rf,500)}
async function dc(i){await fetch('/api/channel/'+i+'/disconnect',{method:'POST'});rf()}
rf();setInterval(rf,3000);
</script>
</body>
</html>"""

# === Web Server ===
class Handler(BaseHTTPRequestHandler):
    def send_json(self, data: Any, status: int = HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = CHANNELS_HTML.replace("{channels_json}", json.dumps([c.to_dict() for c in channels]))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html.encode())))
            self.end_headers()
            self.wfile.write(html.encode())
        elif path == "/api/status":
            with nodes_cache_lock:
                countries = sorted(set(n.get("country_long", "") for n in nodes_cache if n.get("country_long")))
                node_count = len(nodes_cache)
            self.send_json({
                "channels": [c.to_dict() for c in channels],
                "countries": countries,
                "node_count": node_count,
            })
        elif path == "/api/nodes":
            with nodes_cache_lock:
                safe = [{k: v for k, v in n.items() if k != "config_text"} for n in nodes_cache]
            self.send_json({"nodes": safe})
        else:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        m = re.match(r"^/api/channel/(\d+)/(connect|disconnect)$", path)
        if not m:
            self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        idx = int(m.group(1))
        action = m.group(2)
        if idx < 0 or idx >= NUM_CHANNELS:
            self.send_json({"error": "channel out of range"}, HTTPStatus.BAD_REQUEST)
            return
        ch = channels[idx]
        if action == "connect":
            country = params.get("country", [""])[0]
            ch.force_country = country
            node = get_best_node_for_country(country)
            if not node:
                self.send_json({"error": "No nodes available"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            ok = connect_channel(ch, node)
            self.send_json({"ok": ok, "channel": ch.to_dict()})
        else:  # disconnect
            disconnect_channel(ch)
            ch.force_country = ""
            self.send_json({"ok": True, "channel": ch.to_dict()})

# === Main ===
def main():
    log("=== AimiliVPN 6-Channel Manager ===")

    try:
        subprocess.run(["pkill", "-f", "openvpn"], timeout=5, capture_output=True)
    except:
        pass

    ch_cfg = read_json(CHANNELS_FILE)
    for ch in channels:
        c = ch_cfg.get(str(ch.index), {})
        ch.force_country = c.get("force_country", "")

    start_all_proxies()
    log("[init] All 6 proxies started")

    threading.Thread(target=collector_loop, daemon=True).start()
    log("[init] Collector started")

    time.sleep(2)
    nodes = fetch_nodes()
    with nodes_cache_lock:
        nodes_cache = nodes

    for ch in channels:
        if ch.force_country:
            node = get_best_node_for_country(ch.force_country)
            if node:
                threading.Thread(target=connect_channel, args=(ch, node), daemon=True).start()
                time.sleep(2)

    class DualStackServer(ThreadingHTTPServer):
        allow_reuse_address = True

    log(f"[UI] Dashboard on http://{UI_HOST}:{UI_PORT}/")

    try:
        server = DualStackServer((UI_HOST, UI_PORT), Handler)
        server.serve_forever()
    except Exception:
        server = DualStackServer(("0.0.0.0", UI_PORT), Handler)
        server.serve_forever()

if __name__ == "__main__":
    main()
