/* 共用：API、SSE、Toast、格式化、地圖 */
const API = {
  async get(u){ const r = await fetch(u); if(!r.ok) throw new Error(u+' '+r.status); return r.json(); },
  async post(u, body){ const r = await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body||{})}); if(!r.ok) throw new Error(u+' '+r.status); return r.json(); }
};
function fmtP(x){ return x==null?'–':Math.round(x*100)+'%'; }
function fmtN(x,d=0){ return x==null||Number.isNaN(x)?'–':Number(x).toFixed(d); }
function hhmm(ts){ return ts? String(ts).slice(11,16):'–'; }
function el(tag, attrs={}, ...kids){ const e=document.createElement(tag); for(const [k,v] of Object.entries(attrs)){ if(k==='class') e.className=v; else if(k==='html') e.innerHTML=v; else if(k.startsWith('on')) e.addEventListener(k.slice(2),v); else e.setAttribute(k,v);} for(const k of kids){ if(k==null) continue; e.append(k.nodeType?k:document.createTextNode(k)); } return e; }
function riskColor(p){ if(p==null) return '#b0b8c4'; if(p>=0.6) return '#d64545'; if(p>=0.35) return '#e08a00'; if(p>=0.2) return '#e6c229'; return '#2fa66a'; }
function statusLabel(s){ return {normal:'正常',empty:'無車可借',full:'無位可還',both_zero:'雙零待查',stale_flat:'長期不變待查',no_data:'無資料',cap_conflict:'容量矛盾待查'}[s]||s; }
function statusBadge(s){ const c={normal:'ok',empty:'high',full:'high',both_zero:'grey',stale_flat:'grey',no_data:'grey',cap_conflict:'grey'}[s]||'grey'; return `<span class="badge ${c}">${statusLabel(s)}</span>`; }
function srcBadge(src){ if(!src) return ''; if(src==='bedrock') return '<span class="badge ai">Bedrock 生成</span>'; if(src==='template') return '<span class="badge tpl">模板文字</span>'; if(src==='hgb') return '<span class="badge real">模型預測</span>'; if(src==='baseline') return '<span class="badge tpl">基準預測</span>'; if(String(src).startsWith('mixed')) return '<span class="badge real">模型/基準混合</span>'; return `<span class="badge grey">${src}</span>`; }
const SIM = '<span class="badge sim">模擬</span>', REAL='<span class="badge real">真實計算</span>';

/* Toast（頁內即時通知；不是手機推播） */
const Toasts = {
  box: null,
  init(){ if(!this.box){ this.box = el('div',{class:'toasts'}); document.body.append(this.box); } },
  show(title, body, kind='info', meta='', ttl=9000){ this.init(); const t = el('div',{class:'toast '+kind}); t.innerHTML = `<div class="t">${title}</div><div class="b">${body||''}</div>${meta?`<div class="m">${meta}</div>`:''}`; this.box.prepend(t); while(this.box.children.length>5) this.box.lastChild.remove(); setTimeout(()=>t.remove(), ttl); return t; }
};

/* SSE，若連不上（例如經過 CDN 被緩衝）自動改成輪詢 */
function connectEvents(handlers){
  let es, fails = 0, polling = null, lastTs = null, closed = false;
  const fire = (ev)=>{ try{ (handlers[ev.type]||handlers['*']||(()=>{}))(ev.payload, ev); if(handlers.any) handlers.any(ev); }catch(e){ console.warn(e); } };
  function startPolling(){
    if(polling) return;
    console.info('即時事件改用輪詢');
    polling = setInterval(async ()=>{
      if(closed) return;
      try{ const st = await API.get('/api/state');
        if(st.clock.ts !== lastTs){ lastTs = st.clock.ts; fire({type:'tick', payload:{clock:st.clock, kpi:st.kpi}}); }
      }catch(e){}
    }, 4000);
  }
  function open(){
    if(closed) return;
    try{ es = new EventSource('/api/events'); }catch(e){ startPolling(); return; }
    let got = false;
    const guard = setTimeout(()=>{ if(!got){ try{es.close();}catch(e){} fails++; if(fails>=2) startPolling(); else open(); } }, 8000);
    es.onopen = ()=>{ got = true; clearTimeout(guard); fails = 0; if(polling){ clearInterval(polling); polling = null; } };
    es.onmessage = (m)=>{ got = true; clearTimeout(guard); try{ const ev = JSON.parse(m.data); if(ev.payload&&ev.payload.clock) lastTs = ev.payload.clock.ts; fire(ev); }catch(e){ console.warn(e); } };
    es.onerror = ()=>{ try{es.close();}catch(e){} if(closed) return; fails++; if(fails>=3){ startPolling(); } else setTimeout(open, 2000); };
  }
  open();
  return ()=>{ closed = true; if(es) try{es.close();}catch(e){} if(polling) clearInterval(polling); };
}

/* 地圖 */
function makeMap(id, center=[25.03,121.47], zoom=14, opts={}){
  const m = L.map(id,{zoomControl:true, attributionControl:true}).setView(center, zoom);
  const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19, attribution:'© OpenStreetMap contributors', className:'osm-soft'});
  const aws = L.tileLayer('/api/tiles/raster.satellite/{z}/{x}/{y}',{maxZoom:17, attribution:'衛星影像 © AWS / HERE（Amazon Location Service）'});
  osm.addTo(m);
  if(opts.layerToggle !== false) L.control.layers({'街道圖 (OpenStreetMap)': osm, 'AWS 衛星圖 (Amazon Location)': aws}, null, {position:'topright', collapsed:true}).addTo(m);
  m._osm = osm; m._aws = aws;
  return m;
}
function stationMarker(s, opts={}){
  const horizon = opts.horizon||120, layer = opts.layer||'empty';
  const p = layer==='empty'? s['pe_'+horizon] : s['pf_'+horizon];
  const pending = ['both_zero','stale_flat','no_data','cap_conflict'].includes(s.status);
  const color = pending? '#9aa4b2' : riskColor(p);
  const r = Math.max(5, Math.min(11, 4 + Math.sqrt(s.cap||10)));
  const mk = L.circleMarker([s.lat, s.lon], {radius:r, color: pending?'#6b7684':'#fff', weight:1.5, fillColor:color, fillOpacity: pending?0.55:0.9, dashArray: pending?'2 2':null});
  mk.bindTooltip(`<b>${s.name}</b><br>現況 ${s.bikes??'–'} 輛 / ${s.spaces??'–'} 格（柱 ${s.cap}）<br>${horizon} 分後 零車 ${fmtP(s['pe_'+horizon])}・零位 ${fmtP(s['pf_'+horizon])}<br>${statusLabel(s.status)}`,{direction:'top',offset:[0,-6]});
  return mk;
}
function legendControl(map, html){ const c = L.control({position:'bottomleft'}); c.onAdd=()=>{ const d=L.DomUtil.create('div','legend'); d.innerHTML=html; return d; }; c.addTo(map); return c; }
function sparkline(canvas, values, color='#0f5fa8', max=null){
  const ctx = canvas.getContext('2d'); const W = canvas.width = canvas.clientWidth*2, H = canvas.height = canvas.clientHeight*2; ctx.clearRect(0,0,W,H);
  const vals = values.map(v=>v==null?null:v); const mx = max || Math.max(1, ...vals.filter(v=>v!=null)); const n = vals.length; if(n<2) return;
  ctx.strokeStyle=color; ctx.lineWidth=3; ctx.beginPath(); let started=false;
  vals.forEach((v,i)=>{ if(v==null){ started=false; return;} const x = i/(n-1)*(W-6)+3, y = H-4 - (v/mx)*(H-8); if(!started){ ctx.moveTo(x,y); started=true;} else ctx.lineTo(x,y); }); ctx.stroke();
}
