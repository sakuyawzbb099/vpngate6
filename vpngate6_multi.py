#!/usr/bin/env python3
"""
vpngate6_multi.py - 6-Channel VPN Gateway + Node Management UI
Combines: multi-tunnel + full node table (IP info, filters, assign to channels)
"""
from __future__ import annotations
import base64, csv, io, json, os, random, re, shlex, socket, subprocess, sys
import threading, time, urllib.request, urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import vpn_utils

# === Auth ===
auth_token_store: dict[str, float] = {}

def load_auth_config() -> dict:
    return read_json(DATA_DIR / "ui_auth.json") or {"username":"admin","password":"admin"}

def check_auth_token(headers) -> bool:
    cookie = headers.get("Cookie","") or ""
    for part in cookie.split(";"):
        part=part.strip()
        if part.startswith("token="):
            tok=part[6:]
            entry=auth_token_store.get(tok)
            if entry and time.time()-entry<86400:
                return True
            elif entry:
                del auth_token_store[tok]
    return False

def generate_token() -> str:
    tok=secrets.token_hex(32)
    auth_token_store[tok]=time.time()
    return tok

import hashlib, secrets
LOGIN_HTML_CACHE = ""

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
IP_CACHE_FILE = DATA_DIR / "ip_cache.json"
BLACKLIST_FILE = DATA_DIR / "blacklist.json"

DATA_DIR.mkdir(exist_ok=True, parents=True)
CONFIG_DIR.mkdir(exist_ok=True, parents=True)
if not AUTH_FILE.exists():
    AUTH_FILE.write_text("vpn\nvpn\n")
    AUTH_FILE.chmod(0o600)

# === Channel State ===
class Channel:
    def __init__(self, index: int):
        self.index = index
        self.tun = f"tun{index}"
        self.proxy_port = PROXY_BASE_PORT + index
        self.force_country = ""
        self.force_ip_type = ""
        self.state = "disconnected"
        self.node_id = ""
        self.node_name = ""
        self.node_ip = ""
        self.node_country = ""
        self.node_owner = ""
        self.node_location = ""
        self.node_ip_type = ""
        self.node_latency = 0
        self.process: subprocess.Popen[str] | None = None
        self.error = ""
        self.last_heartbeat = 0.0
        self.lock = threading.Lock()

    def to_dict(self) -> dict:
        d = {"index": self.index, "tun": self.tun, "proxy_port": self.proxy_port,
             "force_country": self.force_country, "force_ip_type": self.force_ip_type, "state": self.state,
             "node_id": self.node_id, "node_name": self.node_name, "node_ip": self.node_ip,
             "node_country": self.node_country, "node_owner": self.node_owner,
             "node_location": self.node_location, "node_ip_type": self.node_ip_type,
             "node_latency": self.node_latency, "error": self.error}
        return d

channels: list[Channel] = [Channel(i) for i in range(NUM_CHANNELS)]
nodes_cache: list[dict[str, Any]] = []
nodes_cache_lock = threading.Lock()
_last_node_ids: set[str] = set()  # Track previous fetch for duplicate detection

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def read_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except: return {}

def write_json(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def stop_process(proc: subprocess.Popen[str] | None):
    if proc is None: return
    try: proc.terminate(); proc.wait(timeout=3)
    except:
        try: proc.kill(); proc.wait(timeout=2)
        except: pass

def cleanup_policy_routing(table: int):
    try: subprocess.run(["ip","rule","del","table",str(table)], capture_output=True, timeout=2)
    except: pass
    try: subprocess.run(["ip","route","flush","table",str(table)], capture_output=True, timeout=2)
    except: pass

def setup_policy_routing(tun_dev: str, table: int):
    cleanup_policy_routing(table)
    try:
        subprocess.run(["ip","route","add","default","dev",tun_dev,"table",str(table)], check=True, timeout=2)
        subprocess.run(["ip","rule","add","oif",tun_dev,"table",str(table)], check=True, timeout=2)
        for p in ["all","default",tun_dev]:
            try: subprocess.run(["sysctl","-w",f"net.ipv4.conf.{p}.rp_filter=2"], capture_output=True, timeout=2)
            except: pass
        log(f"[route {tun_dev}] table {table} OK")
    except Exception as e: log(f"[route {tun_dev}] Failed: {e}")

def openvpn_cmd(config_file: str, tun_dev: str) -> list[str]:
    cmd = ["openvpn","--config",config_file,"--dev",tun_dev,"--dev-type","tun",
           "--pull-filter","ignore","route-ipv6","--pull-filter","ignore","ifconfig-ipv6",
           "--route-delay","2","--connect-retry-max","1","--connect-timeout","15",
           "--auth-user-pass",str(AUTH_FILE),"--auth-nocache","--verb","3",
           "--data-ciphers","AES-128-CBC:AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",
           "--route-nopull"]
    if Path("/etc/ssl/certs").exists(): cmd.extend(["--capath","/etc/ssl/certs"])
    return cmd

def connect_channel(ch: Channel, node: dict) -> bool:
    with ch.lock:
        stop_process(ch.process); ch.process = None
        cleanup_policy_routing(100 + ch.index)
        ch.state = "connecting"
        ch.node_id = node.get("id","")
        ch.node_name = node.get("hostname",node.get("ip",""))
        ch.node_ip = node.get("ip",node.get("remote_host",""))
        ch.node_country = node.get("country_long",node.get("country",""))
        ch.node_owner = node.get("owner","")
        ch.node_location = node.get("location","")
        ch.node_ip_type = node.get("ip_type","")
        ch.error = ""
    config_text = node.get("config_text","")
    config_path = CONFIG_DIR / f"ch{ch.index}.ovpn"
    config_path.write_text(config_text)
    cmd = openvpn_cmd(str(config_path), ch.tun)
    log(f"[CH{ch.index}] Starting {ch.tun}...")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        ch.state = "error"; ch.error = str(e); return False
    ok = False; deadline = time.time() + 35; tail = []
    while time.time() < deadline:
        try: line = proc.stdout.readline()
        except: break
        if not line: break
        line = line.strip(); tail.append(line); tail = tail[-20:]
        if "initialization sequence completed" in line.lower(): ok = True; break
        if "auth_failed" in line.lower() or "authentication failed" in line.lower():
            ch.state = "error"; ch.error = "AUTH_FAILED"; stop_process(proc); return False
    if not ok:
        stop_process(proc); ch.state = "error"; ch.error = tail[-1][:200] if tail else "timeout"; return False
    with ch.lock: ch.process = proc; ch.state = "connected"; ch.last_heartbeat = time.time()
    setup_policy_routing(ch.tun, 100 + ch.index)
    log(f"[CH{ch.index}] Connected! {ch.tun} :{ch.proxy_port} {ch.node_ip}")
    return True

def disconnect_channel(ch: Channel):
    with ch.lock:
        stop_process(ch.process); ch.process = None
        cleanup_policy_routing(100 + ch.index)
        ch.state = "disconnected"; ch.node_id = ""; ch.node_name = ""; ch.node_ip = ""
        ch.node_country = ""; ch.node_owner = ""; ch.node_location = ""; ch.node_ip_type = ""
        ch.node_latency = 0; ch.error = ""
    try: (CONFIG_DIR / f"ch{ch.index}.ovpn").unlink(missing_ok=True)
    except: pass
    log(f"[CH{ch.index}] Disconnected")

def start_all_proxies():
    import proxy_server_multi as proxy
    for ch in channels:
        threading.Thread(target=proxy.start_proxy_server, args=(LOCAL_PROXY_HOST, ch.proxy_port, ch.tun), daemon=True).start()
        time.sleep(0.1)

# === Node Fetching ===
def fetch_nodes() -> list[dict[str, Any]]:
    log("[fetch] Fetching nodes...")
    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e: log(f"[fetch] Failed: {e}"); return []
    lines = raw.strip().split("\n")
    start = 0
    if lines and lines[0].startswith("*"): start = 1
    if len(lines) <= start + 1: return []
    csv_text = "\n".join(lines[start:])
    reader = csv.DictReader(io.StringIO(csv_text))
    nodes = []; seen = set()
    for row in reader:
        node_id = (row.get("#HostName") or row.get("HostName") or "").strip()
        if not node_id or node_id in seen: continue
        seen.add(node_id)
        ip = (row.get("IP") or "").strip()
        port = (row.get("Port") or "443").strip()
        country = (row.get("CountryLong") or "").strip()
        config_b64 = (row.get("OpenVPN_ConfigData_Base64") or "").strip()
        if not config_b64 or not ip: continue
        try: config_text = base64.b64decode(config_b64).decode("utf-8", errors="replace")
        except: continue
        try: ping_val = int((row.get("Ping") or "0").strip())
        except: ping_val = 0
        speed = (row.get("Speed") or "0").strip()
        try: speed_val = int(speed) if speed else 0
        except: speed_val = 0
        nodes.append({"id":node_id, "ip":ip, "port":port, "country_long":country, "ping":ping_val,
                       "speed":speed_val, "config_text":config_text, "hostname":node_id})
    def score(n):
        p = max(1, n["ping"]) if n["ping"] > 0 else 999
        s = n["speed"] if n["speed"] > 0 else 999
        return p + (s if s < 100 else s * 2)
    nodes.sort(key=score)
    nodes = nodes[:300]
    return nodes

def manual_fetch() -> dict:
    """Manually trigger a node fetch. Returns {new, dup, total}."""
    result = {"new": 0, "dup": 0, "total": 0}
    global _last_node_ids
    try:
        nodes = fetch_nodes()
        if nodes:
            try: vpn_utils.enrich_ip_info(nodes)
            except: pass
            new_ids = set(n.get("id","") for n in nodes if n.get("id"))
            result["total"] = len(nodes)
            if _last_node_ids:
                result["new"] = len(new_ids - _last_node_ids)
                result["dup"] = len(new_ids & _last_node_ids)
            else:
                result["new"] = len(new_ids)
            # Mark each node as new or duplicate
            for n in nodes:
                n["is_new"] = n.get("id","") not in _last_node_ids if _last_node_ids else True
            _last_node_ids = new_ids
            with nodes_cache_lock:
                global nodes_cache
                nodes_cache = nodes
            stripped = [{k:v for k,v in n.items() if k!="config_text"} for n in nodes]
            write_json(NODES_FILE, stripped)
            log(f"[manual_fetch] {len(nodes)} nodes (new:{result['new']} dup:{result['dup']})")
    except Exception as e:
        log(f"[manual_fetch] Error: {e}")
    return result


def collector_loop():
    global nodes_cache, _last_node_ids
    while True:
        try:
            nodes = fetch_nodes()
            if nodes:
                try: vpn_utils.enrich_ip_info(nodes)
                except Exception as e: log(f"[enrich] Error: {e}")
                new_ids = set(n.get("id","") for n in nodes if n.get("id"))
                for n in nodes:
                    n["is_new"] = True  # Auto-fetch always marks all as baseline
                if new_ids: _last_node_ids = new_ids
                with nodes_cache_lock: nodes_cache = nodes
                stripped = [{k:v for k,v in n.items() if k != "config_text"} for n in nodes]
                write_json(NODES_FILE, stripped)
                log(f"[fetch] {len(nodes)} nodes, enriched")
        except Exception as e: log(f"[collector] Error: {e}")
        time.sleep(FETCH_INTERVAL)

# === Channel Manager ===
def get_best_node_for_country(country: str, ip_type: str = "") -> dict | None:
    with nodes_cache_lock: candidates = list(nodes_cache)
    if not candidates: return None
    filtered = candidates
    if country:
        filtered = [n for n in filtered if country.lower() in n.get("country_long","").lower()]
    if ip_type:
        filtered = [n for n in filtered if n.get("ip_type","") == ip_type]
    if not filtered: return None
    pool = filtered[:min(30, len(filtered))]
    return random.choice(pool) if pool else None

def get_node_by_id(node_id: str) -> dict | None:
    with nodes_cache_lock:
        for n in nodes_cache:
            if n.get("id") == node_id: return n
    return None

# === Web UI ===
PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AimiliVPN 6通道</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f13;color:#e0e0e0;font-size:14px}
.hd{padding:16px 20px;border-bottom:1px solid rgba(255,255,255,0.06);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.hd h1{font-size:18px;font-weight:600}
.bdg{background:#22c55e20;color:#22c55e;font-size:11px;padding:2px 10px;border-radius:10px;border:1px solid #22c55e30}
.nc{margin-left:auto;color:#6b7280;font-size:13px}
.grid{padding:12px 16px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:600px){.grid{grid-template-columns:1fr}}
.card{background:#1a1a24;border:1px solid rgba(255,255,255,0.06);border-radius:12px;padding:14px}
.card.on{border-color:#22c55e40;background:#1a2a1e}
.card.bz{border-color:#f59e0b40;background:#2a2418}
.card.fail{border-color:#ef444440;background:#2a1818}
.chf{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.ct{font-size:14px;font-weight:600;display:flex;align-items:center;gap:6px}
.cn{background:#818cf820;color:#818cf8;font-size:10px;padding:2px 8px;border-radius:6px}
.dt{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dt.g{background:#22c55e;box-shadow:0 0 6px #22c55e60}
.dt.y{background:#f59e0b;box-shadow:0 0 6px #f59e0b60}
.dt.r{background:#ef4444;box-shadow:0 0 6px #ef444460}
.dt.g2{background:#6b7280}
.bd{font-size:12px;color:#9ca3af;line-height:1.7}
.ll{color:#6b7280;font-size:11px}
.ac{display:flex;gap:8px;margin-top:12px}
.ac select{flex:1;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;padding:6px 8px;font-size:12px;outline:none}
.ac select:focus{border-color:#818cf8}
.btn{background:#818cf8;border:none;color:#fff;padding:6px 14px;border-radius:8px;font-size:12px;cursor:pointer;font-weight:500}
.btn:hover{background:#6d79e8}
.btn.d{background:#ef4444}.btn.d:hover{background:#dc2626}
.btn:disabled{opacity:.4;cursor:not-allowed}
.i{display:flex;justify-content:space-between;padding:2px 0}
.section{margin:12px 16px}
.ftr{display:flex;gap:10px;margin:8px 16px;flex-wrap:wrap;align-items:center}
.ftr select{background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;padding:6px 10px;font-size:12px;outline:none}
.ftr select:focus{border-color:#818cf8}
.ftr .btn{padding:6px 12px;font-size:12px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;padding:8px 10px;color:#6b7280;font-weight:500;border-bottom:1px solid rgba(255,255,255,0.06);white-space:nowrap;position:sticky;top:0;background:#0f0f13;z-index:1}
.tbl td{padding:7px 10px;border-bottom:1px solid rgba(255,255,255,0.03);vertical-align:middle}
.tbl tr:hover{background:rgba(255,255,255,0.02)}
.sta{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px}
.sta.ok{background:#22c55e15;color:#22c55e}
.sta.no{background:#ef444415;color:#ef4444}
.sta.na{background:#6b728015;color:#6b7280}
.act-cell{display:flex;gap:4px;flex-wrap:wrap}
.act-cell .btn{font-size:11px;padding:3px 8px}
.tp{display:inline-block;padding:1px 6px;border-radius:3px;font-size:10px}
.tp.r{background:#22c55e15;color:#22c55e}
.tp.h{background:#f59e0b15;color:#f59e0b}
.tp.m{background:#818cf815;color:#818cf8}
.tp.u{background:#6b728015;color:#6b7280}
.ow{max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-c{background:#1a1a24;border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:24px;min-width:280px;max-width:400px}
.modal-c h3{margin-bottom:12px;font-size:15px}
.modal-c select{width:100%;margin:8px 0;padding:8px;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;font-size:13px}
.modal-c .btns{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
</style>
</head>
<body>
<div class="hd">
  <h1>AimiliVPN</h1>
  <span class="bdg">6通道</span>
  <span class="nc" id="nc">节点: 加载中...</span>
  <button class="btn" style="background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);color:#e0e0e0" onclick="showAdmin()">管理员</button>
</div>
<div class="grid" id="grid"></div>

<div class="section" style="margin-top:0">
  <div class="ftr">
    <select id="f_status"><option value="">全部节点</option><option value="available">可用节点</option><option value="failed">失效节点</option></select>
    <select id="f_country"><option value="">所有国家</option></select>
    <select id="f_type"><option value="">所有IP类型</option><option value="residential">住宅IP</option><option value="hosting">机房IP</option></select>
    <button class="btn" onclick="refreshNodes()">刷新</button>
    <button class="btn" onclick="fetchNodes()">获取节点</button>
    <span id="rc" style="color:#6b7280;font-size:12px;margin-left:auto"></span>
  </div>
  <div style="overflow-x:auto">
  <table class="tbl" id="tbl"><thead><tr>
    <th>状态</th><th>IP : 端口</th><th>物理位置</th><th>运营主体 / ISP</th><th>IP类型</th><th>操作</th>
  </tr></thead><tbody id="tb"></tbody></table>
  </div>
</div>

<div class="modal" id="modal"><div class="modal-c">
  <h3>分配到通道</h3>
  <p style="font-size:12px;color:#9ca3af;margin-bottom:8px" id="m_node_info"></p>
  <select id="m_ch"></select>
  <div class="btns">
    <button class="btn" onclick="switchNode()">确认切换</button>
    <button class="btn d" onclick="closeModal()">取消</button>
  </div>
</div></div>

<script>
var CH=[]; var CUR_NODE=null;
var _saved={}; // Save dropdown state across renders
function render(d){
  // Preserve current dropdown selections
  for(var i=0;i<6;i++){
    var el=document.getElementById('cs_'+i);if(el)_saved['cs_'+i]=el.value;
    var el2=document.getElementById('ipt_'+i);if(el2)_saved['ipt_'+i]=el2.value;
  }
  CH=d.channels; var g=document.getElementById('grid'),h='';
  for(var i=0;i<CH.length;i++){
    var c=CH[i];
    var cls=c.state==='connected'?'card on':c.state==='connecting'?'card bz':c.state==='error'?'card fail':'card';
    var dt=c.state==='connected'?'g':c.state==='connecting'?'y':c.state==='error'?'r':'g2';
    var st={connected:'已连接',connecting:'连接中',disconnected:'未连接',error:'错误'}[c.state]||c.state;
    var it=c.node_ip_type;
    var ipc=c.node_ip_type==='residential'?'tp r':c.node_ip_type==='hosting'?'tp h':c.node_ip_type==='mobile'?'tp m':'tp u';
    h+='<div class="'+cls+'"><div class="chf"><div class="ct"><span class="cn">CH'+c.index+'</span><span class="dt '+dt+'"></span>'+st+'</div><span style="font-size:10px;color:#6b7280">'+c.tun+'</span></div>';
    h+='<div class="bd"><div class="i"><span class="ll">出口IP</span><span>'+ (c.node_ip||'-') +'</span></div>';
    h+='<div class="i"><span class="ll">国家</span><span>'+ (c.node_country||'-') +'</span></div>';
    h+='<div class="i"><span class="ll">位置</span><span style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+ (c.node_location||'-') +'</span></div>';
    h+='<div class="i"><span class="ll">运营主体</span><span class="ow" title="'+c.node_owner+'">'+ (c.node_owner||'-') +'</span></div>';
    h+='<div class="i"><span class="ll">IP类型</span><span'+(ipc?' class="'+ipc+'"':'')+'>'+ (it||'-') +'</span></div>';
    h+='<div class="i"><span class="ll">延迟</span><span>'+ (c.node_latency>0?c.node_latency+'ms':'-') +'</span></div>';
    h+='<div class="i"><span class="ll">代理</span><span>:'+c.proxy_port+'</span></div>';
    if(c.error) h+='<div class="i"><span class="ll">错误</span><span style="color:#ef4444">'+c.error+'</span></div>';
    h+='</div><div class="ac"><select id="cs_'+c.index+'"'+(c.state==='connecting'?' disabled':'')+'>';
    h+='<option value="">自动选择</option>';
    if(d.countries) for(var j=0;j<d.countries.length;j++){
      var co=d.countries[j];
      var sel=(_saved['cs_'+c.index]===co)||(c.force_country===co)?' selected':'';
      h+='<option value="'+co+'"'+sel+'>'+co+'</option>';
    }
    h+='</select>';
    var savedIpt=_saved['ipt_'+c.index]||'';
    h+='<select id="ipt_'+c.index+'"'+(c.state==='connecting'?' disabled':'')+'>';
    h+='<option value="">全部IP</option><option value="residential"'+(savedIpt==='residential'||c.force_ip_type==='residential'?' selected':'')+'>住宅</option>';
    h+='<option value="hosting"'+(savedIpt==='hosting'||c.force_ip_type==='hosting'?' selected':'')+'>机房</option>';
    h+='<option value="mobile"'+(savedIpt==='mobile'||c.force_ip_type==='mobile'?' selected':'')+'>移动</option></select>';
    if(c.state==='connected') h+='<button class="btn d" onclick="dc('+c.index+')">断开</button>';
    else h+='<button class="btn" onclick="connectAuto('+c.index+')"'+(c.state==='connecting'?' disabled':'')+'>连接</button>';
    h+='</div></div>';
  }
  g.innerHTML=h; document.getElementById('nc').textContent='节点: '+(d.node_count||0);
}
async function rf(){try{var r=await fetch('/api/status');var d=await r.json();render(d)}catch(e){}}
async function connectAuto(i){
  var s=document.getElementById('cs_'+i);var c=s?s.value:'';
  var t=document.getElementById('ipt_'+i);var ip=t?t.value:'';
  await fetch('/api/channel/'+i+'/connect?country='+encodeURIComponent(c)+'&ip_type='+encodeURIComponent(ip),{method:'POST'});
  setTimeout(rf,500)}
async function fetchNodes(){
  document.getElementById('rc').textContent='获取中...';
  try{
    var r=await fetch('/api/fetch_nodes',{method:'POST'});
    var d=await r.json();
    if(d.ok){
      var msg='获取完成: '+d.total+' 节点';
      if(d.new>0) msg+=', 新增 '+d.new;
      if(d.dup>0) msg+=', 重复 '+d.dup;
      document.getElementById('rc').textContent=msg;
    }
  }catch(e){}
  setTimeout(refreshNodes,3000);}
async function dc(i){await fetch('/api/channel/'+i+'/disconnect',{method:'POST'});rf()}
rf();setInterval(rf,3000);

function openSwitch(node_id,node_ip,node_country){
  CUR_NODE=node_id;
  document.getElementById('m_node_info').textContent=node_ip+' ('+node_country+')';
  var sel=document.getElementById('m_ch'); sel.innerHTML='';
  for(var i=0;i<CH.length;i++){
    var opt=document.createElement('option'); opt.value=i;
    opt.textContent='CH'+i+' ('+CH[i].tun+')'+(CH[i].state==='connected'?' - '+CH[i].node_ip:' - 未连接');
    sel.appendChild(opt);
  }
  document.getElementById('modal').classList.add('show');
}
async function switchNode(){
  var idx=document.getElementById('m_ch').value;
  await fetch('/api/channel/'+idx+'/connect?node_id='+encodeURIComponent(CUR_NODE),{method:'POST'});
  closeModal(); setTimeout(rf,500);
}
function closeModal(){document.getElementById('modal').classList.remove('show');CUR_NODE=null}

async function refreshNodes(){
  var st=document.getElementById('f_status').value;
  var ct=document.getElementById('f_country').value;
  var tp=document.getElementById('f_type').value;
  var q='?filter='+st+'&country='+encodeURIComponent(ct)+'&ip_type='+encodeURIComponent(tp);
  try{
    var r=await fetch('/api/nodes'+q); var d=await r.json();
    var tb=document.getElementById('tb'),h='';
    for(var i=0;i<d.nodes.length;i++){
      var n=d.nodes[i];
      var av=n.available?'sta ok':'sta no';
      var avt=n.available?'可用':'不可用';
      var ipc=n.ip_type==='residential'?'tp r':n.ip_type==='hosting'?'tp h':n.ip_type==='mobile'?'tp m':'tp u';
      h+='<tr><td><span class="sta '+av+'">'+avt+'</span></td>';
      h+='<td>'+n.ip+':'+n.port+'</td>';
      h+='<td class="ow" title="'+n.location+'">'+(n.location||'-')+'</td>';
      h+='<td class="ow" title="'+n.owner+'">'+(n.owner||'-')+'</td>';
      h+='<td>'+(n.ip_type?'<span class="'+ipc+'">'+(n.ip_type==='residential'?'住宅':n.ip_type==='hosting'?'机房':n.ip_type)+'</span>':'-')+'</td>';
      h+='<td class="act-cell">';
      if(n.available) h+='<button class="btn" onclick="openSwitch(\''+n.id+'\',\''+n.ip+':'+n.port+'\',\''+(n.country_long||'')+'\')">切换</button>';
      else h+='<button class="btn" disabled>切换</button>';
      h+='</td></tr>';
    }
    tb.innerHTML=h; document.getElementById('rc').textContent='共 '+d.total+' 个节点 (显示 '+d.nodes.length+')';
  }catch(e){}
}
async function loadCountries(){
  try{
    var r=await fetch('/api/status'); var d=await r.json();
    var sel=document.getElementById('f_country');
    if(d.countries) for(var i=0;i<d.countries.length;i++){var o=document.createElement('option');o.value=d.countries[i];o.textContent=d.countries[i];sel.appendChild(o)}
  }catch(e){}
}
loadCountries(); refreshNodes();
</script>

<div class="modal" id="adminModal"><div class="modal-c">
  <h3>修改账号密码</h3>
  <div class="fg" style="margin-bottom:12px"><label>当前账号</label><input type="text" id="a_user" style="width:100%;padding:8px;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;font-size:13px;outline:none"></div>
  <div class="fg" style="margin-bottom:12px"><label>当前密码</label><input type="password" id="a_pass" style="width:100%;padding:8px;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;font-size:13px;outline:none"></div>
  <div class="fg" style="margin-bottom:12px"><label>新密码</label><input type="password" id="a_newpass" style="width:100%;padding:8px;background:#0f0f13;border:1px solid rgba(255,255,255,0.1);border-radius:8px;color:#e0e0e0;font-size:13px;outline:none"></div>
  <div class="btns" style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
    <button class="btn" onclick="changePwd()">确认修改</button>
    <button class="btn d" onclick="document.getElementById('adminModal').classList.remove('show')">取消</button>
  </div>
  <div id="a_msg" style="color:#22c55e;font-size:12px;margin-top:8px;display:none"></div>
</div></div>

<script>
function showAdmin(){
  document.getElementById("adminModal").classList.add("show");
  document.getElementById("a_msg").style.display="none";
}
async function changePwd(){
  var u=document.getElementById("a_user").value;
  var p=document.getElementById("a_pass").value;
  var n=document.getElementById("a_newpass").value;
  var r=await fetch("/api/admin/change_password",{method:"POST",body:new URLSearchParams({username:u,password:p,new_password:n})});
  var d=await r.json();
  var msg=document.getElementById("a_msg");msg.style.display="block";
  if(d.ok){msg.style.color="#22c55e";msg.textContent="密码修改成功！请重新登录";setTimeout(function(){location.href="/"},2000)}
  else{msg.style.color="#ef4444";msg.textContent=d.error||"修改失败"}
}
</script>
</body>
</html>"""

# === Web Server ===
class Handler(BaseHTTPRequestHandler):
    def send_json(self, data: Any, status: int = HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, fmt, *args): pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if not check_auth_token(self.headers):
            if path == "/api/login":
                self.send_json({"ok":False})
                return
            global LOGIN_HTML_CACHE
            if not LOGIN_HTML_CACHE:
                try:
                    LOGIN_HTML_CACHE = open(str(DATA_DIR / "login.html")).read()
                except:
                    LOGIN_HTML_CACHE = "<html><body><h2>Login page not found</h2></body></html>"
            body = LOGIN_HTML_CACHE.replace("${error_display}", "block" if params.get("error") else "none").encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return

        if path in ("/","/index.html"):
            html = PAGE_HTML.replace("{channels_json}",json.dumps([c.to_dict() for c in channels]))
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(html.encode())))
            self.end_headers(); self.wfile.write(html.encode())
        elif path == "/api/status":
            with nodes_cache_lock:
                countries = sorted(set(n.get("country_long","") for n in nodes_cache if n.get("country_long")))
                node_count = len(nodes_cache)
            self.send_json({"channels":[c.to_dict() for c in channels],"countries":countries,"node_count":node_count})
        elif path == "/api/nodes":
            filter_type = params.get("filter",[""])[0]
            country = params.get("country",[""])[0]
            ip_type = params.get("ip_type",[""])[0]
            with nodes_cache_lock:
                nodes = list(nodes_cache)
            result = []
            for n in nodes:
                ip = n.get("ip","")
                port = n.get("port","443")
                available = n.get("ping",999) > 0 and n.get("ping",999) < 999  # rough availability
                # Actually check: the node has config_text means it has a config
                available = bool(n.get("config_text"))
                n_country = n.get("country_long","")
                n_ip_type = n.get("ip_type","")
                if country and country.lower() not in n_country.lower(): continue
                if ip_type and n_ip_type != ip_type: continue
                if filter_type == "available" and not available: continue
                if filter_type == "failed" and available: continue
                result.append({
                    "id": n.get("id",""), "ip": ip, "port": port,
                    "country_long": n_country, "available": available,
                    "location": n.get("location",""), "owner": n.get("owner",""),
                    "ip_type": n_ip_type, "asn": n.get("asn",""),
                    "is_new": n.get("is_new", False),
                })
            self.send_json({"nodes":result[:200], "total":len(result)})
        else:
            self.send_json({"error":"not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        # Login POST endpoint
        if path == "/api/login":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                p = urllib.parse.parse_qs(body)
                user = (p.get("username", [""])[0] or "").strip()
                pwd = (p.get("password", [""])[0] or "").strip()
                cfg = load_auth_config()
                if user == cfg.get("username", "admin") and pwd == cfg.get("password", "admin"):
                    tok = generate_token()
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Set-Cookie", f"token={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
                    self.send_header("Location", "/")
                    self.end_headers()
                else:
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", "/?error=1")
                    self.end_headers()
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/fetch_nodes":
            res = manual_fetch()
            self.send_json({"ok": True, "new": res["new"], "dup": res["dup"], "total": res["total"]})
            return

        # Change password
        if path == "/api/admin/change_password":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                p = urllib.parse.parse_qs(body)
                user = (p.get("username", [""])[0] or "").strip()
                pwd = (p.get("password", [""])[0] or "").strip()
                new_pwd = (p.get("new_password", [""])[0] or "").strip()
                if len(new_pwd) < 4:
                    self.send_json({"ok": False, "error": "密码至少4位"}, HTTPStatus.BAD_REQUEST)
                    return
                cfg = load_auth_config()
                if user == cfg.get("username", "admin") and pwd == cfg.get("password", "admin"):
                    cfg["password"] = new_pwd
                    write_json(DATA_DIR / "ui_auth.json", cfg)
                    log(f"[auth] Password changed by {user}")
                    self.send_json({"ok": True})
                else:
                    self.send_json({"ok": False, "error": "当前账号或密码错误"}, HTTPStatus.UNAUTHORIZED)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, HTTPStatus.BAD_REQUEST)
            return

        m = re.match(r"^/api/channel/(\d+)/(connect|disconnect)$", path)
        if not m: self.send_json({"error":"not found"}, HTTPStatus.NOT_FOUND); return
        idx = int(m.group(1)); action = m.group(2)
        if idx < 0 or idx >= NUM_CHANNELS: self.send_json({"error":"out of range"}, HTTPStatus.BAD_REQUEST); return
        ch = channels[idx]
        if action == "disconnect":
            disconnect_channel(ch)
            ch.force_country = ""
            ch.force_ip_type = ""
            self.send_json({"ok":True,"channel":ch.to_dict()})
        else:
            node_id = params.get("node_id",[None])[0]
            country = params.get("country",[""])[0]
            ip_type = params.get("ip_type",[""])[0]
            if node_id:
                node = get_node_by_id(node_id)
                if not node: self.send_json({"error":"node not found"}, HTTPStatus.NOT_FOUND); return
                # Also set force_country so watchdog can auto-reconnect
                node_country = node.get("country_long","")
                if node_country: ch.force_country = node_country
                node_iptype = node.get("ip_type","")
                if node_iptype: ch.force_ip_type = node_iptype
            else:
                node = get_best_node_for_country(country, ip_type)
                if not node: self.send_json({"error":"No nodes"}, HTTPStatus.SERVICE_UNAVAILABLE); return
                if country: ch.force_country = country
                ch.force_ip_type = ip_type
            ok = connect_channel(ch, node)
            self.send_json({"ok":ok,"channel":ch.to_dict()})

# === Main ===
def channel_watchdog():
    """Watchdog: detect dead connections and auto-reconnect if channel has criteria set."""
    while True:
        time.sleep(20)
        for ch in channels:
            try:
                with ch.lock:
                    if ch.state == "connecting":
                        continue  # Still trying to connect, skip
                    if ch.state == "connected":
                        # Check if OpenVPN process died
                        if ch.process is None or ch.process.poll() is not None:
                            ch.state = "disconnected"
                            ch.process = None
                            log(f"[WD CH{ch.index}] Connection lost")
                        else:
                            continue
                    # Auto-reconnect if criteria are set
                    if ch.state in ("disconnected", "error") and ch.force_country:
                        node = get_best_node_for_country(ch.force_country, ch.force_ip_type)
                        if node:
                            log(f"[WD CH{ch.index}] Reconnect {ch.force_country} {ch.force_ip_type}")
                            threading.Thread(target=connect_channel, args=(ch, node), daemon=True).start()
                        else:
                            log(f"[WD CH{ch.index}] No node for {ch.force_country} {ch.force_ip_type}")
            except Exception as e:
                log(f"[WD CH{ch.index}] Error: {e}")

def main():
    log("=== AimiliVPN 6-Channel Manager + Node UI ===")
    try: subprocess.run(["pkill","-f","openvpn"], timeout=5, capture_output=True)
    except: pass
    ch_cfg = read_json(CHANNELS_FILE)
    for ch in channels:
        c = ch_cfg.get(str(ch.index),{})
        ch.force_country = c.get("force_country","")
        ch.force_ip_type = c.get("force_ip_type","")
    start_all_proxies()
    log("[init] 6 proxies started")
    threading.Thread(target=collector_loop, daemon=True).start()
    log("[init] Collector started")
    threading.Thread(target=channel_watchdog, daemon=True).start()
    log("[init] Watchdog started")
    time.sleep(2)
    # Initial fetch
    nodes = fetch_nodes()
    if nodes:
        try: vpn_utils.enrich_ip_info(nodes)
        except: pass
        with nodes_cache_lock: nodes_cache = nodes
        stripped = [{k:v for k,v in n.items() if k!="config_text"} for n in nodes]
        write_json(NODES_FILE, stripped)
    for ch in channels:
        if ch.force_country:
            node = get_best_node_for_country(ch.force_country)
            if node:
                threading.Thread(target=connect_channel, args=(ch,node), daemon=True).start()
                time.sleep(2)
    class DualStackServer(ThreadingHTTPServer):
        allow_reuse_address = True
    log(f"[UI] http://{UI_HOST}:{UI_PORT}/")
    try:
        server = DualStackServer((UI_HOST, UI_PORT), Handler); server.serve_forever()
    except Exception:
        server = DualStackServer(("0.0.0.0", UI_PORT), Handler); server.serve_forever()

if __name__ == "__main__":
    main()
