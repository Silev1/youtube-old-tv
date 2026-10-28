#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tvyt — YouTube для старых Samsung TV (Orsay 2011-2014).
Чистый stdlib, без Flask/pip. Телевизор = плеер, телефон = пульт.
"""
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
COOKIES = os.path.join(BASE, "cookies.txt")
os.makedirs(CACHE, exist_ok=True)

PORT = int(os.environ.get("TVYT_PORT", "80"))
MAXH = int(os.environ.get("TVYT_MAXH", "480"))   # 480 по умолчанию: старый плеер + экономия
MAX_CACHE = 12 * 1024 ** 3          # 12 ГБ кэша
FMT = ("bv*[height<=%d][vcodec^=avc1]+ba[ext=m4a]"
       "/bv*[height<=%d]+ba"
       "/b[height<=%d][ext=mp4]/b") % (MAXH, MAXH, MAXH)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

LOCK = threading.Lock()
STATE = {
    "rev": 1,
    "current": None,
    "queue": [],
    "cmd": None,
    "report": {"pos": 0, "dur": 0, "paused": True, "at": 0},
}
CID = [0]
THUMBS = {}
SEARCH_CACHE = {}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ensure_faststart(path):
    """Переносим moov в начало файла: старые плееры иначе не начинают играть."""
    mark = path + ".fs"
    if os.path.exists(mark) or not os.path.exists(path):
        return
    tmp = path + ".fs.mp4"
    try:
        rc = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path,
                             "-c", "copy", "-movflags", "+faststart", tmp],
                            timeout=600).returncode
        if rc == 0 and os.path.getsize(tmp) > 1000:
            os.replace(tmp, path)
            open(mark, "w").close()
            log("faststart ok", os.path.basename(path))
        else:
            raise RuntimeError("ffmpeg rc=%d" % rc)
    except Exception as exc:
        log("faststart fail", os.path.basename(path), exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def bump():
    with LOCK:
        STATE["rev"] += 1


def fmt_len(s):
    s = int(s or 0)
    return "%d:%02d" % (s // 60, s % 60)


# ---------------------------------------------------------------- yt-dlp

def yt_search(q, n=12):
    key = q.lower().strip()
    hit = SEARCH_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    cmd = ["yt-dlp", "--flat-playlist", "-J", "--no-warnings",
           "ytsearch%d:%s" % (n, q)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    data = json.loads(out or "{}")
    res = []
    for e in (data.get("entries") or []):
        if not e.get("id"):
            continue
        res.append({
            "id": e["id"],
            "title": e.get("title") or e["id"],
            "dur": int(e.get("duration") or 0),
            "len": fmt_len(e.get("duration")),
        })
    SEARCH_CACHE[key] = (time.time(), res)
    return res


def download(vid, path):
    """Качаем в mp4 (H.264/AAC), репортим прогресс в STATE."""
    outtmpl = os.path.join(CACHE, vid + ".%(ext)s")
    cmd = ["yt-dlp", "-f", FMT, "--no-playlist", "--no-warnings",
           "--newline", "--merge-output-format", "mp4",
           "-o", outtmpl, "https://www.youtube.com/watch?v=" + vid]
    if os.path.exists(COOKIES):
        cmd[1:1] = ["--cookies", COOKIES]
    log("download", vid)
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
        tail = []
        for line in p.stdout:
            line = line.rstrip()
            m = re.search(r"\[download\]\s+([\d.]+)%", line)
            if m:
                with LOCK:
                    c = STATE["current"]
                    if c and c["id"] == vid:
                        c["progress"] = float(m.group(1))
                        STATE["rev"] += 1
            elif line.startswith("ERROR"):
                tail.append(line)
        p.wait()
        if p.returncode != 0:
            raise RuntimeError(" ".join(tail[-2:]) or "yt-dlp rc=%d" % p.returncode)
        ensure_faststart(path)
        with LOCK:
            c = STATE["current"]
            if c and c["id"] == vid:
                c.update(status="ready", progress=100, path=path)
                STATE["rev"] += 1
        log("ready", vid, os.path.getsize(path) // 1024, "KB")
        cleanup(path)
    except Exception as exc:
        log("FAIL", vid, exc)
        with LOCK:
            c = STATE["current"]
            if c and c["id"] == vid:
                c.update(status="error", error=str(exc)[:300])
                STATE["rev"] += 1


def cleanup(keep):
    try:
        files = []
        for name in os.listdir(CACHE):
            p = os.path.join(CACHE, name)
            if os.path.isfile(p) and name.endswith(".mp4") and p != keep:
                files.append((os.path.getmtime(p), p))
        total = sum(os.path.getsize(p) for _, p in files)
        for _, p in sorted(files):
            if total <= MAX_CACHE:
                break
            total -= os.path.getsize(p)
            os.unlink(p)
            log("cache rm", os.path.basename(p))
    except Exception as exc:
        log("cleanup", exc)


def play(vid, title, enqueue=False):
    path = os.path.join(CACHE, vid + ".mp4")
    item = {"id": vid, "title": title or vid,
            "status": "ready" if os.path.exists(path) else "downloading",
            "progress": 100 if os.path.exists(path) else 0,
            "path": path}
    with LOCK:
        if enqueue and STATE["current"]:
            STATE["queue"].append({"id": vid, "title": title or vid})
            STATE["rev"] += 1
            return
        STATE["current"] = item
        STATE["report"] = {"pos": 0, "dur": 0, "paused": False, "at": time.time()}
        STATE["rev"] += 1
    if item["status"] != "ready":
        threading.Thread(target=download, args=(vid, path), daemon=True).start()


def next_item():
    with LOCK:
        q = STATE["queue"]
        if not q:
            STATE["current"] = None
            STATE["rev"] += 1
            return
        nxt = q.pop(0)
        STATE["rev"] += 1
    play(nxt["id"], nxt["title"])


# ---------------------------------------------------------------- html

PHONE = r"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tvyt</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e8eaed;font:15px/1.4 -apple-system,Roboto,Arial,sans-serif}
header{position:sticky;top:0;background:#151821;padding:10px;border-bottom:1px solid #242938;z-index:5}
h1{font-size:16px;margin:0 0 8px;color:#8ab4f8;font-weight:600;letter-spacing:.3px}
form{display:flex;gap:8px}
input{flex:1;min-width:0;padding:11px 12px;border-radius:10px;border:1px solid #2b3243;background:#0b0d12;color:#fff;font-size:16px}
button{padding:11px 16px;border-radius:10px;border:0;background:#3b82f6;color:#fff;font-size:15px;font-weight:600}
button.ghost{background:#232937}
#now{padding:10px;background:#151821;border-bottom:1px solid #242938;font-size:13px;color:#9aa4b8}
#now b{color:#fff;font-weight:600}
#ctl{display:flex;gap:8px;margin-top:8px}
#ctl button{flex:1;padding:10px;font-size:14px}
#list{padding:10px;display:grid;gap:10px}
.card{display:flex;gap:10px;background:#151821;border-radius:12px;padding:8px;align-items:center}
.card img{width:112px;height:63px;border-radius:8px;background:#000;flex:none;object-fit:cover}
.meta{min-width:0;flex:1}
.t{font-size:14px;line-height:1.25;max-height:3.6em;overflow:hidden;margin-bottom:4px}
.d{font-size:12px;color:#8b95a8}
.act{display:flex;flex-direction:column;gap:6px}
.act button{padding:8px 10px;font-size:13px;border-radius:8px}
.act .q{background:#232937}
.e{padding:20px;color:#f28b82}
</style></head><body>
<header>
  <h1>YouTube → телевизор</h1>
  <form onsubmit="go(event)"><input id="q" placeholder="Что ищем?" autocomplete="off"><button>Найти</button></form>
</header>
<div id="now">Ничего не играет</div>
<div id="list"></div>
<script>
var NOW=null;
function el(i){return document.getElementById(i)}
function req(u,d,cb){var x=new XMLHttpRequest();x.open(d?'POST':'GET',u,true);
  if(d)x.setRequestHeader('Content-Type','application/json');
  x.onload=function(){try{cb(JSON.parse(x.responseText))}catch(e){cb(null)}};
  x.onerror=function(){cb(null)};x.send(d?JSON.stringify(d):null)}
function go(e){e.preventDefault();var q=el('q').value.trim();if(!q)return;
  el('list').innerHTML='<div class=d>Ищу…</div>';
  req('/api/search?q='+encodeURIComponent(q),null,function(r){
    if(!r||!r.items){el('list').innerHTML='<div class=e>yt-dlp не смог получить результаты</div>';return}
    el('list').innerHTML='';
    r.items.forEach(function(it){
      var c=document.createElement('div');c.className='card';
      c.innerHTML='<img src="/api/thumb?id='+it.id+'" loading="lazy">'+
        '<div class="meta"><div class="t">'+esc(it.title)+'</div><div class="d">'+it.len+'</div></div>'+
        '<div class="act"><button>▶ Играть</button><button class="q">+ В очередь</button></div>';
      var b=c.getElementsByTagName('button');
      b[0].onclick=function(){play(it,false)};
      b[1].onclick=function(){play(it,true)};
      el('list').appendChild(c);
    });
  })}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
function play(it,enq){req('/api/play',{id:it.id,title:it.title,next:enq},function(){tick()})}
function cmd(a,v){req('/api/cmd',{action:a,value:v||0},function(){tick()})}
function tick(){req('/api/state',null,function(s){
    if(!s)return;NOW=s;
    var h='';
    if(s.current){
      var st=s.current.status;
      if(st==='downloading')h='<b>Загрузка '+Math.round(s.current.progress)+'%</b> — '+esc(s.current.title);
      else if(st==='error')h='<b style="color:#f28b82">Ошибка</b> — '+esc(s.current.error||'');
      else {
        var p=s.report&&s.report.pos?s.report.pos:0;
        h='<b>Играет</b> — '+esc(s.current.title)+' · '+toClock(p);
      }
    } else h='Ничего не играет';
    if(s.queue&&s.queue.length)h+=' <span style="color:#5f6b80">| в очереди: '+s.queue.length+'</span>';
    el('now').innerHTML=h;
  })}
function toClock(t){t=Math.floor(t);return Math.floor(t/60)+':'+('0'+(t%60)).slice(-2)}
setInterval(tick,2000);tick();
</script>
<div style="position:fixed;left:0;right:0;bottom:0;background:#151821;border-top:1px solid #242938;padding:10px">
  <div id="ctl">
    <button onclick="cmd('prev')">⏮</button>
    <button onclick="cmd('pause')">⏯</button>
    <button onclick="cmd('next')">⏭</button>
  </div>
</div>
<div style="height:76px"></div>
</body></html>
"""

TVL = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>tvyt</title>
<style>
html,body{margin:0;padding:0;height:100%;background:#000;color:#fff;overflow:hidden;
  font-family:Arial,Helvetica,sans-serif}
#m{position:absolute;left:0;right:0;top:0;bottom:0;text-align:center}
#in{position:absolute;left:0;right:0;top:50%;margin-top:-150px}
#big{font-size:80px;color:#3b82f6;line-height:1}
#t1{font-size:34px;padding:18px 60px 6px;color:#fff}
#t2{font-size:20px;color:#6b7280;padding:0 60px;line-height:1.4}
#bar{position:absolute;left:0;right:0;bottom:0;height:6px;background:#141a26;display:none}
#bar div{height:6px;width:0;background:#3b82f6}
</style></head><body>
<div id="m"><div id="in">
  <div id="big">&#9654;</div>
  <div id="t1">tvyt</div>
  <div id="t2">Открой на телефоне 192.168.0.50 и выбери видео</div>
</div></div>
<div id="bar"><div></div></div>
<script>
function el(i){return document.getElementById(i)}
function ck(n){var a=document.cookie.split(';');for(var i=0;i<a.length;i++){
  var s=a[i].replace(/^\s+/,'');if(s.indexOf(n+'=')===0)return s.substring(n.length+1);}return ''}
function setc(n,v){document.cookie=n+'='+v+';path=/;max-age=2592000'}
function xhr(u,cb){var x=new XMLHttpRequest();x.open('GET',u,true);
  x.onload=function(){try{cb(JSON.parse(x.responseText))}catch(e){cb(null)}};
  x.onerror=function(){cb(null)};x.send(null)}
function show(a,b){el('t1').innerHTML=a;el('t2').innerHTML=b||'';el('bar').style.display='none'}
function prog(p){el('t1').innerHTML='Загрузка';el('t2').innerHTML=p+'%';
  el('bar').style.display='block';el('bar').firstChild.style.width=p+'%'}
function poll(){
  xhr('/api/state',function(s){
    if(!s){setTimeout(poll,2000);return}
    var c=s.current;
    if(!c){show('tvyt','Открой на телефоне 192.168.0.50 и выбери видео')}
    else if(c.status==='error'){show('Ошибка',String(c.error||'').substring(0,140))}
    else if(c.status==='downloading'){prog(Math.round(c.progress))}
    else if(c.status==='ready'){
      if(c.id===ck('last')){show('Готово','Нажми Return и выбери другое видео')}
      else{setc('last',c.id);show('Запуск…',c.title||'');
        location.href='/stream/'+c.id+'.mp4';return}
    }
    setTimeout(poll,1000);
  });
}
poll();
</script></body></html>
"""

MIN = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>tvyt</title>
<style>
html,body{margin:0;padding:0;background:transparent;overflow:hidden}
video{display:block;background:transparent}
#m{position:absolute;left:60px;top:60px;color:#fff;font-family:Arial,Helvetica,sans-serif;
  font-size:26px;line-height:1.45;background:rgba(0,0,0,0.55);padding:16px 24px;display:none}
</style></head><body>
<video id="v" preload="auto" x5-playsinline webkit-playsinline></video>
<div id="m"></div>
<script>
var v=document.getElementById('v'),m=document.getElementById('m');
var curId='',fsTried=0,started=0;

function post(o){try{var x=new XMLHttpRequest();x.open('POST','/api/log',true);
  x.setRequestHeader('Content-Type','application/json');x.send(JSON.stringify(o));}catch(e){}}
function get(u,cb){var x=new XMLHttpRequest();x.open('GET',u,true);
  x.onload=function(){try{cb(JSON.parse(x.responseText))}catch(e){cb(null)}};
  x.onerror=function(){cb(null)};x.send(null)}
function say(t){m.style.display='block';m.innerHTML=t}
function hide(){m.style.display='none'}
function size(){
  var w=window.innerWidth||document.documentElement.clientWidth||1280;
  var h=window.innerHeight||document.documentElement.clientHeight||720;
  try{v.style.width=w+'px';v.style.height=h+'px';}catch(e){}
  return [w,h];
}
function geo(){
  var r=null;
  try{r=v.getBoundingClientRect()}catch(e){}
  return {ow:v.offsetWidth,oh:v.offsetHeight,ol:v.offsetLeft,ot:v.offsetTop,
          gw:r?Math.round(r.width):-1,gh:r?Math.round(r.height):-1};
}
function tryFS(){
  if(fsTried)return;fsTried=1;
  var ok='',no='';
  try{ if(v.webkitEnterFullscreen){v.webkitEnterFullscreen();ok+='A';}else{no+='A';} }catch(e){no+='a';}
  try{ if(v.webkitRequestFullscreen){v.webkitRequestFullscreen();ok+='B';}else{no+='B';} }catch(e){no+='b';}
  try{ if(document.documentElement.webkitRequestFullScreen){document.documentElement.webkitRequestFullScreen();ok+='C';}else{no+='C';} }catch(e){no+='c';}
  post({kind:'fs',ok:ok,no:no,iw:window.innerWidth,ih:window.innerHeight});
}
v.onloadedmetadata=function(){post({kind:'meta',vw:v.videoWidth,vh:v.videoHeight});
  post({kind:'geo',stage:'meta',g:geo()});};
v.onplaying=function(){started=1;hide();tryFS();
  post({kind:'playing',t:Math.round(v.currentTime*10)/10,g:geo()});};
v.onerror=function(){post({kind:'err',code:v.error?v.error.code:0,
  src:(v.currentSrc||'').split('/').pop()});
  say('Ошибка плеера '+(v.error?v.error.code:'?'));};
v.onended=function(){post({kind:'ended'})};

function poll(){
  get('/api/state',function(s){
    if(!s){setTimeout(poll,2000);return}
    var c=s.current;
    if(!c){curId='';if(!started){say('tvyt<br>открой на телефоне 192.168.0.50')}}
    else if(c.status==='error'){say('Ошибка загрузки<br>'+String(c.error||'').substring(0,110))}
    else if(c.status==='downloading'){say('Загрузка '+Math.round(c.progress)+'%')}
    else if(c.status==='ready'&&c.id!==curId){
      curId=c.id;started=0;fsTried=0;
      say('Запуск...');
      size();
      v.src='/stream/'+c.id+'.mp4';
      try{v.play()}catch(e){}
      post({kind:'load',id:c.id,g:geo()});
      setTimeout(function(){ if(!started){say('Нажми OK на пульте');} },3000);
    }
    setTimeout(poll,1000);
  });
}
setInterval(function(){ if(curId){post({kind:'hb',t:Math.round(v.currentTime*10)/10,
  p:v.paused?1:0,rs:v.readyState,vw:v.videoWidth||0});} },4000);
document.onkeydown=function(e){
  var k=e.keyCode||e.which;
  if(k===13||k===415||k===179){ if(v.paused){try{v.play()}catch(x){}}else{v.pause()} return false; }
  if(k===37){v.currentTime=Math.max(0,v.currentTime-10);return false;}
  if(k===39){v.currentTime=v.currentTime+10;return false;}
  return false;
};
size();
poll();
</script></body></html>
"""

TV = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>tvyt</title>
<style>
html,body{margin:0;padding:0;height:100%;background:#000;color:#fff;overflow:hidden;
  font-family:Arial,Helvetica,sans-serif}
video{position:absolute;left:0;top:0;width:100%;height:100%;background:#000}
#cover{position:absolute;left:0;top:0;width:100%;height:100%;background:rgba(0,0,0,0.72);
  text-align:center;display:none}
#cover .box{position:absolute;left:0;right:0;top:50%;margin-top:-120px}
#big{font-size:64px;color:#3b82f6}
#h1{font-size:30px;color:#fff;padding:20px 80px;line-height:1.3}
#h2{font-size:22px;color:#8b95a8;padding:10px 80px}
#prog{font-size:30px;color:#8ab4f8;padding:20px}
#bar{position:absolute;left:0;right:0;bottom:0;height:8px;background:#1a1f2b;display:none}
#bar div{height:8px;background:#3b82f6;width:0}
#tip{position:absolute;left:0;right:0;bottom:18px;text-align:center;font-size:20px;
  color:#6b7280;display:none}
</style></head><body>
<video id="v" preload="auto" x5-playsinline webkit-playsinline></video>
<div id="cover"><div class="box">
  <div id="big">▶</div>
  <div id="h1">tvyt</div>
  <div id="h2">Открой на телефоне адрес этого сервера и выбери видео</div>
  <div id="prog"></div>
</div></div>
<div id="bar"><div></div></div>
<div id="tip">OK — пауза · ←/→ — 10 сек · ↑ — следующее</div>
<script>
var v=document.getElementById('v'),cover=document.getElementById('cover'),
    h1=document.getElementById('h1'),h2=document.getElementById('h2'),
    prog=document.getElementById('prog'),bar=document.getElementById('bar').firstChild,
    tip=document.getElementById('tip');
var rev=-1,curId=null,lastCmd=0,tries=0,hideTip=0,started=false;

function xhr(u,d,cb){var x=new XMLHttpRequest();x.open(d?'POST':'GET',u,true);
  if(d)x.setRequestHeader('Content-Type','application/json');
  x.onload=function(){try{cb(JSON.parse(x.responseText))}catch(e){}};
  x.onerror=function(){};
  x.send(d?JSON.stringify(d):null);}

function elog(o){try{xhr('/api/log',o,function(){})}catch(e){}}
function fit(){
  var w=window.innerWidth||document.documentElement.clientWidth||1280;
  var h=window.innerHeight||document.documentElement.clientHeight||720;
  try{v.style.width=w+'px';v.style.height=h+'px';v.style.left='0px';v.style.top='0px';}catch(e){}
  elog({kind:'fit',w:w,h:h,dw:document.documentElement.clientWidth,
        dh:document.documentElement.clientHeight});
}
var EVS=['loadstart','loadedmetadata','loadeddata','canplay','canplaythrough',
         'playing','pause','ended','error','stalled','waiting','seeking','suspend'];
for(var ei=0;ei<EVS.length;ei++){(function(n){
  v.addEventListener(n,function(){
    elog({kind:'vid',e:n,t:Math.round((v.currentTime||0)*10)/10,
      vw:v.videoWidth||0,vh:v.videoHeight||0,rs:v.readyState,ns:v.networkState,
      er:v.error?v.error.code:0,src:(v.currentSrc||'').split('/').pop()});
  },false);
})(EVS[ei]);}
setInterval(function(){ if(curId) elog({kind:'hb',t:Math.round((v.currentTime||0)*10)/10,
  p:v.paused?1:0,rs:v.readyState,vw:v.videoWidth||0}); },3000);
fit();
v.onresize=fit;

function show(title,sub,busy){
  cover.style.display='block';h1.innerHTML=title;h2.innerHTML=sub||'';
  prog.innerHTML=busy?busy:'';bar.style.display='none';
}
function hide(){cover.style.display='none';}

function load(item){
  curId=item.id;started=false;
  if(item.status==='downloading'){show('Загрузка…',esc(item.title),'0%');return;}
  if(item.status==='error'){show('Не удалось',esc(item.error||''),'');return;}
  show('Запуск…',esc(item.title),'');
  v.src='/stream/'+item.id+'.mp4';
  v.load();
  playNow();
}
function playNow(){
  try{v.play();}catch(e){}
  setTimeout(function(){ if(v.paused){ show('▶','Нажмите OK на пульте','') ; } },900);
}
function esc(s){return String(s).replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]})}

function poll(){
  xhr('/api/state',null,function(s){
    if(!s){setTimeout(poll,2000);return;}
    if(s.rev!=rev){rev=s.rev;apply(s);}
    if(s.cmd&&s.cmd.cid!=lastCmd){lastCmd=s.cmd.cid;doCmd(s.cmd);}
    setTimeout(poll,1000);
  });
}
function apply(s){
  var c=s.current;
  if(!c){curId=null;v.pause();v.removeAttribute('src');v.load();
    show('tvyt','Открой на телефоне адрес этого сервера и выбери видео','');return;}
  if(c.id!==curId){load(c);return;}
  if(c.status==='downloading'){show('Загрузка…',esc(c.title),Math.round(c.progress)+'%');return;}
  if(c.status==='error'){show('Не удалось',esc(c.error||''),'');return;}
  if(cover.style.display!=='none'&&!started){hide();if(v.paused)playNow();}
}
function doCmd(c){
  if(c.action==='pause'){if(v.paused)v.play();else v.pause();return;}
  if(c.action==='seek'){v.currentTime=Math.max(0,(v.currentTime||0)+c.value);return;}
  if(c.action==='stop'){v.pause();v.currentTime=0;return;}
}
function report(ended){
  started=true;
  xhr('/api/report',{pos:v.currentTime||0,dur:v.duration||0,
    paused:v.paused,ended:ended?1:0},function(){});
}
v.onplaying=function(){started=true;hide();bar.style.display='block';};
v.onended=function(){report(true);};
v.onerror=function(){ if(!curId)return;
  var c=(v.error&&v.error.code)||'?';
  show('Ошибка плеера '+c,'файл '+(v.currentSrc||'').split('/').pop(),'');
  xhr('/api/log',{kind:'vidErr',code:String(c),src:v.currentSrc||''},function(){});
};
setInterval(function(){ if(curId&&!v.paused){report(false);} },5000);
setInterval(function(){ if(v.duration){bar.style.width=(100*v.currentTime/v.duration)+'%';} },500);
setInterval(function(){ if(v.currentTime>0&&!v.paused&&cover.style.display!=='none'){hide();} },400);

document.onkeydown=function(e){
  var k=e.keyCode||e.which;
  if(k===13||k===415||k===179){ if(v.paused)v.play();else v.pause(); return false; }
  if(k===37){ v.currentTime=Math.max(0,(v.currentTime||0)-10); return false; }
  if(k===39){ v.currentTime=(v.currentTime||0)+10; return false; }
  if(k===38){ report(true); return false; }
  if(k===40){ report(true); return false; }
  if(k===461||k===8){ return true; }
  return true;
};
show('tvyt','Открой на телефоне адрес этого сервера и выбери видео','');

function diag(extra){
  var probe=null,t='',hv=0;
  try{probe=document.createElement('video');}catch(e){}
  try{if(probe)t=probe.canPlayType('video/mp4; codecs="avc1.4D401E,mp4a.40.2"')||'';}catch(e){t='err';}
  try{hv=probe&&probe.play?1:0;}catch(e){}
  xhr('/api/log',{kind:'hello',ua:navigator.userAgent,mp4:t,hasVideo:hv,
    href:location.href,extra:extra||''},function(){});
}
window.onerror=function(m,u,l){
  m=String(m);
  if(m.indexOf('MostVisited')>=0||m.indexOf('IsolatedUI')>=0)return;
  xhr('/api/log',{kind:'jserr',m:m,l:String(l),u:String(u)},function(){});
};
diag();
poll();
</script>
</body></html>
"""


# ---------------------------------------------------------------- http

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tvyt/1.0"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers
    def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False),
                  "application/json; charset=utf-8")

    def body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- routes
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)

        if path != "/api/state":
            log("GET", path[:70],
                "ip=" + self.client_address[0],
                "ua=" + (self.headers.get("User-Agent") or "")[:60],
                "rng=" + (self.headers.get("Range") or "")[:24])

        if path in ("/", "/index.html"):
            return self.send(200, PHONE, "text/html; charset=utf-8")
        if path in ("/tv", "/tv/"):
            return self.send(200, MIN, "text/html; charset=utf-8")
        if path in ("/tv3", "/tv3/"):
            return self.send(200, TV, "text/html; charset=utf-8")
        if path in ("/tv2", "/tv2/"):
            return self.send(200, TVL, "text/html; charset=utf-8")
        if path == "/api/state":
            with LOCK:
                return self.send_json(STATE)
        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            if not q:
                return self.send_json({"items": []})
            try:
                return self.send_json({"items": yt_search(q)})
            except Exception as exc:
                log("search fail", exc)
                return self.send_json({"items": [], "error": str(exc)[:200]}, 500)
        if path == "/api/thumb":
            vid = (qs.get("id") or [""])[0]
            return self.thumb(vid)
        if path.startswith("/stream/"):
            vid = path[len("/stream/"):].split(".")[0]
            f = os.path.join(CACHE, vid + ".mp4")
            if not os.path.exists(f):
                return self.send(404, "not ready")
            return self.stream(f)
        if path == "/favicon.ico":
            return self.send(204, b"", "image/x-icon")
        return self.send(404, "nope")

    def do_HEAD(self):
        u = urllib.parse.urlparse(self.path)
        if u.path.startswith("/stream/"):
            vid = u.path[len("/stream/"):].split(".")[0]
            f = os.path.join(CACHE, vid + ".mp4")
            if os.path.exists(f):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(os.path.getsize(f)))
                self.end_headers()
                return
        self.send(404, "nope")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        d = self.body_json()
        if u.path == "/api/log":
            log("TVLOG", json.dumps(d, ensure_ascii=False)[:1200])
            return self.send_json({"ok": True})
        if u.path == "/api/play":
            play(str(d.get("id") or ""), d.get("title") or "",
                 bool(d.get("next")))
            return self.send_json({"ok": True})
        if u.path == "/api/cmd":
            act = d.get("action") or ""
            if act in ("next", "prev"):
                threading.Thread(target=next_item, daemon=True).start()
                return self.send_json({"ok": True})
            CID[0] += 1
            with LOCK:
                STATE["cmd"] = {"cid": CID[0],
                                "action": d.get("action") or "",
                                "value": float(d.get("value") or 0)}
                STATE["rev"] += 1
            return self.send_json({"ok": True, "cid": CID[0]})
        if u.path == "/api/report":
            with LOCK:
                STATE["report"] = {"pos": float(d.get("pos") or 0),
                                   "dur": float(d.get("dur") or 0),
                                   "paused": bool(d.get("paused")),
                                   "at": time.time()}
            if d.get("ended"):
                log("ended -> next")
                threading.Thread(target=next_item, daemon=True).start()
            return self.send_json({"ok": True})
        return self.send_json({"error": "nope"}, 404)

    # ---- pieces
    def thumb(self, vid):
        if not vid or not re.match(r"^[\w-]{6,20}$", vid):
            return self.send(404, "no")
        hit = THUMBS.get(vid)
        if hit is None:
            data = None
            for name in ("mqdefault.jpg", "hqdefault.jpg", "default.jpg"):
                try:
                    rq = urllib.request.Request(
                        "https://i.ytimg.com/vi/%s/%s" % (vid, name),
                        headers={"User-Agent": UA})
                    with urllib.request.urlopen(rq, timeout=8) as r:
                        data = r.read()
                    if len(data) > 1000:
                        break
                except Exception:
                    data = None
            hit = data or b""
            THUMBS[vid] = hit
            if len(THUMBS) > 400:
                THUMBS.clear()
        if not hit:
            return self.send(404, "no")
        self.send(200, hit, "image/jpeg",
                  {"Cache-Control": "max-age=86400"})

    def stream(self, path):
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        ctype = "video/mp4"
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                if start > end:
                    start = 0
        length = end - start + 1
        code = 206 if rng else 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range",
                             "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass


def main():
    def warm():
        for name in os.listdir(CACHE):
            if name.endswith(".mp4"):
                ensure_faststart(os.path.join(CACHE, name))
    threading.Thread(target=warm, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    srv.daemon_threads = True
    log("tvyt listening on :%d, cache=%s" % (PORT, CACHE))
    srv.serve_forever()


if __name__ == "__main__":
    main()
