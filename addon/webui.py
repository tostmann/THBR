#!/usr/bin/env python3
"""The add-on's web face, served through Home Assistant's ingress.

Three things the log could not give a user: what state the stick is in, a way
to update its firmware, and a way to restart it.  Plus a pass-through to the
border router's own GUI, which lives on the private backbone and is otherwise
unreachable from a browser.

Everything is relative-path only: ingress serves this under a generated prefix
(/api/hassio_ingress/<token>/), so an absolute link would leave the add-on.
"""
import html
import ipaddress
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Filled in by thbrctl at start-up.
CTX = {"env": {}, "req_file": "", "res_file": "", "log_path": ""}

# Home Assistant's ingress proxy reaches this server from the Supervisor's own
# docker network; a browser on the LAN does not.  The add-on has to run in the
# host's network namespace for the tap device, so the port is on every host
# interface and the source address is what separates the two.
#
# Not the X-Ingress-Path header: anything on the LAN can set that itself, which
# makes it a label, not a control.
#
# The ranges are the Supervisor's own, from supervisor/const.py —
# DOCKER_IPV4_NETWORK_MASK and DOCKER_IPV6_NETWORK_MASK.
INGRESS_NETS = ("127.0.0.0/8", "::1/128", "172.30.32.0/23", "fd0c:ac1e:2100::/48")


def _log(msg):
    print(f"{time.strftime('%H:%M:%S')} [thbr] {msg}", flush=True)


def parse_allow(spec):
    """Networks that may reach this server, or None for no restriction.

    "ingress" is the Supervisor's networks, "any" lifts the restriction, and
    anything else is read as a comma-separated list of addresses or CIDRs.
    """
    spec = (spec or "").strip()
    if spec.lower() in ("any", "all", "*"):
        return None
    parts = INGRESS_NETS if spec.lower() in ("", "ingress") else spec.split(",")
    nets = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            _log(f"web access: ignoring '{part}', not an address or network")
    return nets or [ipaddress.ip_network("127.0.0.0/8")]

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>THBR</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--fg:#1c1e21;--mut:#6b7280;--line:#e3e6ea;--card:#fff;
  --acc:#0a66c2;--ok:#0a7f3f;--warn:#a5720b;--bad:#b42318;--shadow:0 1px 2px rgba(0,0,0,.06)}
@media(prefers-color-scheme:dark){:root{--bg:#0f1319;--fg:#e7ebf0;--mut:#98a3b3;
  --line:#242c38;--card:#161c25;--acc:#4b90f7;--ok:#3fbf78;--warn:#e0a33a;--bad:#f0685c;
  --shadow:0 1px 2px rgba(0,0,0,.4)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:60rem;margin:0 auto;padding:1.25rem 1rem 3rem}
header{display:flex;align-items:center;gap:.75rem;flex-wrap:wrap;margin-bottom:1.25rem}
header .logo{height:38px;width:auto;display:block}
header h1{font-size:1.25rem;margin:0;letter-spacing:-.01em}
header .ver{color:var(--mut);font-size:.85rem}
header .sp{flex:1}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
  box-shadow:var(--shadow);margin-bottom:1rem}
.card>h2{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
  margin:0;padding:.75rem 1rem;border-bottom:1px solid var(--line)}
.card>.body{padding:.75rem 1rem}
table{border-collapse:collapse;width:100%}
td,th{padding:.32rem .5rem;border-bottom:1px solid var(--line);text-align:left;font-size:.9rem;
  vertical-align:top}
tr:last-child td,tr:last-child th{border-bottom:none}
th{width:13rem;font-weight:600;color:var(--mut)}
table.grid th{width:auto;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.warn{color:var(--warn);font-weight:600}.mut{color:var(--mut)}
button{font:inherit;padding:.45rem .85rem;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--fg);cursor:pointer;margin-right:.5rem}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
button:disabled{opacity:.5;cursor:default}
pre{background:var(--bg);padding:.6rem;border-radius:8px;overflow:auto;max-height:15rem;
  font-size:.79rem;line-height:1.4;margin:0;border:1px solid var(--line)}
details>summary::marker{color:var(--mut)}
details[open]>summary{margin-bottom:.2rem}
footer{margin-top:2rem;padding-top:1rem;border-top:1px solid var(--line);
  color:var(--mut);font-size:.82rem;text-align:center}
footer a{color:var(--mut)} footer a:hover{color:var(--acc)}
#msg{font-size:.9rem;color:var(--mut)}
</style></head><body><div class="wrap">

<header>
  <picture>
    <source srcset="logo-dark.png" media="(prefers-color-scheme: dark)">
    <img src="logo.png" alt="busware" class="logo" onerror="this.style.display='none'">
  </picture>
  <h1>ESP32-C6 Thread Border Router</h1>
  <span class="ver" id="ver"></span>
  <span class="sp"></span>
  <span id="hdrstat" class="mut"></span>
</header>

<div class="card"><h2>Stick</h2><div class="body">
  <table id="t_stick"><tr><td class="mut">wird geladen …</td></tr></table>
</div></div>

<div class="card"><h2>Thread-Netz</h2><div class="body">
  <table id="t_net"><tr><td class="mut">—</td></tr></table>
</div></div>

<div class="card"><h2>Topologie</h2><div class="body">
  <svg id="graph" viewBox="0 0 640 340" style="width:100%;height:auto;max-height:20rem"></svg>
  <div class="mut" style="font-size:.8rem;margin-top:.4rem">
    Linienstärke = Funkgüte (LinkQuality 1–3) &middot; gestrichelt = Kind &middot;
    Ring = Leader
  </div>
  <details style="margin-top:.6rem">
    <summary style="cursor:pointer;color:var(--mut);font-size:.85rem">Knotenliste</summary>
    <table class="grid" id="t_topo" style="margin-top:.5rem"><tr><td class="mut">—</td></tr></table>
  </details>
</div></div>

<div class="card"><h2>Backbone</h2><div class="body">
  <table id="t_bb"><tr><td class="mut">—</td></tr></table>
</div></div>

<div class="card"><h2>Aktionen</h2><div class="body">
  <button class="primary" id="flash">Firmware schreiben</button>
  <button id="reboot">Stick neu starten</button>
  <button id="backup">Netzdaten sichern</button>
  <span id="msg"></span>
  <div id="backups" class="mut" style="margin-top:.6rem;font-size:.85rem"></div>
</div></div>

<div class="card"><h2>Protokoll</h2><div class="body">
  <pre id="log">…</pre>
  <div style="margin-top:.6rem"><button id="clearlog">Protokoll leeren</button></div>
</div></div>

<footer>
  <span>&copy; 2026 Dirk Tostmann</span> &middot;
  <span><a href="https://polyformproject.org/licenses/noncommercial/1.0.0" target="_blank" rel="noopener">PolyForm Noncommercial 1.0.0</a></span> &middot;
  <span><a href="https://github.com/tostmann/THBR" target="_blank" rel="noopener">THBR auf GitHub</a></span>
</footer>

</div><script>
// Ingress serves this under /api/hassio_ingress/<token>, sometimes without the
// trailing slash — a relative "api/status" would then resolve one level too
// high and never reach the add-on.  Build the base once, explicitly.
const BASE=(location.pathname.endsWith('/')?location.pathname:location.pathname+'/');
const url=p=>BASE+p;
const $=i=>document.getElementById(i);
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const row=(k,v,cls)=>`<tr><th>${esc(k)}</th><td class="${cls||''}">${v}</td></tr>`;
const na='<span class="mut">—</span>';

async function refresh(){
  let s={};
  try{ s=await (await fetch(url('api/status'))).json(); }
  catch(e){ $('hdrstat').innerHTML='<span class="bad">nicht erreichbar</span>'; return; }

  $('ver').textContent = [s.release?'THBR '+s.release:'',
                          s.installed?'Firmware '+s.installed:''].filter(Boolean).join(' \u00b7 ');
  $('hdrstat').innerHTML = s.br==='running'
    ? '<span class="ok">Border Routing läuft</span>'
    : '<span class="bad">Border Routing: '+esc(s.br||'keine Antwort')+'</span>';

  let h='';
  h+=row('THBR-Version', s.release?'<span class="mono">'+esc(s.release)+'</span>':na);
  h+=row('Firmware auf dem Stick', s.installed?'<span class="mono">'+esc(s.installed)+'</span>':na, s.installed?'':'bad');
  h+=row('Firmware in diesem Add-on', s.bundled?'<span class="mono">'+esc(s.bundled)+'</span>':na,
         (s.installed&&s.bundled&&s.installed!==s.bundled)?'warn':'');
  h+=row('Laufzeit', esc(s.uptime||'—'));
  h+=row('Freier Speicher', s.heap?esc(s.heap)+' Byte':na);
  h+=row('Chip / IDF', s.chip?esc(s.chip)+' / '+esc(s.idf||''):na);
  h+=row('MAC', s.mac?'<span class="mono">'+esc(s.mac)+'</span>':na);
  if(s.flash_result) h+=row('Letztes Firmware-Schreiben', esc(s.flash_result), s.flash_result==='OK'?'ok':'warn');
  if(s.restore_result) h+=row('Letztes Zur&uuml;ckspielen', esc(s.restore_result), s.restore_result==='OK'?'ok':'bad');
  $('t_stick').innerHTML=h;

  const m=s.mesh_info||{};
  h='';
  h+=row('Netzname', m.name?esc(m.name):na);
  h+=row('Rolle', esc(s.role||'—'), s.role==='leader'||s.role==='router'?'ok':'');
  h+=row('Kanal', m.channel?esc(m.channel):na);
  h+=row('PAN ID', m.panid?'<span class="mono">'+esc(m.panid)+'</span>':na);
  h+=row('Extended PAN ID', m.extpanid?'<span class="mono">'+esc(m.extpanid)+'</span>':na);
  h+=row('Router im Netz', m.routers!=null?esc(m.routers):na);
  h+=row('Netzschlüssel', '<span class="mut">nicht angezeigt</span>');
  $('t_net').innerHTML=h;

  drawGraph(s.graph||{nodes:[],edges:[]});
  const nodes=s.nodes||[];
  if(nodes.length){
    h='<tr><th>RLOC16</th><th>Ext. Adresse</th><th>Kinder</th><th>Funkgüte</th><th>Adresse im Mesh</th></tr>';
    for(const n of nodes){
      h+=`<tr><td class="mono">${esc(n.rloc)}</td><td class="mono">${esc(n.ext)}</td>`+
         `<td>${esc(n.children)}</td><td>${n.lq==null?na:esc(n.lq)}</td>`+
         `<td class="mono">${n.omr?esc(n.omr):na}</td></tr>`;
    }
  } else h='<tr><td class="mut">keine Knoten gemeldet</td></tr>';
  $('t_topo').innerHTML=h;

  h='';
  h+=row('Backbone', s.link==='up'?'<span class="ok">verbunden</span>':'<span class="bad">getrennt</span>');
  h+=row('Route ins Mesh', s.route?'<span class="mono">'+esc(s.route)+'</span>':'<span class="bad">fehlt</span>');
  h+=row('Mesh antwortet', s.mesh?('<span class="mono">'+esc(s.mesh)+'</span>'):'<span class="mut">noch nicht geprüft</span>',
         s.mesh_ok?'ok':(s.mesh?'bad':''));
  $('t_bb').innerHTML=h;

  $('flash').disabled=!!s.busy;
  $('backup').disabled=!!s.busy;
  const rq=(s.requests||[]).join('\n');
  $('log').textContent=(s.log||'')+(rq?'\n--- Zugriffe ---\n'+rq:'');
  const b=s.backups||[];
  $('backups').innerHTML = b.length
    ? 'Sicherungen:<br>'+b.map(f=>`${esc(f)} &middot; `+
        `<a href="${url('api/backup/download?f='+encodeURIComponent(f))}">herunterladen</a> &middot; `+
        `<a href="#" onclick="restore('${esc(f)}');return false">zur&uuml;ckspielen</a>`).join('<br>')
    : '';
}

// A live force-directed graph: edges pull, nodes push each other away, and a
// node under the pointer drags the rest of the network with it.  Written out
// here rather than pulled from a library, so the page stays self-contained.
const G={nodes:[],edges:[],by:{},sig:'',alpha:0,drag:null,frame:0};
const W=640,H=340;
const sigOf=g=>g.nodes.map(n=>n.id).sort().join(',')+'|'+
               g.edges.map(e=>e.a+'-'+e.b+e.kind).sort().join(',');

function drawGraph(g){
  const svg=$('graph');
  if(!g.nodes.length){
    svg.innerHTML='<text x="16" y="28" fill="#888" font-size="13">keine Knoten gemeldet</text>';
    G.sig=''; return;
  }
  const sig=sigOf(g);
  if(sig===G.sig){                       // same shape: only refresh the labels
    g.edges.forEach(e=>{ const old=G.edges.find(x=>x.a===e.a&&x.b===e.b);
      if(old){ old.lq_in=e.lq_in; old.lq_out=e.lq_out; old.cost=e.cost; } });
    g.nodes.forEach(n=>{ const old=G.by[n.id];
      if(old){ old.kind=n.kind; old.children=n.children; old.ext=n.ext; } });
    paint(); return;
  }
  const keep={}; G.nodes.forEach(n=>keep[n.id]={x:n.x,y:n.y});
  G.nodes=g.nodes.map((n,i)=>{
    const a=2*Math.PI*i/g.nodes.length, k=keep[n.id];
    return Object.assign({},n,{x:k?k.x:W/2+130*Math.cos(a), y:k?k.y:H/2+100*Math.sin(a),
                               vx:0, vy:0, fx:null, fy:null});
  });
  G.by={}; G.nodes.forEach(n=>G.by[n.id]=n);
  G.edges=g.edges.slice();
  G.sig=sig;
  build(svg);
  G.alpha=1; tick();
}

function build(svg){
  let out='';
  G.edges.forEach((e,i)=>{
    out+=`<line id="e${i}" stroke-linecap="round"><title></title></line>`;
    out+=`<text id="el${i}" font-size="10" fill="var(--mut)" text-anchor="middle"></text>`;
  });
  G.nodes.forEach((n,i)=>{
    const r=n.kind==='child'?8:12;
    out+=`<g id="n${i}" style="cursor:grab">`+
         (n.kind==='leader'?`<circle r="${r+4}" fill="none" stroke="var(--acc)" stroke-width="1.5"/>`:'')+
         `<circle r="${r}" fill="${n.kind==='child'?'var(--mut)':'var(--acc)'}" `+
         `stroke="var(--card)" stroke-width="2"><title></title></circle>`+
         `<text y="${r+13}" font-size="11" fill="var(--fg)" text-anchor="middle">${esc(n.rloc)}</text>`+
         `</g>`;
  });
  svg.innerHTML=out;
  G.nodes.forEach((n,i)=>{
    const el=document.getElementById('n'+i);
    n.el=el;
    el.addEventListener('pointerdown',ev=>{
      ev.preventDefault(); el.setPointerCapture(ev.pointerId);
      el.style.cursor='grabbing'; G.drag=n; G.alpha=Math.max(G.alpha,0.6); tick();
    });
    el.addEventListener('pointermove',ev=>{
      if(G.drag!==n) return;
      const pt=toSvg(ev); n.fx=pt.x; n.fy=pt.y;
      G.alpha=Math.max(G.alpha,0.6); tick();
    });
    const stop=ev=>{ if(G.drag!==n) return;
      try{ el.releasePointerCapture(ev.pointerId); }catch(_){}
      el.style.cursor='grab'; n.fx=n.fy=null; G.drag=null; };
    el.addEventListener('pointerup',stop);
    el.addEventListener('pointercancel',stop);
  });
}

function toSvg(ev){
  const svg=$('graph'), p=svg.createSVGPoint();
  p.x=ev.clientX; p.y=ev.clientY;
  return p.matrixTransform(svg.getScreenCTM().inverse());
}

function step(){
  const N=G.nodes;
  for(let i=0;i<N.length;i++) for(let j=i+1;j<N.length;j++){
    const a=N[i], b=N[j];
    let dx=a.x-b.x, dy=a.y-b.y, d=Math.hypot(dx,dy)||0.01;
    const f=3000/(d*d);
    a.vx+=dx/d*f; a.vy+=dy/d*f; b.vx-=dx/d*f; b.vy-=dy/d*f;
  }
  G.edges.forEach(e=>{
    const a=G.by[e.a], b=G.by[e.b]; if(!a||!b) return;
    let dx=b.x-a.x, dy=b.y-a.y, d=Math.hypot(dx,dy)||0.01;
    const f=(d-150)*0.06;
    a.vx+=dx/d*f; a.vy+=dy/d*f; b.vx-=dx/d*f; b.vy-=dy/d*f;
  });
  N.forEach(n=>{
    n.vx+=(W/2-n.x)*0.002; n.vy+=(H/2-n.y)*0.002;   // keep it on screen
    if(n.fx!=null){ n.x=n.fx; n.y=n.fy; n.vx=n.vy=0; return; }
    n.vx*=0.82; n.vy*=0.82;
    n.x=Math.max(30,Math.min(W-30,n.x+n.vx*G.alpha));
    n.y=Math.max(26,Math.min(H-26,n.y+n.vy*G.alpha));
  });
}

function paint(){
  G.edges.forEach((e,i)=>{
    const a=G.by[e.a], b=G.by[e.b], el=document.getElementById('e'+i),
          lb=document.getElementById('el'+i);
    if(!a||!b||!el) return;
    const lq=Math.max(e.lq_in||0,e.lq_out||0);
    el.setAttribute('x1',a.x.toFixed(1)); el.setAttribute('y1',a.y.toFixed(1));
    el.setAttribute('x2',b.x.toFixed(1)); el.setAttribute('y2',b.y.toFixed(1));
    el.setAttribute('stroke',e.kind==='child'?'var(--mut)':
      (lq>=3?'var(--ok)':lq===2?'var(--warn)':'var(--bad)'));
    el.setAttribute('stroke-width',e.kind==='child'?1.2:(lq?lq*1.4:1));
    if(e.kind==='child') el.setAttribute('stroke-dasharray','5,4');
    el.firstChild.textContent=a.rloc+' — '+b.rloc+': '+
      (e.kind==='child'?'Kind':'Funkgüte ein '+e.lq_in+', aus '+e.lq_out+', Kosten '+e.cost);
    if(lb){
      lb.setAttribute('x',((a.x+b.x)/2).toFixed(1));
      lb.setAttribute('y',((a.y+b.y)/2-5).toFixed(1));
      lb.textContent=(e.kind==='child'||!lq)?'':e.lq_in+'/'+e.lq_out;
    }
  });
  G.nodes.forEach(n=>{
    if(!n.el) return;
    n.el.setAttribute('transform','translate('+n.x.toFixed(1)+','+n.y.toFixed(1)+')');
    const c=n.el.querySelector('title');
    if(c) c.textContent=n.rloc+(n.ext?' · '+n.ext:'')+
      (n.children?' · '+n.children+' Kinder':'')+
      (n.kind==='leader'?' · Leader':'');
  });
}

function tick(){
  if(G.frame) return;
  const run=()=>{
    G.frame=0;
    step(); paint();
    if(G.drag) G.alpha=Math.max(G.alpha,0.5); else G.alpha*=0.97;
    if(G.alpha>0.02||G.drag) G.frame=requestAnimationFrame(run);
  };
  G.frame=requestAnimationFrame(run);
}

async function post(p,btn){
  btn.disabled=true; $('msg').textContent='läuft …';
  try{ const r=await (await fetch(url(p),{method:'POST'})).json(); $('msg').textContent=r.message||'fertig'; }
  catch(e){ $('msg').textContent='fehlgeschlagen: '+e; }
  btn.disabled=false; refresh();
}
$('flash').onclick=async e=>{
  const s=await (await fetch(url('api/status'))).json();
  const same=s.installed&&s.bundled&&s.installed===s.bundled;
  const q=same
    ? 'Der Stick läuft bereits mit '+s.bundled+'. Trotzdem erneut schreiben? Das Mesh pausiert etwa eine Minute.'
    : 'Firmware '+(s.bundled||'')+' auf den Stick schreiben? Das Mesh pausiert etwa eine Minute.';
  if(confirm(q)) post('api/flash'+(same?'?force=1':''),e.target);
};
$('reboot').onclick=e=>{ if(confirm('Stick neu starten?')) post('api/reboot',e.target); };
$('backup').onclick=e=>{
  if(confirm('Netzdaten des Sticks sichern? Der Backbone pausiert wenige Sekunden.\n\n'+
             'Die Datei enthält die Zugangsdaten des Thread-Netzes — entsprechend aufbewahren.'))
    post('api/backup',e.target);
};
$('clearlog').onclick=e=>post('api/log/clear',e.target);
async function restore(f){
  if(!confirm('Netzdaten aus '+f+' auf den Stick schreiben?\n\n'+
              'Der Stick uebernimmt damit das gespeicherte Thread-Netz und verliert sein '+
              'jetziges. Gedacht fuer Ersatzhardware.')) return;
  $('msg').textContent='laeuft ...';
  try{ const r=await (await fetch(url('api/restore?f='+encodeURIComponent(f)),{method:'POST'})).json();
       $('msg').textContent=r.message||'fertig'; }
  catch(e){ $('msg').textContent='fehlgeschlagen: '+e; }
  refresh();
}
refresh(); setInterval(refresh,5000);
</script></body></html>
"""


def _get(url, timeout=4.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def collect_status():
    env = CTX["env"]
    stick, port = env.get("stick", ""), env.get("info_port", 8082)
    out = {"bundled": CTX.get("bundled", ""), "release": CTX.get("release", "")}
    try:
        ver = json.loads(_get(f"http://{stick}:{port}/version"))
        out.update(chip=ver.get("chip"), idf=ver.get("idf"), mac=ver.get("mac"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        st = json.loads(_get(f"http://{stick}:{port}/status"))
        out.update(installed=st.get("fw"), br=st.get("br"), role=st.get("role"),
                   heap=st.get("heap"))
        secs = int(st.get("uptime_s", 0))
        out["uptime"] = f"{secs // 3600} h {secs % 3600 // 60} min" if secs >= 3600 \
            else f"{secs // 60} min {secs % 60} s"
        out["link"] = st.get("link")
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        bb = json.loads(_get(f"http://{stick}:{port}/backbone"))
        omr = bb.get("omr_prefix")
        CTX["omr_prefix"] = omr or ""
        if omr:
            p = subprocess.run(["ip", "-6", "route", "show", omr, "dev", env.get("tap", "tap0")],
                               capture_output=True, text=True, timeout=5)
            out["route"] = omr if p.stdout.strip() else ""
    except (urllib.error.URLError, OSError, ValueError, subprocess.SubprocessError):
        pass
    # Served from the cache the refresher fills: a mesh-wide diagnostic query
    # takes seconds, and the page asks every five.
    out["mesh_info"] = CTX.get("mesh_info", {})
    out["nodes"] = CTX.get("nodes", [])
    try:
        out["backups"] = sorted(os.listdir(CTX.get("backup_dir", "")), reverse=True)[:5]
    except OSError:
        out["backups"] = []
    try:
        with open(CTX.get("backup_res", "")) as fh:
            r = fh.read().strip()
        out["backup_result"] = r
    except OSError:
        out["backup_result"] = ""
    out["graph"] = CTX.get("graph", {"nodes": [], "edges": []})
    out["requests"] = list(CTX.get("requests", []))[-12:]
    out["mesh"] = CTX.get("mesh_text", "")
    out["mesh_ok"] = CTX.get("mesh_ok", False)
    out["busy"] = os.path.exists(CTX["req_file"])
    try:
        with open(CTX["res_file"]) as fh:
            out["flash_result"] = fh.read().strip()
    except OSError:
        out["flash_result"] = ""
    try:
        with open(CTX.get("restore_res", "")) as fh:
            out["restore_result"] = fh.read().strip()
    except OSError:
        out["restore_result"] = ""
    try:
        with open(CTX["log_path"]) as fh:
            out["log"] = "".join(fh.readlines()[-40:])
    except OSError:
        out["log"] = ""
    return out


def mesh_graph(diag):
    """Every relationship the network reports, as nodes and edges.

    Each router lists, for every other router it can hear, the link quality in
    both directions and the cost of the route — `RouteId` identifies the peer,
    whose RLOC16 is that id shifted by ten bits.  Children appear in their
    parent's ChildTable, and their RLOC16 is the parent's with the child id in
    the low bits.  That is the whole graph; nothing has to be inferred.
    """
    nodes, edges, seen = {}, [], set()
    leader_id = None
    for d in diag:
        rloc = d.get("Rloc16", 0)
        leader_id = leader_id or d.get("LeaderData", {}).get("LeaderRouterId")
        nodes[rloc] = {"rloc": f"0x{rloc:04x}", "id": rloc,
                       "ext": d.get("ExtAddress", ""), "kind": "router",
                       "children": len(d.get("ChildTable", []))}
    for d in diag:
        a = d.get("Rloc16", 0)
        own = a >> 10
        for r in d.get("Route", {}).get("RouteData", []):
            rid = r.get("RouteId")
            if rid is None or rid == own:
                continue
            b = rid << 10
            if b not in nodes:
                nodes[b] = {"rloc": f"0x{b:04x}", "id": b, "ext": "",
                            "kind": "router", "children": 0}
            key = tuple(sorted((a, b)))
            if key in seen:
                continue
            seen.add(key)
            edges.append({"a": key[0], "b": key[1], "kind": "router",
                          "lq_in": r.get("LinkQualityIn"),
                          "lq_out": r.get("LinkQualityOut"),
                          "cost": r.get("RouteCost")})
        for c in d.get("ChildTable", []):
            cid = c.get("ChildId", 0)
            child = a | cid
            nodes[child] = {"rloc": f"0x{child:04x}", "id": child, "ext": "",
                            "kind": "child", "children": 0}
            edges.append({"a": a, "b": child, "kind": "child",
                          "lq_in": None, "lq_out": None, "cost": None})
    if leader_id is not None and (leader_id << 10) in nodes:
        nodes[leader_id << 10]["kind"] = "leader"
    return {"nodes": list(nodes.values()), "edges": edges}


def mesh_details(stick):
    """Network summary and topology.  The network key is deliberately not read:
    a status page is no place for it."""
    info, nodes = {}, []
    try:
        n = json.loads(_get(f"http://{stick}/node", timeout=6))
        info["name"] = n.get("NetworkName")
        info["extpanid"] = n.get("ExtPanId")
        info["routers"] = n.get("NumOfRouter")
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        props = json.loads(_get(f"http://{stick}/get_properties", timeout=6)).get("result", {})
        info["channel"] = props.get("RCP:Channel")
        info["panid"] = props.get("Network:PANID")
        info.setdefault("name", props.get("Network:Name"))
    except (urllib.error.URLError, OSError, ValueError):
        pass
    try:
        diag = json.loads(_get(f"http://{stick}/diagnostics", timeout=15))
        CTX["graph"] = mesh_graph(diag if isinstance(diag, list) else [])
        omr_pfx = CTX.get("omr_prefix", "")
        for d in diag if isinstance(diag, list) else []:
            rloc = d.get("Rloc16", 0)
            lq = None
            for r in d.get("Route", {}).get("RouteData", []):
                if r.get("LinkQualityIn"):
                    lq = f"{r['LinkQualityIn']}/{r['LinkQualityOut']}"
                    break
            omr = ""
            for a in d.get("IP6AddressList", []):
                if omr_pfx and a.startswith(omr_pfx.split("::")[0]):
                    omr = a
                    break
            nodes.append({"rloc": f"0x{rloc:04x}", "ext": d.get("ExtAddress", ""),
                          "children": len(d.get("ChildTable", [])), "lq": lq, "omr": omr})
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return info, nodes


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Quiet in the add-on log — that belongs to the stick — but every
        # request is kept in a ring buffer the page shows, which is the only
        # way to see what a browser behind ingress actually asks for.
        pass

    def _record(self, code, size):
        entry = (f"{time.strftime('%H:%M:%S')} {self.command} {self.path[:70]} "
                 f"-> {code} ({size} B)")
        reqs = CTX.setdefault("requests", [])
        reqs.append(entry)
        del reqs[:-40]

    def _allowed(self):
        nets = CTX.get("allow")
        if nets is None:
            return True
        try:
            peer = ipaddress.ip_address(self.client_address[0].split("%")[0])
        except (ValueError, IndexError, TypeError):
            return False
        if peer.version == 6 and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        return any(peer in net for net in nets)

    def _refuse(self):
        """Say no, and say it once per source so a probe cannot fill the log."""
        peer = self.client_address[0] if self.client_address else "?"
        seen = CTX.setdefault("refused", set())
        if peer not in seen:
            seen.add(peer)
            _log(f"web access refused for {peer}: this page and its controls are "
                 "reachable through Home Assistant only.  Set the 'web_allow' "
                 "option (or THBR_WEB_ALLOW) to widen that deliberately.")
        self._send(403, "not available from here", "text/plain")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self._record(code, len(body))

    def _proxy(self, path):
        """Pass a request through to the border router's own web server."""
        env = CTX["env"]
        url = f"http://{env.get('stick', '')}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "text/html")
            self._send(200, body, ctype)
        except urllib.error.HTTPError as e:
            self._send(e.code, e.read() or b"", e.headers.get("Content-Type", "text/plain"))
        except (urllib.error.URLError, OSError) as e:
            self._send(502, f"border router unreachable: {html.escape(str(e))}", "text/plain")

    def do_GET(self):
        if not self._allowed():
            return self._refuse()
        path = self.path.split("?")[0].lstrip("/")
        if path in ("", "index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path in ("logo.png", "logo-dark.png"):
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       path), "rb") as fh:
                    return self._send(200, fh.read(), "image/png")
            except OSError:
                return self._send(404, b"", "image/png")
        if path == "api/status":
            return self._send(200, json.dumps(collect_status()))
        if path == "api/backup/download":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[-1])
            name = os.path.basename((q.get("f") or [""])[0])
            full = os.path.join(CTX.get("backup_dir", ""), name)
            if not name or not os.path.exists(full):
                return self._send(404, "not found", "text/plain")
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if path.startswith("br/") or path == "br":
            return self._proxy(path[3:])
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._allowed():
            return self._refuse()
        path = self.path.split("?")[0].lstrip("/")
        if path == "api/flash":
            force = "force" in self.path
            try:
                os.unlink(CTX["res_file"])
            except OSError:
                pass
            tmp = CTX["req_file"] + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("force" if force else "")
            os.replace(tmp, CTX["req_file"])
            return self._send(200, json.dumps(
                {"message": "flashing — this takes about a minute"}))
        if path == "api/log/clear":
            try:
                open(CTX["log_path"], "w").close()
                return self._send(200, json.dumps({"message": "Protokoll geleert"}))
            except OSError as e:
                return self._send(500, json.dumps({"message": f"fehlgeschlagen: {e}"}))
        if path == "api/restore":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[-1])
            name = os.path.basename((q.get("f") or [""])[0])
            if not name:
                return self._send(400, json.dumps({"message": "keine Datei angegeben"}))
            try:
                os.unlink(CTX["restore_res"])
            except OSError:
                pass
            tmp = CTX["restore_req"] + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(name)
            os.replace(tmp, CTX["restore_req"])
            return self._send(200, json.dumps(
                {"message": "Netzdaten werden zurueckgespielt ..."}))
        if path == "api/backup":
            try:
                os.unlink(CTX["backup_res"])
            except OSError:
                pass
            tmp = CTX["backup_req"] + ".tmp"
            with open(tmp, "w") as fh:
                fh.write("")
            os.replace(tmp, CTX["backup_req"])
            return self._send(200, json.dumps({"message": "Netzdaten werden gelesen …"}))
        if path == "api/reboot":
            env = CTX["env"]
            try:
                req = urllib.request.Request(
                    f"http://{env['stick']}:{env['info_port']}/reboot", data=b"", method="POST")
                urllib.request.urlopen(req, timeout=8).read()
                return self._send(200, json.dumps({"message": "restarting"}))
            except (urllib.error.URLError, OSError) as e:
                return self._send(502, json.dumps({"message": f"failed: {e}"}))
        self._send(404, json.dumps({"message": "not found"}))


def _refresher():
    """Keep the expensive numbers fresh in the background."""
    while True:
        try:
            info, nodes = mesh_details(CTX["env"].get("stick", ""))
            if info or nodes:
                CTX["mesh_info"], CTX["nodes"] = info, nodes
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(20)


def start(env, req_file, res_file, log_path, bundled, port, allow="ingress",
          release=""):
    CTX.update(env=env, req_file=req_file, res_file=res_file,
               log_path=log_path, bundled=bundled, allow=parse_allow(allow),
               release=release)
    threading.Thread(target=_refresher, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
