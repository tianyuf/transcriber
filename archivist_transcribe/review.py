"""A self-contained HTML proofreading app for a merged transcription.

Every review marker becomes a chip next to the page image: pick A, B or C, type a correction,
keep or remove a restored line. Decisions are saved in the browser as you go and can be
exported as the final Markdown, or as a JSON file to continue on another computer.
The interface is in English, or in Chinese for the CJK profile.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

from .sources import Source

STRINGS = {
    "en": {
        "lang": "en", "title": "proofread", "prev": "‹ Previous", "next": "Next ›",
        "acceptA": "Accept A for the rest of this page", "exportMd": "Export final Markdown",
        "exportJson": "Export decisions", "importJson": "Import decisions",
        "legendUnc": "model unsure", "legendDrop": "restored line", "zoom": "Zoom",
        "keys": "<kbd>←</kbd><kbd>→</kbd> page · <kbd>Tab</kbd> next item · <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> choose A/B/C",
        "page": "Page", "agreement": "agreement", "two": "two models", "three": "three models", "noFlags": "nothing flagged",
        "decided": "decided", "of": "of", "items": "items", "none": "(nothing)", "keep": "keep", "remove": "remove",
        "typeFix": "type the correct text", "edit": "type a correction", "cantRead": "Could not read: ",
        "status": "proofread against the page images",
    },
    "zh": {
        "lang": "zh", "title": "校对", "prev": "‹ 上一页", "next": "下一页 ›",
        "acceptA": "本页未决项全取A", "exportMd": "导出最终 Markdown",
        "exportJson": "导出决定", "importJson": "导入决定",
        "legendUnc": "模型不确定", "legendDrop": "补回的行", "zoom": "缩放",
        "keys": "<kbd>←</kbd><kbd>→</kbd> 翻页 · <kbd>Tab</kbd> 下一项 · <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> 为当前项选 A/B/C",
        "page": "第", "agreement": "一致度", "two": "两模型", "three": "三模型", "noFlags": "无标记",
        "decided": "已决定", "of": "/", "items": "项", "none": "（无）", "keep": "保留", "remove": "删去",
        "typeFix": "输入正确文字", "edit": "手动输入", "cantRead": "无法读取: ",
        "status": "已对照扫描件校对",
    },
}


def segments(merged: str, notation) -> list[dict]:
    """Split merged text into plain text and marker segments."""
    segs: list[dict] = []
    pos = 0
    for m in notation.mark_re.finditer(merged):
        if m.start() > pos:
            segs.append({"t": "text", "s": merged[pos:m.start()]})
        if m.group(1) is not None:
            segs.append({"t": "drop", "s": m.group(2), "who": m.group(1)})
        elif m.group(3) is not None:
            segs.append({"t": "conf", "opts": m.group(3).split("|")})
        elif m.group(4) is not None:
            segs.append({"t": "unc", "s": m.group(4)})
        else:
            segs.append({"t": "unc", "s": ""})  # an illegible marker
        pos = m.end()
    if pos < len(merged):
        segs.append({"t": "text", "s": merged[pos:]})
    return segs


def write_review(src: Source, checks, labels: dict[str, str], notation, out: Path) -> None:
    pages = [{
        "page": pc.page,
        "label": src.label(pc.page - 1),
        "img": src.preview(pc.page - 1),
        "segs": segments(pc.merged, notation),
        "flags": pc.flags,
        "ratio": round(pc.ratio, 3),
        "three": pc.c is not None,
    } for pc in sorted(checks, key=lambda c: c.page)]
    t = STRINGS["zh" if notation.name == "cjk" else "en"]
    data = {"stem": src.stem, "name": src.name, "labels": labels, "pages": pages, "notation": notation.js(), "T": t}
    page = (TEMPLATE.replace("__LANG__", t["lang"]).replace("__TITLE__", html.escape(src.stem))
            .replace("__TTITLE__", html.escape(t["title"]))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")))
    out.write_text(page, encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="__LANG__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · __TTITLE__</title>
<style>
:root{--bg:#f4f4f2;--panel:#fff;--ink:#1f1f1f;--muted:#6b6b6b;--line:#e3e3df;--a:#cfe3ff;--b:#ffe3a3;--c:#d9f2d0;--pick:#1f6feb;--unc:#ffc9c9;--drop:#c9f0d4}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,"Noto Sans","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC",sans-serif;font-size:14px}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0 12px 0 0;font-weight:600}
header .stat{color:var(--muted)}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 10px;cursor:pointer;color:inherit}
button:hover{border-color:#bbb}
button.primary{background:var(--pick);color:#fff;border-color:var(--pick)}
.layout{display:grid;grid-template-columns:200px minmax(0,1fr);min-height:calc(100vh - 50px)}
nav{border-right:1px solid var(--line);background:var(--panel);overflow:auto;max-height:calc(100vh - 50px);position:sticky;top:50px}
nav a{display:flex;justify-content:space-between;gap:8px;padding:6px 12px;text-decoration:none;color:inherit;border-bottom:1px solid #f0f0ee}
nav a span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
nav a.cur{background:#eef3ff}
nav .n{color:var(--muted)}
nav .n.todo{color:#b4232c;font-weight:600}
main{padding:12px 16px}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.scan{position:sticky;top:60px;align-self:start;max-height:calc(100vh - 70px);overflow:auto;background:#fff;border:1px solid var(--line);border-radius:8px}
.scan img{display:block;width:100%}
.tools{display:flex;gap:10px;align-items:center;margin-bottom:8px;flex-wrap:wrap;color:var(--muted)}
.txt{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 16px;white-space:pre-wrap;overflow-wrap:anywhere;line-height:2.1;font-size:15px}
.chip{display:inline-flex;align-items:center;flex-wrap:wrap;gap:2px;border-radius:6px;padding:0 3px;margin:0 1px;vertical-align:baseline;line-height:1.5;border:1px solid transparent}
.chip.open{background:#fff7e0;border-color:#f0d58c}
.chip.done{background:transparent}
.chip.unc.open{background:var(--unc)}
.chip.drop.open{background:var(--drop)}
.chip .opt{border:1px solid var(--line);border-radius:4px;padding:0 5px;cursor:pointer;background:#fff;font:inherit;line-height:1.5;white-space:pre-wrap}
.chip .opt.A{background:var(--a)} .chip .opt.B{background:var(--b)} .chip .opt.C{background:var(--c)}
.chip .opt.pick{outline:2px solid var(--pick);outline-offset:-1px;font-weight:600}
.chip .opt small{color:var(--muted);font-size:10px;margin-right:2px}
.chip .opt.empty{color:var(--muted);font-style:italic}
.chip.done .opt:not(.pick){display:none}
.chip.done .opt.pick{outline:none;background:#e8f0fe;border-color:#c5d6f5}
.chip.done .opt.pick.custom{background:#fff0c2}
.chip input{font:inherit;width:8em;border:1px solid var(--pick);border-radius:4px;padding:0 4px}
.chip .edit{color:var(--muted);cursor:pointer;padding:0 3px}
.chip.drop .text{max-width:40em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:bottom}
.legend span{display:inline-block;padding:0 6px;border-radius:4px;margin-right:6px;color:var(--ink)}
.flags{color:var(--muted);margin:0 0 8px}
kbd{background:#eee;border-radius:3px;padding:0 4px;font-family:inherit}
.hidden{display:none}
@media (max-width:900px){.layout{grid-template-columns:1fr}nav{position:static;max-height:160px}.cols{grid-template-columns:1fr}.scan{position:static;max-height:60vh}}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span class="stat" id="stat"></span>
  <span style="flex:1"></span>
  <button id="prev"></button><button id="next"></button>
  <button id="acceptA"></button>
  <button id="exportMd" class="primary"></button>
  <button id="exportJson"></button>
  <button type="button" id="importBtn"></button><input type="file" id="importJson" accept="application/json" class="hidden">
</header>
<div class="layout">
  <nav id="nav"></nav>
  <main>
    <div class="tools">
      <span class="legend"><span style="background:var(--a)">A</span><span style="background:var(--b)">B</span><span style="background:var(--c)">C</span><span style="background:var(--unc)" id="lgUnc"></span><span style="background:var(--drop)" id="lgDrop"></span></span>
      <span><span id="zoomLabel"></span> <input type="range" id="zoom" min="60" max="220" value="100"></span>
      <span id="keys"></span>
    </div>
    <p class="flags" id="flags"></p>
    <div class="cols">
      <div class="scan"><img id="scan" alt=""></div>
      <div class="txt" id="txt" lang="__LANG__"></div>
    </div>
  </main>
</div>
<script>
const DATA = __DATA__;
const T = DATA.T, N = DATA.notation;
const KEY = 'archivist-transcribe:' + DATA.stem;
let dec = {};
try { dec = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { dec = {}; }
function save(){ try { localStorage.setItem(KEY, JSON.stringify(dec)); } catch (e) {} }
let cur = 0, focusIdx = -1;

for (const [id, k] of [['prev','prev'],['next','next'],['acceptA','acceptA'],['exportMd','exportMd'],['exportJson','exportJson'],['importBtn','importJson'],['lgUnc','legendUnc'],['lgDrop','legendDrop'],['zoomLabel','zoom']]) document.getElementById(id).textContent = T[k];
document.getElementById('keys').innerHTML = T.keys;
document.getElementById('importBtn').onclick = () => document.getElementById('importJson').click();

function markerIds(p){ const ids=[]; p.segs.forEach((s,i)=>{ if(s.t!=='text') ids.push(i); }); return ids; }
// Decisions are keyed by page + chip content + occurrence, not by position, so they survive
// regenerating the app as long as the chip itself is unchanged.
const ID_CACHE = {};
function idOf(pi, si){
  const k = pi+':'+si; if (ID_CACHE[k]) return ID_CACHE[k];
  const p = DATA.pages[pi]; const seen = {};
  p.segs.forEach((s,i)=>{ if (s.t==='text') return;
    const key = s.t==='conf' ? s.opts.join('|') : (s.t==='drop' ? s.who+':'+s.s : 'unc:'+s.s);
    seen[key] = (seen[key]||0)+1; ID_CACHE[pi+':'+i] = 'p'+p.page+':'+key+'#'+seen[key]; });
  return ID_CACHE[k];
}
function resolved(pi, si){ return dec[idOf(pi,si)] !== undefined; }
function counts(pi){ const ids=markerIds(DATA.pages[pi]); let done=0; ids.forEach(si=>{ if(resolved(pi,si)) done++; }); return [done, ids.length]; }
function unresolved(s){
  if (s.t==='conf') return N.conf[0]+s.opts.join('|')+N.conf[1];
  if (s.t==='drop') return N.drop[0]+s.who+N.drop[1]+s.s+N.drop[2];
  return s.s ? N.unc[0]+s.s+N.unc[1] : N.ill;
}
function finalText(pi){
  const p = DATA.pages[pi]; let out='';
  p.segs.forEach((s,i)=>{ if (s.t==='text') { out += s.s; return; } const d = dec[idOf(pi,i)]; out += d ? d.text : unresolved(s); });
  return out;
}
function pageName(p){ return T.lang==='zh' ? '第 '+p.page+' 页' : p.label.charAt(0).toUpperCase()+p.label.slice(1); }

function renderNav(){
  const nav = document.getElementById('nav'); nav.innerHTML='';
  let tot=0, totDone=0;
  DATA.pages.forEach((p,i)=>{
    const [d,n]=counts(i); tot+=n; totDone+=d;
    const a=document.createElement('a'); a.href='#'; a.className = i===cur?'cur':''; a.title = p.label;
    a.innerHTML = '<span>'+esc(pageName(p))+'</span><span class="n '+(d<n?'todo':'')+'">'+(n? d+'/'+n : '·')+'</span>';
    a.onclick=(e)=>{e.preventDefault(); go(i);};
    nav.appendChild(a);
  });
  document.getElementById('stat').textContent = T.decided+' '+totDone+' '+T.of+' '+tot+' '+T.items;
}
function esc(s){ return s.replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function optButton(label, text, cls, pi, si, picked, choice){
  const b=document.createElement('button'); b.type='button'; b.className='opt '+cls+(picked?' pick':'')+(text===''?' empty':'');
  b.innerHTML = (label?'<small>'+esc(label)+'</small>':'') + (text===''?T.none:esc(text));
  b.onclick=(e)=>{ e.stopPropagation(); choose(pi,si,choice||label||cls,text); };
  return b;
}
function choose(pi, si, choice, text){ dec[idOf(pi,si)] = {choice, text}; save(); renderPage(); renderNav(); }
function unchoose(pi, si){ delete dec[idOf(pi,si)]; save(); renderPage(); renderNav(); }
function editBox(pi, si, chip, initial){
  const inp=document.createElement('input'); inp.value=initial||''; inp.placeholder=T.typeFix;
  inp.onkeydown=(e)=>{ if(e.key==='Enter'){ choose(pi,si,'custom',inp.value); } if(e.key==='Escape'){ renderPage(); } e.stopPropagation(); };
  inp.onclick=(e)=>e.stopPropagation();
  chip.appendChild(inp); inp.focus();
}
function renderPage(){
  const p = DATA.pages[cur]; const pi=cur;
  document.getElementById('scan').src = p.img;
  document.getElementById('flags').textContent = pageName(p)+' · '+T.agreement+' '+p.ratio+' · '+(p.three?T.three:T.two)+' · '+(p.flags.length?p.flags.join(', '):T.noFlags);
  const box=document.getElementById('txt'); box.innerHTML='';
  const ids = markerIds(p);
  p.segs.forEach((s,si)=>{
    if (s.t==='text'){ box.appendChild(document.createTextNode(s.s)); return; }
    const d = dec[idOf(pi,si)];
    const chip=document.createElement('span'); chip.className='chip '+s.t+(d?' done':' open'); chip.dataset.si=si; chip.tabIndex=0;
    if (ids.indexOf(si)===focusIdx) chip.style.boxShadow='0 0 0 2px var(--pick)';
    if (s.t==='conf'){
      const L=['A','B','C'];
      s.opts.forEach((o,k)=> chip.appendChild(optButton(L[k], o, L[k], pi, si, d && d.choice===L[k] && d.text===o)));
    } else if (s.t==='drop'){
      const lab=document.createElement('span'); lab.className='text'; lab.title=s.s; lab.textContent='+'+s.who+': '+s.s; chip.appendChild(lab);
      chip.appendChild(optButton(T.keep, s.s, 'A', pi, si, d && d.choice==='keep', 'keep'));
      chip.appendChild(optButton(T.remove, '', 'B', pi, si, d && d.choice==='remove', 'remove'));
    } else {
      chip.appendChild(optButton(s.s?'A':'', s.s||N.ill, 'A', pi, si, d && d.choice==='A', 'A'));
    }
    if (d && d.choice==='custom') chip.appendChild(optButton('✎', d.text, 'custom', pi, si, true));
    const ed=document.createElement('span'); ed.className='edit'; ed.textContent='✎'; ed.title=T.edit;
    ed.onclick=(e)=>{ e.stopPropagation(); ed.remove(); editBox(pi,si,chip,d?d.text:''); };
    chip.appendChild(ed);
    chip.onclick=()=>{ focusIdx=ids.indexOf(si); if (d) unchoose(pi,si); else renderPage(); };
    box.appendChild(chip);
  });
}
function go(i){ cur=Math.max(0,Math.min(DATA.pages.length-1,i)); focusIdx=-1; renderNav(); renderPage(); window.scrollTo(0,0); }
document.getElementById('prev').onclick=()=>go(cur-1);
document.getElementById('next').onclick=()=>go(cur+1);
document.getElementById('zoom').oninput=(e)=>{ document.getElementById('scan').style.width=e.target.value+'%'; };
document.getElementById('acceptA').onclick=()=>{
  const p=DATA.pages[cur];
  p.segs.forEach((s,si)=>{ if (s.t==='text' || resolved(cur,si)) return;
    if (s.t==='conf') dec[idOf(cur,si)]={choice:'A',text:s.opts[0]};
    else if (s.t==='drop') dec[idOf(cur,si)]= s.who==='BC' ? {choice:'keep',text:s.s} : {choice:'remove',text:''};
    else dec[idOf(cur,si)]={choice:'A',text:s.s||N.ill};
  });
  save(); renderPage(); renderNav();
};
function download(name, text, type){ const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([text],{type})); a.download=name; a.click(); }
document.getElementById('exportMd').onclick=()=>{
  const [d,n] = DATA.pages.reduce((acc,p,i)=>{ const c=counts(i); return [acc[0]+c[0], acc[1]+c[1]]; }, [0,0]);
  const who = Object.entries(DATA.labels).map(([k,v])=>k+': '+v).join('; ');
  let md = '---\nsource: '+JSON.stringify(DATA.name)+'\nmethod: '+JSON.stringify('machine transcription checked by model comparison ('+who+')')+
    '\nstatus: '+JSON.stringify(T.status+'; '+d+' of '+n+' review items decided')+'\nexported: '+new Date().toISOString().slice(0,10)+'\n---\n\n# '+DATA.stem+'\n';
  DATA.pages.forEach((p,i)=>{ md+='\n\n<!-- '+p.label+' -->\n\n'+finalText(i)+'\n'; });
  download(DATA.stem+'.final.md', md, 'text/markdown');
};
document.getElementById('exportJson').onclick=()=>download(DATA.stem+'.decisions.json', JSON.stringify(dec,null,1), 'application/json');
document.getElementById('importJson').onchange=(e)=>{ const f=e.target.files[0]; if(!f) return; f.text().then(t=>{ try{ Object.assign(dec, JSON.parse(t)); save(); renderPage(); renderNav(); }catch(err){ alert(T.cantRead+err); } }); };
document.addEventListener('keydown',(e)=>{
  if (e.target.tagName==='INPUT') return;
  const ids=markerIds(DATA.pages[cur]);
  if (e.key==='ArrowLeft'){ go(cur-1); }
  else if (e.key==='ArrowRight'){ go(cur+1); }
  else if (e.key==='Tab'){ e.preventDefault(); if(!ids.length) return; focusIdx=(focusIdx+(e.shiftKey?-1:1)+ids.length)%ids.length; renderPage(); const el=document.querySelector('.chip[data-si="'+ids[focusIdx]+'"]'); if(el) el.scrollIntoView({block:'center'}); }
  else if (['1','2','3'].includes(e.key) && focusIdx>=0){
    const si=ids[focusIdx]; const s=DATA.pages[cur].segs[si]; const k=+e.key-1;
    if (s.t==='conf' && k<s.opts.length) choose(cur,si,['A','B','C'][k],s.opts[k]);
    else if (s.t==='drop' && k<2) choose(cur,si,k===0?'keep':'remove',k===0?s.s:'');
    else if (s.t==='unc' && k===0) choose(cur,si,'A',s.s||N.ill);
  }
});
go(0);
</script>
</body>
</html>
"""
