/* 微笑單車調度中心 — 即時作戰室
   資料來源分三類：A=我們的資料算出來、B=公開資料引用、C=情境假設，對照 docs/DATA_SOURCES.md。
   全市編制 40 車／350 人為 B 類；職務拆分、位置訊號、現場回報內容為 C 類，介面上標示。 */
const $ = id => document.getElementById(id);
const FOCUS = ['板橋區', '新莊區', '土城區'];
const HANDOVER = 15;                 // C5 趟次間整備（分）
const CO2_TRUCK = 0.35;              // C10 調度車 kg CO2e/km
const MOVER_PER_GROUP = 2;           // C7 人力調度每組人數
const LAB = { gap_summary: '區域缺口', planned: '已排定', dispatched: '出車中', en_route: '行進中', done: '完成',
  too_late: '來不及', needs_cross_district: '需跨區', minor_gap: '缺口小', superseded: '已取代', cancelled: '取消' };
const ISSUES = [
  { k: 'dock_blocked', t: '車柱被占用', asset: 'dock', ticket: true },
  { k: 'construction', t: '站點施工', asset: 'station', ticket: true },
  { k: 'broken_bike', t: '車輛故障待回收', asset: 'bike', ticket: true },
  { k: 'stuck', t: '車輛卡樁', asset: 'bike', ticket: true },
  { k: 'no_parking', t: '現場無法停車', asset: null, ticket: false },
  { k: 'traffic', t: '交通壅塞', asset: null, ticket: false },
  { k: 'weather', t: '天候中斷', asset: null, ticket: false }];
const CREW_ST = { on: '在勤', meal: '用餐', rest: '休息', off: '下班' };
const TK_FLOW = ['reported', 'accepted', 'on_site', 'recovered', 'verified', 'closed'];
const TK_LABEL = { reported: '已受理', accepted: '班組接單', on_site: '現場檢查', recovered: '已處理', verified: '驗收復役', closed: '結案' };
const TK_NEXT = { reported: 'accepted', accepted: 'on_site', on_site: 'recovered', recovered: 'verified', verified: 'closed' };
const ASSET_TXT = { bike: '車輛', dock: '車柱', station: '站端系統', unknown: '待判定' };
const ESCALATE = { cross_district: '跨區支援', accept_delay: '接受延誤並通知', divert_only: '無可派資源，改民眾分流' };
const SVC = { unknown: '資料不足以判定', nominal: '觀測未見中斷', degraded: '觀測到服務中斷中', restored: '曾中斷，已觀測到恢復' };
const SVC_CLASS = { unknown: 'grey', nominal: 'ok', degraded: 'high', restored: 'info' };
const SVC_FLAG = { handled_not_restored: '已處理但站點服務仍中斷', restored_not_closed: '服務已恢復但工單未結案' };
function svcBadge(t) {
  const st = t.service_state || 'unknown';
  return `<span class="badge ${SVC_CLASS[st]}">${SVC[st]}${t.service_stale ? '（資料中斷）' : ''}</span>`;
}
const SERVICE_TARGET_MIN = 30;   // 主管訪談輸入：空站約 30 分鐘可接受（非官方 SLA，見 docs/DIRECTION_REVIEW_2026-09-12.md）
const HAV = (a, b) => { const R = 6371000, r = Math.PI / 180;
  const x = (b[0] - a[0]) * r, y = (b[1] - a[1]) * r * Math.cos((a[0] + b[0]) / 2 * r);
  return Math.sqrt(x * x + y * y) * R; };

const S = { base: null, stations: [], tasks: [], A: null, tickets: [], flow: [], cycle: null, clock: null, scen: '',
  view: 'ops', dist: '', sel: null, alloc: {}, manual: [], reports: [], actuals: {}, crewState: {},
  routes: {}, sched: null, briefs: {}, mseq: 0, alerts: [], leftTab: 'tasks', peak: 'am' };

const T = s => s ? new Date(String(s).replace(' ', 'T')).getTime() : 0;
const NOW = () => S.clock ? T(S.clock.ts) : Date.now();
const MIN = ms => Math.round(ms / 60000);
const hm = ms => { const d = new Date(ms); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
const cap = () => S.A ? S.A.truck_capacity : 20;
const short = d => String(d).replace('區', '');
const perStopDue = () => S.tasks.some(t => (t.stops || []).some(x => x.due_ts));   // 伺服器是否已載入逐站服務時限的排程器
const coverPct = () => (S.base && S.base.city ? S.base.city.neighbor_cover_am_pct : null);   // A12：由 05_verify_public.py 算出後寫進 ops_baseline.json

/* ---------------- 編制：全市公開數字 → 三區配置 ---------------- */
function baseDistrict(d) { return (S.base ? S.base.districts : []).find(x => x.district === d) || {}; }
function initAlloc() {
  if (Object.keys(S.alloc).length) return;
  FOCUS.forEach(d => { S.alloc[d] = baseDistrict(d)['trucks_' + S.peak] || 1; });
}
function crewsOf(d) { return S.alloc[d] || 0; }
function moverGroups(d) { return Math.max(1, Math.round((baseDistrict(d).movers || 0) / MOVER_PER_GROUP)); }
function repairGroups(d) { return Math.max(1, Math.round((baseDistrict(d).repair || 0) / MOVER_PER_GROUP)); }
function repairCrewIds(d) { return [...Array(repairGroups(d))].map((_, i) => `${short(d)}維修${i + 1}組`); }
function allRepairCrews() { return FOCUS.flatMap(repairCrewIds); }
function crewBusy(id) { return S.tickets.filter(t => t.crew === id && !['closed', 'verified'].includes(t.status)).length; }
function crewId(d, n) { return `${short(d)}${n}車`; }
function crewStatus(id) { return S.crewState[id] || 'on'; }
function setCrew(id, v) { S.crewState[id] = v; render(); log(`${id} 狀態改為 ${CREW_ST[v]}`, 'crew'); }

/* ---------------- 任務池 ---------------- */
function truckTrips() { return S.tasks.filter(t => t.stops.length > 0 && ['planned', 'dispatched', 'en_route'].includes(t.status)); }
function moving(t) { return t.status === 'dispatched' || t.status === 'en_route'; }
function moverTasks() { return S.manual.filter(m => m.status !== 'done' && m.status !== 'cancelled'); }
function repairTasks() { return S.tickets.filter(t => t.status !== 'closed'); }

/* ---------------- 排班：把每一趟指派到在勤車組 ---------------- */
function prepMin(first) { const A = S.A || {}; return first ? (A.lead_prepare_min ?? 15) : (A.divert_prepare_min ?? 3); }
function scheduleDistrict(trips, crewIds, now) {
  const veh = crewIds.map(id => ({ id, free: now, trips: [] }));
  const out = { veh, late: 0, lateLoad: 0, onTime: 0, busy: 0 };
  const sorted = trips.slice().sort((a, b) => (moving(a) ? 0 : 1) - (moving(b) ? 0 : 1) || T(a.depart_by) - T(b.depart_by));
  for (const t of sorted) {
    if (!veh.length) { out.late++; out.lateLoad += t.load; continue; }
    const v = veh.reduce((a, b) => b.free < a.free ? b : a);
    const first = v.trips.length === 0;                       // 這一趟是不是該組今天的第一趟
    const prep = prepMin(first) * 60000;
    const start = moving(t) ? Math.min(T(t.depart_by), now) : Math.max(v.free + (first ? prep : 0), now + (first ? prep : 0));
    const end = start + t.route_minutes * 60000, late = start > T(t.depart_by) + 60000;
    v.trips.push({ t, start, end, late, slack: MIN(T(t.depart_by) - start), first, prep: prepMin(first) });
    v.free = end + HANDOVER * 60000; out.busy += t.route_minutes + HANDOVER;
    if (late) { out.late++; out.lateLoad += t.load; } else out.onTime++;
  }
  return out;
}
function activeCrews(d) {
  const ids = []; for (let i = 1; i <= crewsOf(d); i++) { const id = crewId(d, i); if (crewStatus(id) === 'on') ids.push(id); }
  return ids;
}
function schedule() {
  const now = NOW(), byD = {}; const all = [];
  for (const d of FOCUS) {
    const r = scheduleDistrict(truckTrips().filter(t => t.district === d), activeCrews(d), now);
    byD[d] = r; r.veh.forEach(v => v.trips.forEach(x => all.push({ ...x, district: d, crew: v.id })));
  }
  S.sched = { byD, all, now };
}
function schedOf(id) { return (S.sched ? S.sched.all : []).find(x => x.t.id === id); }
function needFor(d) {
  const trips = truckTrips().filter(t => t.district === d); if (!trips.length) return 0;
  for (let n = 1; n <= 12; n++) {
    const ids = [...Array(n)].map((_, i) => crewId(d, i + 1));
    if (scheduleDistrict(trips, ids, NOW()).late === 0) return n;
  }
  return null;
}
function bump(d, k) { S.alloc[d] = Math.max(0, Math.min(12, (S.alloc[d] || 0) + k)); render(); }
function resetAlloc() { S.alloc = {}; initAlloc(); render(); Toasts.show('已回到依資料配置', FOCUS.map(d => `${short(d)} ${S.alloc[d]} 車`).join('・') + `　依 ${S.peak === 'am' ? '早' : '晚'}尖峰缺口權重分配全市 40 車`, 'task'); }

/* ---------------- 回報 ---------------- */
function log(text, kind, extra) {
  S.reports.unshift({ ts: S.clock ? S.clock.ts.slice(11, 16) : '--:--', text, kind, ...(extra || {}) });
  S.reports = S.reports.slice(0, 120);
}
function actual(tid) { return S.actuals[tid] || (S.actuals[tid] = {}); }
function reportArrive(tid, i) {
  const t = findTask(tid), s = t.stops[i], a = actual(tid);
  a[i] = { ...(a[i] || {}), arrived: NOW() };
  log(`${schedOf(tid) ? schedOf(tid).crew : tid} 抵達 ${s.name}`, 'progress', { tid, i }); render();
}
function reportDone(tid, i, qty) {
  const t = findTask(tid), s = t.stops[i], a = actual(tid);
  const q = Math.max(0, Math.min(99, Number(qty)));
  a[i] = { ...(a[i] || {}), done: NOW(), qty: q };
  const diff = q - s.qty;
  log(`${schedOf(tid) ? schedOf(tid).crew : tid} 於 ${s.name} ${s.action === 'pickup' ? '取' : '送'} ${q}/${s.qty} 輛` + (diff ? `（${diff > 0 ? '多' : '短少'} ${Math.abs(diff)}）` : ''), diff ? 'diff' : 'progress', { tid, i });
  render();
}
function openIssue(tid, i, key) {
  const t = findTask(tid), s = t.stops[i], def = ISSUES.find(x => x.k === key);
  if (!def) return;
  const needAsset = !!def.ticket;
  showModal(`回報現場異常｜${s.name}`, `
    <div class="small">類別：<b>${def.t}</b>${needAsset ? `　會開維修工單（${{ bike: '車輛', dock: '車柱', station: '站端系統' }[def.asset]}）` : '　屬作業狀況，不開維修工單'}</div>
    ${needAsset ? `<div class="xs muted" style="margin:6px 0">工單去重靠資產識別：<b>車號優先，其次柱號</b>，都沒有才退回「站點＋問題類別」。沒填的話，同一站不同設備的故障會被合併成一張單，維修派不出去。</div>
    <div class="row wrap" style="gap:6px;margin-top:4px">
      <label class="xs">車號 <input id="iss_bike" placeholder="例 YB-12345" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:120px"></label>
      <label class="xs">柱號 <input id="iss_dock" placeholder="例 07" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:70px"></label>
      <label class="xs">錯誤碼 <input id="iss_err" placeholder="車機顯示" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:110px"></label>
    </div>` : ''}
    <label class="xs" style="display:block;margin-top:8px">現場備註 <input id="iss_note" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:100%"></label>
    <div id="iss_err_msg" class="xs" style="color:var(--text-danger,#d64545);margin-top:6px"></div>
    <div style="text-align:right;margin-top:10px"><button class="small" onclick="closeModal()">取消</button>
      <button class="small primary" onclick="submitIssue('${tid}',${i},'${key}')">送出回報</button></div>`);
}
async function submitIssue(tid, i, key) {
  const t = findTask(tid), s = t.stops[i], a = actual(tid), def = ISSUES.find(x => x.k === key);
  const v = id => (($(id) || {}).value || '').trim();
  const bike = v('iss_bike'), dock = v('iss_dock'), err = v('iss_err'), note = v('iss_note');
  if (def.ticket && def.asset === 'bike' && !bike && !dock) {
    $('iss_err_msg').textContent = '車輛類異常請至少填車號或柱號，否則工單只能靠站點去重，會跟其他設備的故障合併。'; return;
  }
  const crew = schedOf(tid) ? schedOf(tid).crew : tid;
  a[i] = { ...(a[i] || {}), issue: key, issue_ts: NOW(), bike_no: bike, dock_id: dock, error_code: err, note };
  log(`${crew} 於 ${s.name} 回報「${def.t}」${bike ? `・車號 ${bike}` : ''}${dock ? `・${dock} 號柱` : ''}${err ? `・錯誤碼 ${err}` : ''}`, 'issue', { tid, i, key });
  closeModal(); render();
  if (!def.ticket || !(s.sid > 0)) return;
  try {
    const tk = await API.post('/api/ops/tickets', { sid: s.sid, issue: `調度員現場回報：${def.t}`, note: note || `由 ${crew} 於派工途中回報`,
      bike_no: bike, dock_id: dock, error_code: err, asset_type: def.asset, source: 'ops' });
    Toasts.show(tk.merged ? `併入既有工單 ${tk.id}` : `已開維修工單 ${tk.id}`,
      `${s.name}｜${def.t}${bike ? `（車號 ${bike}）` : dock ? `（${dock} 號柱）` : ''}${tk.merged ? `・第 ${tk.reports} 次回報` : ''}`, 'warn');
    loadTickets();
  } catch (e) {
    Toasts.show('工單建立失敗', '伺服器可能尚未載入派車端端點（需重啟一次）', 'warn');
  }
}
function findTask(id) { return S.tasks.find(t => t.id === id) || S.manual.find(t => t.id === id); }

/* ---------------- 人力調度任務（就近補車，不出動貨車） ---------------- */
function nearbyStock(s) {
  let best = 0, n = 0;
  for (const o of S.stations) {
    if (o.sid === s.sid || o.lat == null) continue;
    if (Math.abs(o.lat - s.lat) > 0.006) continue;
    if (HAV([s.lat, s.lon], [o.lat, o.lon]) > 500) continue;
    n++; if ((o.bikes || 0) > best) best = o.bikes || 0;
  }
  return { neighbors: n, best };
}
function emptyStations() {
  const list = S.stations.filter(s => FOCUS.includes(s.district) && (!S.dist || s.district === S.dist)
    && ['normal', 'empty', 'full'].includes(s.status) && (s.bikes === 0 || (s.pe_60 != null && s.pe_60 >= 0.6)))
    .sort((a, b) => (b.pe_60 || 0) - (a.pe_60 || 0)).slice(0, 40);
  list.forEach(s => { const k = nearbyStock(s); s._nb = k.neighbors; s._nbBest = k.best; s._isolated = k.best < 3; });
  return list.sort((a, b) => (b._isolated - a._isolated) || ((b.bikes === 0) - (a.bikes === 0)) || (b.pe_60 - a.pe_60));
}
async function openMover(sid) {
  const d = await API.get('/api/station/' + sid);
  const st = d.station, nbs = (d.neighbors || []).filter(n => n.dist_m <= 500 && n.bikes >= 3).slice(0, 5);
  const body = `<b>${st.name}</b> <span class="pill">${statusLabel(st.status)}</span>
    <div class="small" style="margin-top:4px">現況 ${st.bikes ?? '–'} 輛／柱 ${st.cap}・60 分後零車機率 ${fmtP(st.pe_60)}</div>
    <div class="xs muted" style="margin:8px 0 4px">500 公尺內還有車的站${coverPct() ? `（早尖峰有 ${coverPct()}% 的缺車站符合這個條件，所以第一手段是人力就近補，不是出動貨車）` : ''}</div>
    ${nbs.length ? nbs.map(n => `<div class="row between" style="padding:5px 0;border-bottom:1px solid var(--line)">
        <span class="small">${n.name}<br><span class="xs muted">${Math.round(n.dist_m)} m・步行 ${Math.round(n.walk_min)} 分・現有 ${n.bikes} 輛</span></span>
        <span class="row"><input type="number" id="mq${n.sid}" value="${Math.min(5, Math.max(1, Math.floor((n.bikes - 3) / 2) || 1))}" min="1" max="10" style="width:52px;font:inherit;padding:2px 4px;border:1px solid var(--line);border-radius:6px">
        <button class="small primary" onclick="createMover(${sid},${n.sid})">派人力</button></span></div>`).join('')
      : '<div class="muted small">500 公尺內沒有可供給的鄰站，需改由車組處理或跨區支援。</div>'}`;
  showModal('派人力調度員就近補車', body);
  S._mover = { to: st, nbs };
}
function coordOf(sid, fallback) {
  const s = S.stations.find(x => x.sid === sid);
  return s && s.lat != null ? { lat: s.lat, lon: s.lon } : (fallback && fallback.lat != null ? { lat: fallback.lat, lon: fallback.lon } : null);
}
function createMover(toSid, fromSid) {
  const n = S._mover.nbs.find(x => x.sid === fromSid), to = S._mover.to;
  const cf = coordOf(fromSid, n), ct = coordOf(toSid, to);
  if (!cf || !ct) { Toasts.show('無法建立人力任務', '取不到站點座標，請重新整理後再試', 'warn'); return; }
  const qty = Math.max(1, Number(($('mq' + fromSid) || {}).value || 2));
  const ride = Math.max(3, Math.round(n.dist_m / 1000 / 12 * 60));         // 騎乘 12 km/h（與 planner 同）
  const mins = ride * 2 + 2 * qty;                                          // 來回＋每輛 2 分
  S.mseq++;
  const id = 'M' + String(S.mseq).padStart(3, '0');
  const group = `${short(to.district)}人力${(S.mseq % moverGroups(to.district)) + 1}組`;
  S.manual.push({ id, kind: 'mover', district: to.district, group, from: { sid: n.sid, name: n.name, lat: cf.lat, lon: cf.lon, bikes: n.bikes },
    to: { sid: to.sid, name: to.name, lat: ct.lat, lon: ct.lon, bikes: to.bikes, cap: to.cap, pe_60: to.pe_60 },
    qty, dist_m: Math.round(n.dist_m), minutes: mins, created: S.clock.ts, status: 'assigned',
    reason: `${to.name} 60 分後零車機率 ${fmtP(to.pe_60)}，由 ${Math.round(n.dist_m)} m 外的 ${n.name}（現有 ${n.bikes} 輛）騎 ${qty} 輛過來，不出動貨車。` });
  closeModal(); log(`指派 ${group}：${n.name} → ${to.name} 騎乘補 ${qty} 輛`, 'mover', { tid: id });
  S.sel = id; render();
  Toasts.show('已派人力調度 ' + id, `${group}｜${n.name} → ${to.name}・${qty} 輛・約 ${mins} 分`, 'task');
}
function moverAct(id, act) {
  const m = S.manual.find(x => x.id === id); if (!m) return;
  if (act === 'start') { m.status = 'moving'; m.started = NOW(); log(`${m.group} 出發前往 ${m.from.name}`, 'progress', { tid: id }); }
  if (act === 'done') { m.status = 'done'; m.finished = NOW(); log(`${m.group} 完成 ${m.from.name} → ${m.to.name} 補 ${m.qty} 輛`, 'progress', { tid: id }); }
  if (act === 'cancel') { m.status = 'cancelled'; log(`${m.group} 任務 ${id} 取消`, 'issue', { tid: id }); }
  render();
}

/* ---------------- 位置（模擬訊號） ---------------- */
function truckPos(x) {
  const geo = S.routes[x.t.id];
  const frac = Math.max(0, Math.min(1, (NOW() - x.start) / Math.max(1, x.end - x.start)));
  if (geo && geo.length > 1) { const i = Math.min(geo.length - 1, Math.floor(frac * (geo.length - 1))); return geo[i]; }
  const pts = [x.t.depot, ...x.t.stops.map(s => [s.lat, s.lon])];
  const i = Math.min(pts.length - 1, Math.floor(frac * (pts.length - 1)));
  return pts[i];
}

/* ---------------- 全市盤面 ---------------- */
function rateColor(r) { return r == null ? '#c3cad4' : r < 88 ? '#d64545' : r < 90 ? '#e08a00' : r < 93 ? '#e6c229' : '#2fa66a'; }
function liveByDistrict() {
  const m = {};
  S.stations.forEach(s => {
    if (!['normal', 'empty', 'full'].includes(s.status)) return;
    const o = m[s.district] || (m[s.district] = { n: 0, a: 0, k: 0 });
    o.n++; if (s.bikes > 0) o.a++; if (s.spaces > 0) o.k++;
  });
  Object.values(m).forEach(o => { o.avail = 100 * o.a / o.n; o.dock = 100 * o.k / o.n; });
  return m;
}
function renderCity() {
  if (!S.base) return;
  const cb = $('cityBadge');
  if (cb && S.base.city) cb.textContent = `見車率與缺口由 ${S.base.data_window} 的歷史快照算出`;
  const live = liveByDistrict(), pk = S.peak;
  const ds = S.base.districts.slice().sort((a, b) => (b['deficit_' + pk] || 0) - (a['deficit_' + pk] || 0));
  $('cityCells').innerHTML = ds.map(b => {
    const r = b['avail_' + pk], lv = live[b.district], t = b['trucks_' + pk];
    const foc = FOCUS.includes(b.district);
    return `<div class="cell ${foc ? 'foc' : ''} ${S.dist === b.district ? 'on' : ''}" onclick="cityClick('${b.district}')"
      title="${b.district}｜站 ${b.stations}｜六月${pk === 'am' ? '早' : '晚'}尖峰見車率 ${r ?? '–'}%｜缺口 ${b['deficit_' + pk]} 輛｜配車 ${t}">
      <div class="nm">${short(b.district)}</div>
      <div class="rt" style="color:${rateColor(r)}">${r == null ? '–' : r.toFixed(0)}<span class="pc">%</span></div>
      <div class="bar"><i style="width:${Math.max(2, Math.min(100, (b['deficit_' + pk] / 260) * 100))}%"></i></div>
      <div class="tk">${t ? '🚚' + t : '<span class="muted">–</span>'}${lv ? `<span class="lv" style="color:${rateColor(lv.avail)}">現 ${lv.avail.toFixed(0)}%</span>` : ''}</div></div>`;
  }).join('');
  const below = ds.filter(b => b['below_target_' + pk]);
  const f = S.base.focus;
  $('citySum').innerHTML = `全市 <b>${S.base.public.dispatch_trucks}</b> 車／<b>${S.base.public.dispatch_staff}</b> 人
    <span class="xs muted">（公開數字）</span>　依${pk === 'am' ? '早' : '晚'}尖峰缺口權重分配到 ${ds.filter(b => b['trucks_' + pk] > 0).length} 個有缺口的區<br>
    <b style="color:var(--bad)">${below.length}</b> 區${pk === 'am' ? '早' : '晚'}尖峰見車率低於合約 90%：${below.slice(0, 6).map(b => short(b.district) + ' ' + b['avail_' + pk].toFixed(1) + '%').join('、')}${below.length > 6 ? ' 等' : ''}<br>
    <span class="xs muted">本系統納管 ${f.districts.map(short).join('／')} 共 ${f.stations} 站（全市 ${f.station_share_pct}%），但吃掉 ${f['deficit_share_' + pk + '_pct']}% 的尖峰缺口 → 配 ${f['trucks_by_deficit_share_' + pk]} 車（若按站數比例只有 ${f.trucks_by_station_share} 車）</span>`;
}
function cityClick(d) { if (FOCUS.includes(d)) { S.dist = S.dist === d ? '' : d; render(); } else showDistrict(d); }
function showDistrict(d) {
  const b = baseDistrict(d), lv = liveByDistrict()[d];
  showModal(d + '｜未納管區', `<table>
    <tr><th>指標</th><th>早尖峰</th><th>晚尖峰</th></tr>
    <tr><td>見車率（2026 年 6 月平均）</td><td>${b.avail_am ?? '–'}%</td><td>${b.avail_pm ?? '–'}%</td></tr>
    <tr><td>見位率</td><td>${b.dock_am ?? '–'}%</td><td>${b.dock_pm ?? '–'}%</td></tr>
    <tr><td>缺口（輛）</td><td>${b.deficit_am ?? 0}</td><td>${b.deficit_pm ?? 0}</td></tr>
    <tr><td>同時段可抽出（輛）</td><td>${b.surplus_am ?? 0}</td><td>${b.surplus_pm ?? 0}</td></tr>
    <tr><td>依缺口權重應配車</td><td>${b.trucks_am ?? 0}</td><td>${b.trucks_pm ?? 0}</td></tr></table>
    <div class="small" style="margin-top:8px">站數 ${b.stations}・回放當下見車率 ${lv ? lv.avail.toFixed(1) + '%' : '–'}</div>
    <div class="xs muted" style="margin-top:6px">此區目前不在本系統的即時排程範圍（demo 只納管 ${FOCUS.map(short).join('／')} 三區），數字來自六個月歷史資料與全市 40 車的權重換算。</div>`);
}

/* ---------------- KPI（營運視角） ---------------- */
function renderKpis() {
  const sc = S.sched, now = NOW();
  const crewsOn = FOCUS.reduce((s, d) => s + activeCrews(d).length, 0);
  const crewsAll = FOCUS.reduce((s, d) => s + crewsOf(d), 0);
  const late = FOCUS.reduce((s, d) => s + sc.byD[d].late, 0);
  const trips = truckTrips().filter(t => !S.dist || t.district === S.dist);
  const mv = moverTasks().filter(m => !S.dist || m.district === S.dist);
  const rp = repairTasks();
  let planned = 0, doneQty = 0, shortfall = 0, issues = 0, pending = 0;
  trips.forEach(t => { const a = S.actuals[t.id] || {};
    t.stops.forEach((s, i) => { planned += s.qty; const r = a[i];
      if (r && r.qty != null) { doneQty += r.qty; if (r.qty < s.qty) shortfall += s.qty - r.qty; }
      if (r && r.issue) issues++;
      if (schedOf(t.id) && moving(t) && !(r && r.done) && s.eta_min_from_depart <= MIN(now - schedOf(t.id).start)) pending++; }); });
  const nexts = sc.all.filter(x => !moving(x.t)).sort((a, b) => T(a.t.depart_by) - T(b.t.depart_by))[0];
  const cd = nexts ? MIN(T(nexts.t.depart_by) - now) : null;
  const km = sc.all.reduce((s, x) => s + (x.t.route_km || 0), 0);
  const co2 = km * CO2_TRUCK;
  const moverQty = mv.reduce((s, m) => s + m.qty, 0);
  const k = (v, l, s2, c) => `<div class="kpi"><div class="v" style="color:${c || 'inherit'}">${v}</div><div class="l">${l}</div>${s2 ? `<div class="s">${s2}</div>` : ''}</div>`;
  $('kpis').innerHTML =
      k(`${crewsOn}<span style="font-size:13px;color:var(--muted)">/${crewsAll}</span>`, '在勤車組', FOCUS.map(d => `${short(d)} ${activeCrews(d).length}`).join('・'), crewsOn < crewsAll ? 'var(--warn)' : '')
    + k(trips.length + ' <span style="font-size:12px">趟</span>', '車組任務', late ? `${late} 趟趕不上最遲出發` : '全數可如期', late ? 'var(--bad)' : 'var(--ok)')
    + k(mv.length + ' <span style="font-size:12px">件</span>', '人力就近補車', `${moverQty} 輛・零碳排・不占貨車`, 'var(--brand2)')
    + k(rp.length + ' <span style="font-size:12px">件</span>', '維修派工', rp.length ? '民眾回報與現場回報' : '目前沒有待處理', rp.length ? 'var(--sim)' : '')
    + k(`${doneQty}<span style="font-size:13px;color:var(--muted)">/${planned}</span>`, '今日已回報搬運', shortfall ? `短少 ${shortfall} 輛未補` : '與計畫一致', shortfall ? 'var(--warn)' : '')
    + k(pending + ' <span style="font-size:12px">站</span>', '應到未回報', pending ? '已過預計抵達時間' : '沒有逾時未回報', pending ? 'var(--bad)' : 'var(--ok)')
    + k(nexts ? hm(T(nexts.t.depart_by)) : '–', '下一趟最遲出發', nexts ? `倒數 ${cd} 分・${nexts.crew}` : '無待出車任務', cd != null && cd <= 30 ? 'var(--bad)' : 'var(--brand)')
    + k(co2.toFixed(1) + ' <span style="font-size:12px">kg</span>', '今日調度車碳排', `${km.toFixed(1)} km × ${CO2_TRUCK} kg/km・人力任務 0`, 'var(--muted)');
  $('fleetTotal').textContent = `${crewsAll} 車`;
}

/* ---------------- 時間軸（車組／人力／維修三個分道） ---------------- */
function ganttWin() {
  let t0 = NOW() - 20 * 60000, t1 = NOW() + 150 * 60000;
  (S.sched ? S.sched.all : []).forEach(x => t1 = Math.max(t1, x.end + 20 * 60000));
  moverTasks().forEach(m => t1 = Math.max(t1, (m.started || NOW()) + m.minutes * 60000 + 10 * 60000));
  S.tasks.filter(t => ['planned', 'dispatched', 'en_route', 'too_late'].includes(t.status) && T(t.target_ts) >= NOW()).forEach(t => t1 = Math.max(t1, T(t.target_ts) + 20 * 60000));
  return [t0, Math.min(t1, t0 + 9 * 3600000)];
}
function renderGantt() {
  const [t0, t1] = ganttWin(), W = t1 - t0, pos = t => Math.max(0, Math.min(100, (t - t0) / W * 100));
  const stepM = W > 5 * 3600000 ? 60 : 30; let ticks = '', grid = '';
  for (let t = Math.ceil(t0 / (stepM * 60000)) * stepM * 60000; t < t1; t += stepM * 60000) {
    ticks += `<span class="gtick" style="left:${pos(t)}%">${hm(t)}</span>`; grid += `<span class="ggrid" style="left:${pos(t)}%"></span>`;
  }
  const nowBar = `<div class="gnow" style="left:${pos(NOW())}%"></div>`;
  let html = `<div class="grow ghead"><div class="glabel"></div><div class="gtrack" style="overflow:visible">${ticks}<div class="gnow" style="left:${pos(NOW())}%"><b>現在 ${hm(NOW())}</b></div></div></div>`;

  for (const d of FOCUS) {
    if (S.dist && S.dist !== d) continue;
    const r = S.sched.byD[d], trips = truckTrips().filter(t => t.district === d), need = needFor(d);
    html += `<div class="gdistrict"><span class="dot truck"></span>${d}
      <span class="step"><button onclick="bump('${d}',-1)">−</button><span>${crewsOf(d)} 車</span><button onclick="bump('${d}',1)">＋</button></span>
      <span class="sum">${trips.length} 趟・${trips.reduce((s, t) => s + t.load, 0)} 輛${r.late ? `・<b style="color:var(--bad)">${r.late} 趟延誤</b>${need ? `，需 ${need} 車` : ''}` : '・全數可如期'}
      ・人力 ${moverGroups(d)} 組／維修 ${repairGroups(d)} 組</span></div>`;
    const idle = [];
    for (let i = 1; i <= crewsOf(d); i++) {
      const id = crewId(d, i), st = crewStatus(id), v = r.veh.find(x => x.id === id);
      const trs = v ? v.trips : [];
      if (!trs.length) { idle.push({ id, st }); continue; }
      const load = trs.reduce((s, x) => s + x.t.load, 0), stops = trs.reduce((s, x) => s + x.t.stops.length, 0);
      let bars = '';
      trs.forEach(x => {
        const l = pos(x.start), w = Math.max(1.2, pos(x.end) - l);
        const a = S.actuals[x.t.id] || {}, doneN = Object.values(a).filter(y => y.done).length, iss = Object.values(a).some(y => y.issue);
        bars += `<div class="gbar ${x.late ? 'late' : x.t.status} ${S.sel === x.t.id ? 'sel' : ''}" style="left:${l}%;width:${w}%" onclick="sel('${x.t.id}')"
          title="${x.t.id}｜${hm(x.start)}→${hm(x.end)}　載 ${x.t.load} 輛・${x.t.stops.length} 站・已回報 ${doneN} 站${x.late ? `　延誤 ${-x.slack} 分` : `　餘裕 ${x.slack} 分`}">${w > 7 ? `${x.t.id}${x.t.event_related ? ' 🎤' : ''} ${x.t.load}輛${iss ? ' ⚠' : ''}` : ''}</div>`;
        bars += `<div class="gdl" style="left:${pos(T(x.t.depart_by))}%" title="最遲出發 ${hm(T(x.t.depart_by))}"></div>`;
      });
      html += `<div class="grow"><div class="glabel"><span class="dot truck"></span>${id}
        <select class="cst" onchange="setCrew('${id}',this.value)">${Object.entries(CREW_ST).map(([k2, v2]) => `<option value="${k2}" ${st === k2 ? 'selected' : ''}>${v2}</option>`).join('')}</select>
        <span class="xs muted vn">${trs.length} 趟／${load} 輛／${stops} 站</span></div>
        <div class="gtrack ${st !== 'on' ? 'off' : ''}">${grid}${bars}${nowBar}</div></div>`;
    }
    if (idle.length) {
      const onCnt = idle.filter(x => x.st === 'on').length;
      html += `<div class="grow"><div class="glabel muted">待命 ${idle.length} 車</div><div class="gtrack idle">${grid}
        <div class="idlebar">${onCnt} 車在勤待命${idle.length - onCnt ? `・${idle.length - onCnt} 車非在勤` : ''}　可支援突發或其他區
        <button class="small" style="height:14px;line-height:1;padding:0 6px;font-size:9px" onclick="openRoster('${d}')">人員面板</button>
        </div>${nowBar}</div></div>`;
    }
  }
  const mv = moverTasks().filter(m => !S.dist || m.district === S.dist);
  if (mv.length) {
    html += `<div class="gdistrict"><span class="dot mover"></span>人力調度（騎乘就近補車，零碳排）<span class="sum">${mv.length} 件・${mv.reduce((s, m) => s + m.qty, 0)} 輛</span></div>`;
    mv.forEach(m => {
      const s0 = m.started || NOW(), l = pos(s0), w = Math.max(1.2, pos(s0 + m.minutes * 60000) - l);
      html += `<div class="grow"><div class="glabel"><span class="dot mover"></span>${m.group}<span class="xs muted vn">${m.qty} 輛</span></div>
        <div class="gtrack">${grid}<div class="gbar mover ${S.sel === m.id ? 'sel' : ''}" style="left:${l}%;width:${w}%" onclick="sel('${m.id}')"
          title="${m.id}｜${m.from.name} → ${m.to.name}・${m.qty} 輛・約 ${m.minutes} 分">${w > 7 ? `${m.id} ${m.qty}輛` : ''}</div>${nowBar}</div></div>`;
    });
  }
  const rp = repairTasks();
  if (rp.length) {
    html += `<div class="gdistrict"><span class="dot repair"></span>維修派工<span class="sum">${rp.length} 件處理中</span></div>`;
    rp.slice(0, 6).forEach(t => {
      const s0 = T(t.ts), l = pos(s0), w = Math.max(1.2, pos(Math.max(NOW(), s0 + 30 * 60000)) - l);
      html += `<div class="grow"><div class="glabel"><span class="dot repair"></span>${t.id}<span class="xs muted vn">${t.station.slice(0, 6)}</span></div>
        <div class="gtrack">${grid}<div class="gbar repair ${S.sel === t.id ? 'sel' : ''}" style="left:${l}%;width:${w}%" onclick="sel('${t.id}')" title="${t.station}｜${t.issue}">${w > 7 ? `${t.id} ${t.issue.slice(0, 6)}` : ''}</div>${nowBar}</div></div>`;
    });
  }
  const nofit = S.tasks.filter(t => ['too_late', 'needs_cross_district', 'minor_gap'].includes(t.status) && T(t.target_ts) >= NOW() && (!S.dist || t.district === S.dist));
  if (nofit.length) {
    html += `<div class="grow" style="margin-top:6px"><div class="glabel" style="color:var(--bad)">⚠ 無法派車</div><div class="gtrack">${grid}`
      + nofit.slice(0, 10).map(t => `<div class="gchip" style="left:${pos(T(t.target_ts))}%;transform:translateX(-50%)" onclick="sel('${t.id}')" title="${t.reason}">${short(t.district)} ${t.id}</div>`).join('')
      + `${nowBar}</div></div>`;
  }
  $('gantt').innerHTML = html;

  const late = FOCUS.reduce((s, d) => s + S.sched.byD[d].late, 0), lateLoad = FOCUS.reduce((s, d) => s + S.sched.byD[d].lateLoad, 0);
  const fix = FOCUS.filter(d => S.sched.byD[d].late > 0).map(d => { const n = needFor(d); return n ? `${short(d)}再加 ${n - activeCrews(d).length} 車` : `${short(d)}加車仍不足，須改分流`; });
  const spare = FOCUS.filter(d => { const n = needFor(d); return n !== null && activeCrews(d).length > n; }).map(d => `${short(d)}多 ${activeCrews(d).length - needFor(d)} 車`);
  $('verdict').innerHTML = late
    ? `以目前在勤車組：<b class="bad">${late} 趟趕不上目標時間</b>，合計 <b class="bad">${lateLoad} 輛</b>無法如期到位。建議 ${fix.join('、')}${spare.length ? `；${spare.join('、')}可支援` : ''}。<span class="xs muted">${perStopDue() ? `最遲出發＝每個來得及的送車站各自回推後取最緊的一個；趕不上自己時限的站會在派工單上標紅。` : `最遲出發只以第一個送車站回推，逐站服務時限需重啟伺服器後生效。`}車輛連續作業、趟次間整備 ${HANDOVER} 分為情境假設。</span>`
    : `以目前在勤車組：<b class="ok">所有趟次都能在目標時間前到位</b>${spare.length ? `，且 ${spare.join('、')}可支援其他區或留作突發備援` : ''}。<span class="xs muted">${perStopDue() ? `「如期」＝每個送車站都趕得上<b>自己</b>的服務時限；個別站趕不上會在派工單上標紅。` : `「如期」目前只判定第一個送車站，逐站服務時限需重啟伺服器後生效。`}車輛連續作業、趟次間整備 ${HANDOVER} 分為情境假設。</span>`;
}

/* ---------------- 左欄 ---------------- */
function renderTabs() {
  const tabs = [['tasks', '調度任務', truckTrips().length], ['movers', '人力調度', moverTasks().length],
    ['repair', '維修派工', repairTasks().length], ['empty', '缺車站', emptyStations().length], ['log', '回報流', S.reports.length]];
  $('tabs').innerHTML = tabs.map(([k, l, n]) => `<button class="${S.leftTab === k ? 'on' : ''}" onclick="S.leftTab='${k}';render()">${l} <span class="pill">${n}</span></button>`).join('');
}
function cdText(t) {
  const x = schedOf(t.id), m = MIN(T(t.depart_by) - NOW());
  if (moving(t)) return `<span class="cd">執行中</span>`;
  if (x && x.late) return `<span class="cd urgent">延誤 ${-x.slack} 分</span>`;
  if (m < 0) return `<span class="cd urgent">已逾時 ${-m} 分</span>`;
  return `<span class="cd ${m <= 30 ? 'urgent' : (m <= 60 ? 'soon' : '')}">${m} 分後出發</span>`;
}
function taskCard(t) {
  const x = schedOf(t.id), util = Math.round(t.load / cap() * 100);
  const a = S.actuals[t.id] || {}, doneN = Object.values(a).filter(y => y.done).length, iss = Object.values(a).filter(y => y.issue).length;
  const pk = t.stops.filter(s => s.action === 'pickup').length, dp = t.stops.filter(s => s.action === 'dropoff').length;
  return `<div class="task ${x && x.late ? 'latecar' : t.status} ${S.sel === t.id ? 'sel' : ''}" onclick="sel('${t.id}')">
    <div class="hd"><b>${t.id}・${short(t.district)}${t.event_related ? ' 🎤' : ''}</b>${t.stops.length ? cdText(t) : `<span class="pill">${LAB[t.status] || t.status}</span>`}</div>
    ${t.stops.length ? `<div class="sub">${x ? `🚚 ${x.crew}　` : ''}${hm(x ? x.start : T(t.depart_by))} 出發 → ${hm((x ? x.start : T(t.depart_by)) + t.route_minutes * 60000)} 回場</div>
      <div class="rs">${pk} 取 ${dp} 送・載 ${t.load}/${cap()} 輛・${t.route_km} km／${t.route_minutes} 分・已回報 ${doneN}/${t.stops.length} 站${iss ? `・<b style="color:var(--bad)">${iss} 異常</b>` : ''}</div>
      <div class="bar"><i style="width:${Math.round(100 * doneN / t.stops.length)}%"></i></div>`
    : `<div class="sub">${t.status === 'gap_summary' ? `缺口 ${t.deficit_total} 輛・派車補 ${t.covered}・剩餘 ${t.remaining}` : (t.deficit_total ? `缺口 ${t.deficit_total} 輛` : '缺口不足 1 輛')}</div>
       <div class="rs">${String(t.reason).replace('缺口僅 0 輛', '缺口不足 1 輛')}</div>`}</div>`;
}
function moverCard(m) {
  const lab = { assigned: '待出發', moving: '執行中', done: '完成', cancelled: '取消' }[m.status];
  return `<div class="task mover ${S.sel === m.id ? 'sel' : ''}" onclick="sel('${m.id}')">
    <div class="hd"><b>${m.id}・${m.group}</b><span class="pill">${lab}</span></div>
    <div class="sub">${m.from.name} → ${m.to.name}</div>
    <div class="rs">騎 ${m.qty} 輛・${m.dist_m} m・約 ${m.minutes} 分・零碳排</div></div>`;
}
function assetTxt(t) {
  if (t.bike_no) return `車號 ${t.bike_no}`;
  if (t.dock_id) return `${t.dock_id} 號柱`;
  return '無資產識別';
}
function ticketCard(t) {
  const idx = TK_FLOW.indexOf(t.status);
  const pend = t.diagnosis === 'pending_triage';
  const st = S.stations.find(x => x.sid === t.sid);
  return `<div class="task ${t.status === 'closed' ? 'done' : (pend ? 'iso' : 'repair')} ${S.sel === t.id ? 'sel' : ''}" onclick="sel('${t.id}')">
    <div class="hd"><b>${t.id}・${t.station}</b><span class="pill">${TK_LABEL[t.status] || t.status}</span></div>
    <div class="sub">${ASSET_TXT[t.asset_type] || '設備'}｜${assetTxt(t)}　${t.issue}</div>
    <div class="stepper" style="margin-top:5px">${TK_FLOW.map((k, i) => `<div class="st ${i < idx ? 'done' : (i === idx ? 'cur' : '')}"><i></i>${TK_LABEL[k]}</div>`).join('')}</div>
    <div class="rs">${String(t.ts).slice(11, 16)} 受理${t.reports > 1 ? `・合併 ${t.reports} 次` : ''}${st ? `・該站現有 ${st.bikes ?? '–'} 輛` : ''}
      ${t.crew ? `・<b>${t.crew}</b>${t.eta ? ` ETA ${t.eta}` : ''}` : '・<b style="color:var(--bad)">未指派</b>'}
      ${pend ? '・<b style="color:var(--bad)">待診斷</b>' : ''}</div>
    ${(t.service_flags || []).length ? `<div class="rs"><b style="color:var(--bad)">⚠ ${(t.service_flags || []).map(f => SVC_FLAG[f]).join('、')}</b></div>` : ''}</div>`;
}
function emptyCard(s) {
  return `<div class="task ${s._isolated ? 'iso' : 'empty'}" onclick="openMover(${s.sid})">
    <div class="hd"><b>${s.name}</b><span class="cd ${s.bikes === 0 ? 'urgent' : 'soon'}">${s.bikes === 0 ? '現在 0 輛' : `60 分後零車 ${fmtP(s.pe_60)}`}</span></div>
    <div class="sub">${short(s.district)}・現況 ${s.bikes ?? '–'} 輛／柱 ${s.cap}</div>
    <div class="rs">${s._isolated
      ? `<b style="color:var(--bad)">孤立站</b>：500 公尺內 ${s._nb} 站，最多只有 ${s._nbBest} 輛 → 人力補不了，只能排車組`
      : `500 公尺內有站最多 ${s._nbBest} 輛可調 → 點一下派人力就近補`}</div></div>`;
}
function renderList() {
  const box = $('tasks'), tab = S.leftTab;
  const inD = t => !S.dist || t.district === S.dist;
  if (tab === 'movers') { const m = moverTasks().filter(inD), done = S.manual.filter(x => x.status === 'done' && inD(x));
    box.innerHTML = `<div class="xs muted" style="margin-bottom:6px">早尖峰有 <b>${coverPct() ?? '–'}%</b> 的缺車站在 500 公尺內還有車可借（我們的資料算出來），所以小缺口的第一手段是人力就近補，不出動貨車。</div>`
      + (m.length ? m.map(moverCard).join('') : '<div class="muted small">目前沒有人力任務，到「缺車站」分頁指派。</div>')
      + (done.length ? `<div class="ghdr">已完成<span class="pill">${done.length}</span></div>` + done.slice(0, 5).map(moverCard).join('') : ''); return; }
  if (tab === 'repair') {
    const open = repairTasks(), closed = S.tickets.filter(t => t.status === 'closed');
    const pend = open.filter(t => t.diagnosis === 'pending_triage');
    const unassigned = open.filter(t => t.diagnosis !== 'pending_triage' && !t.crew);
    const doing = open.filter(t => t.diagnosis !== 'pending_triage' && t.crew);
    const sec = (title, items, note) => items.length ? `<div class="ghdr">${title}<span class="pill">${items.length}</span>${note ? `<span class="xs muted">${note}</span>` : ''}</div>` + items.map(ticketCard).join('') : '';
    box.innerHTML = `<div class="xs muted" style="margin-bottom:6px">去重依<b>車號 &gt; 柱號 &gt; 站點＋問題類別</b>；同站兩台不同車不會被合併。維修班組可直接派查，不必等調度貨車到場才發現。</div>`
      + sec('🔎 待診斷（證據不足）', pend, '不確定是哪個設備，先派人現場確認')
      + sec('🔧 待派查', unassigned, '明確設備問題，可直接指派班組')
      + sec('🚐 處理中', doing)
      + sec('✅ 已結案', closed.slice(0, 5))
      || '<div class="muted small">目前沒有維修工單</div>';
    return; }
  if (tab === 'empty') { const es = emptyStations();
    const iso = es.filter(s => s._isolated).length;
    box.innerHTML = `<div class="xs muted" style="margin-bottom:6px">納管三區中現在 0 輛、或 60 分鐘後零車機率 ≥60% 的站，孤立站排最前面。<br>其中 <b style="color:var(--bad)">${iso} 站</b>在 500 公尺內找不到 3 輛以上可調，人力補不了。</div>`
      + (es.length ? es.map(emptyCard).join('') : '<div class="muted small">目前沒有缺車站</div>'); return; }
  if (tab === 'log') {
    box.innerHTML = S.reports.length ? S.reports.map(r => `<div class="logrow ${r.kind}"><span class="t">${r.ts}</span><span>${r.text}</span></div>`).join('')
      : '<div class="muted small">還沒有回報。到右邊派工單按「抵達／完成／異常」就會進來。</div>'; return; }
  const act = S.tasks.filter(t => inD(t) && t.stops.length && ['planned', 'dispatched', 'en_route'].includes(t.status));
  const urgent = act.filter(t => { const x = schedOf(t.id); return (x && x.late) || (!moving(t) && MIN(T(t.depart_by) - NOW()) <= 45); });
  const rest = act.filter(t => !urgent.includes(t));
  const blocked = S.tasks.filter(t => inD(t) && ['too_late', 'needs_cross_district', 'minor_gap'].includes(t.status) && T(t.target_ts) >= NOW());
  const gaps = gapRows().filter(inD);
  const done = S.tasks.filter(t => inD(t) && t.status === 'done');
  const sec = (title, items) => items.length ? `<div class="ghdr">${title}<span class="pill">${items.length}</span></div>` + items.map(taskCard).join('') : '';
  box.innerHTML = sec('🔴 現在要決定／快到最遲出發', urgent.sort((a, b) => T(a.depart_by) - T(b.depart_by)))
    + sec('🟦 已排定與執行中', rest.sort((a, b) => T(a.depart_by) - T(b.depart_by)))
    + sec('🟩 區域缺口與分流', gaps) + sec('⚠️ 無法派車', blocked) + sec('✅ 已完成', done.slice(0, 6))
    || '<div class="muted small">目前沒有任務</div>';
}
function gapRows() {
  const best = {}; S.tasks.filter(t => t.status === 'gap_summary').forEach(t => { const k = t.district + '|' + t.horizon; if (!best[k] || t.created > best[k].created) best[k] = t; });
  return Object.values(best).filter(t => T(t.target_ts) >= NOW());
}
function chips() {
  $('chips').innerHTML = [['', '三區全部'], ...FOCUS.map(d => [d, short(d)])].map(([v, l]) => `<button class="${S.dist === v ? 'on' : ''}" onclick="S.dist='${v}';render()">${l}</button>`).join('');
}

/* ---------------- 右欄：派工單與現場回報 ---------------- */
function stopRow(t, s, i) {
  const x = schedOf(t.id), start = x ? x.start : T(t.depart_by), a = (S.actuals[t.id] || {})[i] || {};
  const eta = start + s.eta_min_from_depart * 60000;
  const late = !a.done && moving(t) && NOW() > eta;
  const issue = a.issue ? ISSUES.find(y => y.k === a.issue) : null;
  return `<div class="stop ${s.action} ${a.done ? 'ok' : ''} ${late ? 'lateStop' : ''}">
    <div class="no">${i + 1}</div><div style="flex:1">
      <b>${s.action === 'pickup' ? '取' : '送'} ${s.qty} 輛</b>　${s.name}
      <span class="xs muted">・預計 ${hm(eta)}${a.arrived ? `・${hm(a.arrived)} 抵達` : ''}</span>
      ${a.done ? `<span class="badge ${a.qty === s.qty ? 'ok' : 'warn'}">實際 ${a.qty}/${s.qty}</span>` : ''}
      ${issue ? `<span class="badge high">${issue.t}${a.bike_no ? `・${a.bike_no}` : (a.dock_id ? `・${a.dock_id} 號柱` : '')}</span>` : ''}
      ${s.action === 'dropoff' && s.due_ts ? `<span class="badge ${s.on_time ? 'ok' : 'high'}">本站時限 ${hhmm(s.due_ts)}（${s.due_min} 分）${s.on_time ? ' 可如期' : `　晚 ${s.late_min} 分`}</span>` : ''}<br>
      <span class="muted">${s.cap == null ? '整車補給，無站況資料' : `現況 ${s.now_bikes ?? '–'} 輛／柱 ${s.cap}・${t.horizon} 分後預測 ${s.pred_bikes} 輛${s.p_empty != null ? `・零車 ${fmtP(s.p_empty)}` : ''}`}</span><br>
      <span class="why">為什麼：${s.reason}</span>
      <div class="rep">
        ${a.arrived ? '' : `<button class="small" onclick="reportArrive('${t.id}',${i})">抵達</button>`}
        ${a.done ? '' : `<span class="row" style="gap:4px"><input type="number" id="q_${t.id}_${i}" value="${s.qty}" min="0" max="99" style="width:46px;font:inherit;padding:2px 4px;border:1px solid var(--line);border-radius:6px">
          <button class="small primary" onclick="reportDone('${t.id}',${i},document.getElementById('q_${t.id}_${i}').value)">完成</button></span>`}
        <select class="small" onchange="if(this.value){openIssue('${t.id}',${i},this.value);this.value=''}">
          <option value="">回報異常…</option>${ISSUES.map(y => `<option value="${y.k}">${y.t}</option>`).join('')}</select>
      </div></div></div>`;
}
async function detailTruck(t, fetchRoute) {
  const x = schedOf(t.id), start = x ? x.start : T(t.depart_by);
  const moveQty = t.stops.reduce((s, v) => s + v.qty, 0);
  const work = Math.round(S.A.handling_fixed_min * t.stops.length + S.A.handling_per_bike_min * moveQty);
  const drive = Math.max(0, t.route_minutes - work);
  const a = S.actuals[t.id] || {}, doneN = Object.values(a).filter(y => y.done).length;
  const gotQty = Object.values(a).reduce((s, y) => s + (y.qty || 0), 0);
  const bk = t.id + ':' + t.status;
  $('detail').innerHTML = `
  <div class="row wrap"><b style="font-size:15px">${t.id}｜${t.district}${t.event_related ? ' 🎤 活動任務' : ''}</b><span class="pill">${x && x.late ? '延誤' : (LAB[t.status] || t.status)}</span>
    <span style="flex:1"></span>${t.status === 'planned' ? `<button class="small primary" onclick="act('${t.id}','confirm')">派工確認</button><button class="small" onclick="act('${t.id}','dispatch_now')">立即出車</button><button class="small" onclick="act('${t.id}','cancel')">取消</button>` : ''}</div>
  <div class="small" style="margin-top:4px">指派 <b>🚚 ${x ? x.crew : '未指派'}</b>（駕駛＋隨車 2 人，情境）　集合點：行政區站點重心 <span class="badge sim">未提供實際場站</span></div>
  <div class="big">
    <div><div class="v" style="color:${x && x.late ? 'var(--bad)' : 'var(--brand)'}">${hm(start)}</div><div class="l">建議出發（最遲 ${hhmm(t.depart_by)}）</div></div>
    <div><div class="v">${hhmm(t.target_ts)}</div><div class="l">目標時間（提前 ${t.horizon} 分排定）</div></div>
    <div><div class="v">${doneN}<span style="font-size:12px;color:var(--muted)">/${t.stops.length}</span></div><div class="l">已回報站數（實搬 ${gotQty}/${moveQty} 輛）</div></div></div>
  ${chainRow(t, x, start)}
  <div class="eff"><span>行駛 ${drive} 分／${t.route_km} km</span><span>站上作業 ${work} 分</span><span>載量 ${t.load}/${cap()}（${Math.round(t.load / cap() * 100)}%）</span><span>回場 ${hm(start + t.route_minutes * 60000)}</span><span>${x ? (x.first ? `新動員前置 ${x.prep} 分` : `在勤改道前置 ${x.prep} 分`) : ''}</span><span>碳排 ${(t.route_km * CO2_TRUCK).toFixed(1)} kg</span>${x ? `<span>${x.late ? `比最遲出發晚 ${-x.slack} 分` : `出發餘裕 ${x.slack} 分`}</span>` : ''}</div>
  <div class="row" style="margin-top:8px;align-items:flex-start"><div class="brief" style="flex:1" id="brief">${(S.briefs[bk] || {}).text || t.reason}</div><span id="brief-src">${srcBadge((S.briefs[bk] || {}).source)}</span></div>
  <div style="margin-top:10px"><b class="small">站點順序與現場回報</b> <span class="badge real">順序與載量為真實計算</span> <span class="badge sim">回報為示範，重整即消失</span></div>
  ${t.stops.map((s, i) => stopRow(t, s, i)).join('')}
  ${t.history ? `<div class="xs muted" style="margin-top:8px">${t.history.map(h => `${h.ts.slice(11)} ${LAB[h.status] || h.status}：${h.note}`).join('　｜　')}</div>` : ''}
  <details class="rule"><summary>這趟為什麼這樣排</summary><ul style="padding-left:16px;margin:6px 0">
    <li>先到預測將滿或庫存充裕的站取車（供給站保留 ${Math.round(S.A.donor_keep_ratio * 100)}% 車柱），再依最近鄰順序送到預測缺車的站。</li>
    <li>單趟上限 ${cap()} 輛；本趟載 ${t.load} 輛。</li>
    ${perStopDue()
      ? `<li><b>每個送車站有自己的服務時限</b>：時限＝該站最早同時滿足「零車機率 ≥ ${Math.round(S.A.risk_threshold * 100)}%」與「預測庫存低於目標下限」的尺度（30／60／120／180 分）。本趟最緊的是 ${t.tightest_due_min ?? '–'} 分鐘後。</li>
         <li>最遲出發 ${hhmm(t.depart_by)} ＝ 對每個<b>來得及</b>的送站各自回推，取最緊的那一個（不是只看第一站）。${t.late_stops && t.late_stops.length ? `<b>${t.late_stops.length} 站趕不上自己的時限</b>：${t.late_stops.slice(0, 3).join('、')}，那幾站要改人力就近補或民眾分流。` : ''}</li>
         <li>決策到抵達拆段：<b>新動員</b>（該組第一趟）要人員到位與車輛出場 ${S.A.lead_prepare_min ?? 15} 分；<b>在勤改道</b>（車已在路上）只需 ${S.A.divert_prepare_min ?? 3} 分切換目的地。行車與裝卸已計入路線與站點作業時間（舊版把 60 分再加一次路程，會重複計算）。兩個前置值都是情境假設，不是實測。最早可抵達第一站 ${hhmm(t.earliest_arrival)}。</li>`
      : `<li>最遲出發 ${hhmm(t.depart_by)} ＝ 目標 ${hhmm(t.target_ts)} − 到<b>第一個</b>送車站的行駛與作業時間。後段站點不保證同樣準時。</li>
         <li>決策到抵達至少 ${S.A.lead_time_min} 分；最早可抵達 ${hhmm(t.earliest_arrival)}。<span style="color:var(--bad)">（逐站服務時限與前置拆段需重啟伺服器後生效）</span></li>`}
    </ul></details>`;
  if (fetchRoute) {
    drawRoute(t);
    if (!S.briefs[bk]) { try { const b = await API.get(`/api/briefing?scope=ops&task_id=${t.id}`); S.briefs[bk] = { text: b.text.replace(/\*\*/g, ''), source: b.source }; } catch (e) { } }
    const c = S.briefs[bk]; if (c && S.sel === t.id && $('brief')) { $('brief').textContent = c.text; $('brief-src').innerHTML = srcBadge(c.source); }
  }
}
function detailMover(m) {
  const lab = { assigned: '待出發', moving: '執行中', done: '完成', cancelled: '取消' }[m.status];
  $('detail').innerHTML = `<div class="row wrap"><b style="font-size:15px">${m.id}｜${m.group}</b><span class="pill">${lab}</span><span style="flex:1"></span>
    ${m.status === 'assigned' ? `<button class="small primary" onclick="moverAct('${m.id}','start')">出發</button><button class="small" onclick="moverAct('${m.id}','cancel')">取消</button>` : ''}
    ${m.status === 'moving' ? `<button class="small primary" onclick="moverAct('${m.id}','done')">完成</button>` : ''}</div>
  <div class="big"><div><div class="v">${m.qty}</div><div class="l">騎乘補車（輛）</div></div>
    <div><div class="v">${m.dist_m}<span style="font-size:12px;color:var(--muted)">m</span></div><div class="l">兩站距離</div></div>
    <div><div class="v" style="color:var(--brand2)">0.0<span style="font-size:12px;color:var(--muted)">kg</span></div><div class="l">碳排（不出動貨車）</div></div></div>
  <div class="eff"><span>約 ${m.minutes} 分（來回騎乘＋每輛 2 分）</span><span>${MOVER_PER_GROUP} 人一組</span><span>指派於 ${String(m.created).slice(11, 16)}</span></div>
  <div class="stop pickup"><div class="no">1</div><div><b>取 ${m.qty} 輛</b>　${m.from.name}<br><span class="muted">現有 ${m.from.bikes} 輛</span></div></div>
  <div class="stop dropoff"><div class="no">2</div><div><b>送 ${m.qty} 輛</b>　${m.to.name}<br><span class="muted">現況 ${m.to.bikes ?? '–'} 輛／柱 ${m.to.cap}・60 分後零車 ${fmtP(m.to.pe_60)}</span></div></div>
  <div class="brief" style="margin-top:8px">${m.reason}</div>
  <div class="xs muted" style="margin-top:6px">依據：我們的資料顯示工作日早尖峰的缺車站有 ${coverPct() ?? '–'}% 在 500 公尺內還有車可借，這類缺口用人力騎乘補位比派貨車快、也沒有碳排。<span class="badge sim">人力編制與作業時間為情境假設</span></div>`;
  drawMover(m);
}
function detailTicket(t) {
  const idx = TK_FLOW.indexOf(t.status);
  const st = S.stations.find(x => x.sid === t.sid);
  const nxt = TK_NEXT[t.status];
  const pend = t.diagnosis === 'pending_triage';
  const ev = [];
  (t.sources || []).forEach(x => ev.push(`${String(x.ts).slice(11, 16)}　來源 ${x.source}／可信度 ${{ stated: '用戶自述', observed: 'AI 影像觀察', confirmed: '現場確認' }[x.certainty] || x.certainty}`));
  (t.error_codes || []).forEach(c => ev.push(`錯誤碼 ${c}`));
  (t.report_ids || []).forEach(r => ev.push(`民眾回報 ${r}`));
  (t.evidence || []).forEach(x => ev.push(typeof x === 'string' ? x : JSON.stringify(x)));
  $('detail').innerHTML = `
    <div class="row wrap"><b style="font-size:15px">${t.id}｜${t.station}</b><span class="pill">${TK_LABEL[t.status]}</span>
      ${pend ? '<span class="badge high">待診斷</span>' : '<span class="badge warn">可直接派修</span>'}<span style="flex:1"></span>
      <span class="xs muted">v${t.version || 1}</span></div>
    <div class="big">
      <div><div class="v" style="font-size:13px">${ASSET_TXT[t.asset_type] || '待判定'}</div><div class="l">${assetTxt(t)}</div></div>
      <div><div class="v" style="font-size:13px">${t.crew || '未指派'}</div><div class="l">負責班組${t.eta ? `・ETA ${t.eta}` : ''}</div></div>
      <div><div class="v">${st ? (st.bikes ?? '–') : '–'}</div><div class="l">該站現有車數（影響評估）</div></div></div>
    <div class="eff"><span>位置：${st ? `${short(st.district)}・${st.lat.toFixed(4)}, ${st.lon.toFixed(4)}` : '—'}</span><span>回報 ${t.reports || 1} 次</span><span>${String(t.ts).slice(5, 16)}</span></div>
    <div class="stepper" style="margin:10px 0">${TK_FLOW.map((k, i) => `<div class="st ${i < idx ? 'done' : (i === idx ? 'cur' : '')}"><i></i>${TK_LABEL[k]}</div>`).join('')}</div>
    <div class="brief" style="margin-bottom:8px">
      <div class="row wrap" style="gap:6px"><b>站點服務觀測</b>${svcBadge(t)}
        ${(t.service_flags || []).map(f => `<span class="badge high">${SVC_FLAG[f]}</span>`).join('')}</div>
      <div class="xs muted" style="margin-top:4px">
        ${(() => { const b2 = t.service_basis || {};
          if (b2.observed === 'down') return `${b2.service || '該服務'}目前觀測為 0；${b2.span_min != null ? `零快照跨度 ${b2.span_min} 分` : ''}${b2.upper_known ? `、最多可能 ${b2.upper_min} 分` : '、上界未知'}${b2.gap_inside ? '（中間有缺測）' : ''}。<b>${b2.wording || '觀測跨度，不是確定的連續中斷時間'}</b>`;
          if (b2.observed === 'no_data') return `${b2.reason || '資料不足'}。缺測不是恢復，也不是中斷。`;
          if (b2.observed === 'ok') return `觀測到可借 ${b2.bikes ?? '–'} 輛／可還 ${b2.spaces ?? '–'} 格。<b>快照不是交易，有車不等於借得到。</b>`;
          return '尚未觀測'; })()}
        ${t.degraded_since ? `<br>首次觀測中斷 ${t.degraded_since}` : ''}${t.restored_at ? `・觀測到恢復 ${t.restored_at}` : ''}
        ${t.service_checked_at ? `<br>觀測時間 ${t.service_checked_at}` : ''}
        ${t.asset_type === 'bike' ? '<br><b>注意</b>：這是站點的借車狀況，<b>不代表被回報的那台車修好了</b>。設備狀態看下面的「設備驗收」。' : ''}
      </div></div>
    <div class="row wrap" style="gap:5px">
      ${t.status === 'reported' ? `<button class="small primary" onclick="openAssign('${t.id}')">指派維修班組</button>` : ''}
      ${nxt && t.status !== 'reported' ? `<button class="small primary" onclick="ticketAct('${t.id}','${nxt}')">推進到「${TK_LABEL[nxt]}」</button>` : ''}
      ${t.status === 'recovered' ? '<span class="xs muted">處理完成不等於驗收，要另外按驗收復役</span>' : ''}
    </div>
    <div style="margin-top:10px"><b class="small">證據</b> <span class="badge grey">AI 影像觀察是證據，不是現場驗收</span></div>
    ${ev.length ? ev.map(x => `<div class="logrow"><span>${x}</span></div>`).join('') : '<div class="muted small">目前只有用戶自述，沒有其他證據</div>'}
    <div style="margin-top:10px"><b class="small">處理歷程</b></div>
    ${(t.history || []).map(h => `<div class="logrow"><span class="t">${String(h.ts).slice(11, 16)}</span><span>${h.label}</span></div>`).join('')}
    <div class="xs muted" style="margin-top:8px">
      設備驗收狀態：<b>${{ suspect: '疑似異常', confirmed_faulty: '已確認故障', repaired: '已處理未驗收', verified_ok: '驗收通過', not_applicable: '不適用' }[t.asset_state] || t.asset_state}</b>；
      用戶坐墊標記：<b>${{ done: '已反轉', skipped: '未操作', not_applicable: '不適用', unknown: '未回報' }[(t.saddle_marker || {}).status] || '未回報'}</b>（僅現場提醒，非維修確認）。
      <span class="badge sim">沒有遠端停租介接，本工具只排除推薦，不會鎖車</span></div>`;
}
function openAssign(tid) {
  const t = S.tickets.find(x => x.id === tid); if (!t) return;
  const crews = allRepairCrews();
  showModal(`指派維修班組｜${t.id} ${t.station}`, `
    <div class="small">${ASSET_TXT[t.asset_type]}｜${assetTxt(t)}　${t.issue}</div>
    <div class="xs muted" style="margin:6px 0">交通局確認維修量能足夠，瓶頸在發現與定位。所以這裡直接派具資格且就近的班組，不等調度貨車到場才發現。</div>
    <label class="xs" style="display:block;margin-top:6px">班組
      <select id="as_crew" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:100%">
        ${crews.map(c => `<option value="${c}">${c}（處理中 ${crewBusy(c)} 件）</option>`).join('')}
      </select></label>
    <label class="xs" style="display:block;margin-top:6px">預計到場時間（沒有把握就留空，不要填假的）
      <input id="as_eta" placeholder="HH:MM" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:110px"></label>
    <div style="text-align:right;margin-top:10px"><button class="small" onclick="closeModal()">取消</button>
      <button class="small primary" onclick="doAssign('${tid}')">派查</button></div>`);
}
async function doAssign(tid) {
  const t = S.tickets.find(x => x.id === tid); if (!t) return;
  const crew = ($('as_crew') || {}).value, eta = (($('as_eta') || {}).value || '').trim();
  closeModal();
  await ticketAct(tid, 'accepted', { crew, eta: eta || null });
}
async function ticketAct(tid, to, extra) {
  const t = S.tickets.find(x => x.id === tid); if (!t) return;
  try {
    const r = await API.post(`/api/ops/tickets/${tid}/transition`, { to, version: t.version, actor: '派車端', ...(extra || {}) });
    log(`${tid} ${TK_LABEL[to]}${(extra || {}).crew ? `・${extra.crew}` : ''}`, 'crew', { tid });
    Toasts.show(`${tid} → ${TK_LABEL[to]}`, r.crew ? `${r.crew}${r.eta ? `・ETA ${r.eta}` : ''}` : '', 'task');
  } catch (e) {
    Toasts.show('推進失敗', '版本可能已被其他人更新，畫面重新整理後再試（409）', 'warn');
  }
  await loadTickets();
}
function detailGap(t) {
  $('detail').innerHTML = `<div class="row between"><b style="font-size:14px">${t.district}｜${t.horizon} 分鐘後缺口</b><span class="badge real">真實計算</span></div>
  <div class="big"><div><div class="v">${t.deficit_total}</div><div class="l">預測缺口（輛）</div></div><div><div class="v" style="color:var(--brand)">${t.covered}</div><div class="l">派車可補</div></div><div><div class="v" style="color:var(--warn)">${t.remaining}</div><div class="l">剩餘靠分流</div></div></div>
  <div class="xs muted" style="margin-top:6px">目標時間 ${hhmm(t.target_ts)}。剩餘缺口不硬派車：由民眾端引導到下列預測仍有車的站，並保留在勤資源因應突發。</div>
  ${(t.alternatives || []).map(a => `<div class="stop"><div class="no" style="background:var(--brand2)">↪</div><div><b>${a.name}</b><br><span class="muted">${t.horizon} 分後預測 ${a.pred_bikes} 輛</span></div></div>`).join('')}`;
}
function detailBlocked(t) {
  const e = t.escalation;
  $('detail').innerHTML = `<div class="row between"><b style="font-size:14px">${t.id}｜${t.district}</b><span class="pill">${LAB[t.status] || t.status}</span></div>
  <div class="brief">${String(t.reason).replace('缺口僅 0 輛', '缺口不足 1 輛')}</div>
  <div class="xs muted" style="margin-top:6px">目標時間 ${hhmm(t.target_ts)}・提前 ${t.horizon} 分鐘評估。跨區與逾時由<b>營運端主責</b>，政府端可追蹤但不另建第二套派車任務。</div>
  ${e ? `<div class="brief" style="margin-top:8px;background:#f1f7ff"><b>已決定：${ESCALATE[e.plan]}</b>${e.eta ? `・ETA ${e.eta}` : '・不提供 ETA（無可派資源）'}<br>
      <span class="xs muted">${e.reason || ''}　${e.ts} 由 ${e.owner} 決定，已同步政府端</span></div>`
    : `<div style="margin-top:10px"><b class="small">處理方案</b>
      <div class="xs muted" style="margin:4px 0">沒有可派人車就選「改民眾分流」，<b>不要編一個到不了的 ETA</b>。</div>
      <div class="row wrap" style="gap:5px">
        <button class="small primary" onclick="openEscalate('${t.id}','cross_district')">跨區支援</button>
        <button class="small" onclick="openEscalate('${t.id}','accept_delay')">接受延誤並通知</button>
        <button class="small" onclick="openEscalate('${t.id}','divert_only')">無可派資源，改分流</button>
      </div></div>`}`;
}
function openEscalate(tid, plan) {
  const needEta = plan === 'cross_district';
  showModal(`處理方案｜${ESCALATE[plan]}`, `
    <div class="xs muted">選定後會記錄負責人與時間，並同步通知政府端。政府端只追蹤，不會另外建立派車任務。</div>
    ${needEta ? `<label class="xs" style="display:block;margin-top:8px">跨區車輛預計到場時間（必填，沒有可派資源請改選其他方案）
      <input id="es_eta" placeholder="HH:MM" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:110px"></label>` : ''}
    <label class="xs" style="display:block;margin-top:6px">說明
      <input id="es_reason" style="font:inherit;font-size:12px;padding:3px 6px;border:1px solid var(--line);border-radius:6px;width:100%"></label>
    <div id="es_err" class="xs" style="color:var(--bad);margin-top:6px"></div>
    <div style="text-align:right;margin-top:10px"><button class="small" onclick="closeModal()">取消</button>
      <button class="small primary" onclick="doEscalate('${tid}','${plan}')">確定</button></div>`);
}
async function doEscalate(tid, plan) {
  const eta = (($('es_eta') || {}).value || '').trim(), reason = (($('es_reason') || {}).value || '').trim();
  if (plan === 'cross_district' && !eta) { $('es_err').textContent = '跨區支援必須填實際可行的到場時間；沒有可派資源請改選「改分流」。'; return; }
  try {
    await API.post(`/api/ops/tasks/${tid}/escalate`, { plan, eta: eta || null, reason, owner: '微笑單車調度中心' });
    closeModal(); log(`${tid} 處理方案：${ESCALATE[plan]}${eta ? `・ETA ${eta}` : '・無 ETA'}`, 'crew', { tid });
    Toasts.show('已記錄處理方案並同步政府端', ESCALATE[plan] + (eta ? `・ETA ${eta}` : '・不提供 ETA'), 'task');
    await loadTasks();
  } catch (e) { $('es_err').textContent = '伺服器拒絕：跨區支援必須帶可行的 ETA。'; }
}
async function detail(fetchRoute) {
  const id = S.sel; if (!id) return;
  const t = S.tasks.find(x => x.id === id);
  if (t) { if (t.status === 'gap_summary') return detailGap(t); if (!t.stops.length) return detailBlocked(t); return detailTruck(t, fetchRoute); }
  const m = S.manual.find(x => x.id === id); if (m) return detailMover(m);
  const k = S.tickets.find(x => x.id === id); if (k) return detailTicket(k);
}

/* ---------------- 地圖 ---------------- */
let map, layRoute, layLive;
function initMap() {
  map = makeMap('map', [25.0, 121.45], 12);
  layRoute = L.layerGroup().addTo(map); layLive = L.layerGroup().addTo(map);
  legendControl(map, `<b>地圖圖例</b><br><i style="background:#0f5fa8"></i>送車站　<i style="background:#d98a00"></i>取車站<br>
    <i style="background:#7a5af5"></i>調度車位置　<i style="background:#0aa47a"></i>人力調度<br><span class="xs muted">位置為依排定路線推算的模擬訊號</span>`);
}
async function drawRoute(t) {
  layRoute.clearLayers();
  L.circleMarker(t.depot, { radius: 7, color: '#fff', fillColor: '#1d2430', fillOpacity: 1 }).bindTooltip('集合點（行政區站點重心，情境）').addTo(layRoute);
  t.stops.forEach((s, i) => L.marker([s.lat, s.lon], { icon: L.divIcon({ className: '', html: `<div class="pin" style="background:${s.action === 'pickup' ? '#d98a00' : '#0f5fa8'}">${i + 1}</div>`, iconSize: [22, 22] }) })
    .bindTooltip(`${i + 1}. ${s.action === 'pickup' ? '取' : '送'} ${s.qty}・${s.name}`).addTo(layRoute));
  try { map.fitBounds(L.featureGroup(layRoute.getLayers()).getBounds().pad(0.2)); } catch (e) { }
  if (!S.routes[t.id]) { try { const r = await API.get(`/api/tasks/${t.id}/route`); S.routes[t.id] = r.geometry; } catch (e) { S.routes[t.id] = null; } }
  if (S.routes[t.id] && S.sel === t.id) L.polyline(S.routes[t.id], { color: '#7a5af5', weight: 5, opacity: .85 }).addTo(layRoute);
  drawLive();
}
function drawMover(m) {
  layRoute.clearLayers();
  if (!ok2([m.from.lat, m.from.lon]) || !ok2([m.to.lat, m.to.lon])) return;
  L.polyline([[m.from.lat, m.from.lon], [m.to.lat, m.to.lon]], { color: '#0aa47a', weight: 4, dashArray: '6 5' }).addTo(layRoute);
  L.marker([m.from.lat, m.from.lon], { icon: L.divIcon({ className: '', html: `<div class="pin" style="background:#d98a00">取</div>`, iconSize: [22, 22] }) }).bindTooltip(m.from.name).addTo(layRoute);
  L.marker([m.to.lat, m.to.lon], { icon: L.divIcon({ className: '', html: `<div class="pin" style="background:#0aa47a">送</div>`, iconSize: [22, 22] }) }).bindTooltip(m.to.name).addTo(layRoute);
  try { map.fitBounds(L.featureGroup(layRoute.getLayers()).getBounds().pad(0.4)); } catch (e) { }
  drawLive();
}
function ok2(p) { return p && Number.isFinite(+p[0]) && Number.isFinite(+p[1]); }
function drawLive() {
  layLive.clearLayers();
  (S.sched ? S.sched.all : []).filter(x => moving(x.t)).forEach(x => {
    const p = truckPos(x); if (!ok2(p)) return;
    L.marker(p, { icon: L.divIcon({ className: '', html: `<div class="pin truck">🚚</div>`, iconSize: [24, 24] }) })
      .bindTooltip(`${x.crew}｜${x.t.id}　${hm(NOW())} 位置（模擬訊號）`).addTo(layLive);
  });
  moverTasks().filter(m => m.status === 'moving' && ok2([m.from.lat, m.from.lon]) && ok2([m.to.lat, m.to.lon])).forEach(m => {
    L.marker([(m.from.lat + m.to.lat) / 2, (m.from.lon + m.to.lon) / 2], { icon: L.divIcon({ className: '', html: `<div class="pin mover">🚲</div>`, iconSize: [24, 24] }) })
      .bindTooltip(`${m.group}｜騎乘中（模擬訊號）`).addTo(layLive);
  });
  if (S.leftTab === 'empty') emptyStations().slice(0, 25).forEach(s =>
    L.circleMarker([s.lat, s.lon], { radius: 6, color: '#fff', weight: 1.5, fillColor: s.bikes === 0 ? '#d64545' : '#e08a00', fillOpacity: .9 })
      .bindTooltip(`${s.name}<br>現況 ${s.bikes} 輛／柱 ${s.cap}`).on('click', () => openMover(s.sid)).addTo(layLive));
}

function chainRow(t, x, start) {
  const hist = t.history || [];
  const conf = hist.find(h => h.note && h.note.includes('確認'));
  const dep = hist.find(h => h.status === 'dispatched');
  const a0 = (S.actuals[t.id] || {})[0] || {};
  const seg = (label, ts, prev) => `<span class="${ts ? '' : 'pend'}">${label}<b>${ts ? (typeof ts === 'string' ? ts.slice(11, 16) : hm(ts)) : '待回報'}</b>${ts && prev ? `<i>+${MIN((typeof ts === 'string' ? T(ts) : ts) - (typeof prev === 'string' ? T(prev) : prev))}分</i>` : ''}</span>`;
  return `<div class="chain">${seg('指派 ', t.created)}${seg('確認 ', conf && conf.ts, t.created)}${seg('出發 ', dep ? dep.ts : (moving(t) ? start : null), (conf && conf.ts) || t.created)}${seg('首站抵達 ', a0.arrived, dep ? dep.ts : start)}
    <span class="xs muted">各段耗時是驗收調度流程的指標（指派→確認→出發→抵達→恢復）</span></div>`;
}

/* ---------------- 人員面板：狀態與負荷 ---------------- */
function crewLoad(id) {
  const trs = (S.sched ? S.sched.all : []).filter(x => x.crew === id);
  let qty = 0, stops = 0, done = 0, issues = 0, mins = 0, km = 0;
  trs.forEach(x => {
    const a = S.actuals[x.t.id] || {};
    stops += x.t.stops.length; mins += x.t.route_minutes; km += x.t.route_km || 0;
    x.t.stops.forEach((s, i) => { const r = a[i]; if (r && r.qty != null) { qty += r.qty; done++; } if (r && r.issue) issues++; });
  });
  const first = trs.length ? Math.min(...trs.map(x => x.start)) : null;
  const cont = first != null && NOW() > first ? MIN(NOW() - first) : 0;
  return { trips: trs.length, qty, stops, done, issues, mins, km, cont };
}
function openRoster(d) {
  const rows = [];
  for (let i = 1; i <= crewsOf(d); i++) {
    const id = crewId(d, i), st = crewStatus(id), L = crewLoad(id);
    rows.push(`<tr><td><b>${id}</b><br><span class="xs muted">駕駛＋隨車 2 人</span></td>
      <td><select onchange="setCrew('${id}',this.value)" style="font:inherit;font-size:11px;padding:2px 4px;border:1px solid var(--line);border-radius:6px">
        ${Object.entries(CREW_ST).map(([k, v]) => `<option value="${k}" ${st === k ? 'selected' : ''}>${v}</option>`).join('')}</select></td>
      <td class="mono">${L.trips}</td><td class="mono">${L.done}/${L.stops}</td><td class="mono">${L.qty}</td>
      <td class="mono">${L.mins}</td><td class="mono" style="color:${L.cont > 240 ? 'var(--bad)' : 'inherit'}">${L.cont}</td>
      <td class="mono" style="color:${L.issues ? 'var(--bad)' : 'var(--muted)'}">${L.issues}</td></tr>`);
  }
  const b = baseDistrict(d), mv = moverTasks().filter(m => m.district === d);
  const busyGroups = new Set(mv.map(m => m.group)).size;
  showModal(`${d}｜人員與車輛編制`, `
    <div class="small" style="margin-bottom:8px">本區配置：<b>🚚 車組 ${crewsOf(d)} 車／${crewsOf(d) * MOVER_PER_GROUP} 人</b>　
      <b style="color:var(--brand2)">🚲 人力調度 ${b.movers} 人／${moverGroups(d)} 組</b>（${busyGroups} 組執行任務、其餘巡檢與站點整理）　
      <b style="color:#8a5fd0">🔧 維修 ${b.repair} 人／${repairGroups(d)} 組</b></div>
    <table><tr><th>車組</th><th>狀態</th><th>趟次</th><th>回報站數</th><th>已搬運(輛)</th><th>路程(分)</th><th>連續作業(分)</th><th>異常</th></tr>${rows.join('')}</table>
    <div class="xs muted" style="margin-top:8px">連續作業超過 240 分標紅，提醒安排換班。全市 40 車／350 人為公開數字；每車 2 人與職務拆分為情境假設（見「編制來源」）。</div>`);
}

/* ---------------- 說明 ---------------- */
function showModal(title, body) { $('modalBody').innerHTML = `<b style="font-size:15px">${title}</b><div style="margin-top:8px">${body}</div><div style="text-align:right;margin-top:10px"><button class="small" onclick="closeModal()">關閉</button></div>`; $('modal').classList.add('on'); }
function closeModal() { $('modal').classList.remove('on'); }
function openInfo(k) {
  const A = S.A || {}, b = S.base || { public: {}, city: {}, focus: {}, staffing_assumption: {} };
  if (k === 'fleet') return showModal('編制怎麼來的', `<table>
    <tr><th>項目</th><th>值</th><th>來源</th></tr>
    <tr><td>全市調度車</td><td>${b.public.dispatch_trucks} 輛</td><td>公開報導（由 33 輛增為 40 輛）</td></tr>
    <tr><td>全市調度人力</td><td>${b.public.dispatch_staff} 人</td><td>公開報導（由 314 人增為 350 人）</td></tr>
    <tr><td>車組</td><td>每車 ${b.staffing_assumption.crew_per_truck} 人，共 ${b.staffing_assumption.crew_total} 人</td><td><b>情境假設</b>（公開資料只有總數）</td></tr>
    <tr><td>人力調度員</td><td>${b.staffing_assumption.movers_total} 人</td><td><b>情境假設</b></td></tr>
    <tr><td>維修</td><td>${b.staffing_assumption.repair_total} 人</td><td><b>情境假設</b></td></tr>
    <tr><td>三區配車</td><td>${b.focus['trucks_by_deficit_share_' + S.peak]} 輛</td><td>40 車依各區${S.peak === 'am' ? '早' : '晚'}尖峰缺口權重分配（我們的資料算出來）</td></tr>
    <tr><td>若按站數比例</td><td>${b.focus.trucks_by_station_share} 輛</td><td>三區 ${b.focus.stations} 站／全市 ${b.city.stations} 站</td></tr></table>
    <div class="xs muted" style="margin-top:8px">三區只佔全市 ${b.focus.station_share_pct}% 的站，卻吃掉 ${b.focus['deficit_share_' + S.peak + '_pct']}% 的尖峰缺口，所以按缺口配車比按站數配車多。完整對照見 docs/DATA_SOURCES.md。</div>`);
  if (k === 'rules') return showModal('排程依據（每一步都對應可查的數字）', `<ol class="small" style="padding-left:18px;line-height:1.7">
    <li><b>預測</b>：1–4 月訓練、5 月驗證、6 月測試的梯度提升樹，逐站算 30／60／120／180 分鐘後的可借車數、可還格數與零車／零位機率。</li>
    <li><b>缺口</b>：目標最低庫存＝max(${A.min_stock_abs} 輛, 車柱×${Math.round(A.min_stock_ratio * 100)}%)；預測零車機率 ≥ ${Math.round(A.risk_threshold * 100)}% 的站才列入。</li>
    <li><b>供給</b>：預測將滿的站優先運出；庫存充裕的站可供給但保留 ≥ ${Math.round(A.donor_keep_ratio * 100)}% 車柱。</li>
    <li><b>組趟</b>：單趟上限 ${A.truck_capacity} 輛，最近鄰貪婪，先取後送；缺口不足 3 輛不單獨成趟，改派人力就近補。</li>
    <li><b>每站服務時限</b>：每個缺車站各自算出最早「零車機率過門檻且預測庫存低於下限」的尺度（30／60／120／180 分），那才是該站的期限，不是整趟共用一個目標時間。</li>
    <li><b>最遲出發</b>＝對每個來得及的送車站各自回推（${A.truck_speed_kmh} km/h、直線×${A.road_detour}、每站 ${A.handling_fixed_min} 分＋每輛 ${A.handling_per_bike_min} 分），取最緊的那一個。</li>
    <li><b>決策到抵達拆段</b>：人員準備與車輛出場 ${A.lead_prepare_min ?? 15} 分是固定前置；行車與裝卸已含在路段與站點作業時間裡。訪談講的 60 分鐘是總量，舊寫法「60 分再加路程」會把行車重複算一次。</li>
    <li><b>來不及判定</b>：一趟裡所有送車站都趕不上自己的時限才標「來不及」；只有部分站趕不上時，那幾站改走人力就近補與民眾分流，其餘照送。</li>
    <li><b>跨尺度資源帳</b>：120 與 180 分鐘的規劃共用一本帳，已承諾的送車量與已抽走的供給量會先扣掉，避免同一批車與同一個站被承諾兩次。</li>
    <li><b>人力優先</b>：工作日早尖峰的缺車站有 <b>${b.city.neighbor_cover_am_pct}%</b> 在 500 公尺內還有車可借（${b.city.empty_obs_am.toLocaleString()} 筆零車觀測算出），真正「走五分鐘也借不到」只有 ${b.city.stranded_am_pct}%。所以小缺口先派人力，貨車留給大缺口。</li>
    <li><b>車輛配置</b>：每一趟指派到在勤車組（同區連續作業、趟次間整備 ${HANDOVER} 分），算出哪一趟趕不上最遲出發。</li></ol>
    <div class="xs muted">前六項由伺服器真實計算；車隊數量與人員指派為公開數字＋情境假設的換算，現場回報與位置訊號為示範用。</div>`);
  showModal('情境假設（畫面上數字的前提）', `<table><tr><th>項目</th><th>值</th><th>說明</th></tr>
    <tr><td>單趟載量</td><td>${A.truck_capacity} 輛</td><td>公開資料只提到 3.25 噸貨車，載量為情境值</td></tr>
    <tr><td>市區車速</td><td>${A.truck_speed_kmh} km/h</td><td>直線距離 ×${A.road_detour}</td></tr>
    <tr><td>站上作業</td><td>${A.handling_fixed_min} 分＋${A.handling_per_bike_min} 分/輛</td><td>每台 YouBike 重 21.3 公斤（公開）</td></tr>
    <tr><td>決策到抵達</td><td>≥ ${A.lead_time_min} 分</td><td>使用者確認的現場前置時間</td></tr>
    <tr><td>趟次間整備</td><td>${HANDOVER} 分</td><td>本頁排班用</td></tr>
    <tr><td>新動員前置</td><td>${A.lead_prepare_min ?? 15} 分</td><td>該組第一趟：人員到位＋車輛出場。<b>情境值，非實測</b></td></tr>
    <tr><td>在勤改道前置</td><td>${A.divert_prepare_min ?? 3} 分</td><td>車已在路上，只切換目的地。<b>情境值，非實測</b></td></tr>
    <tr><td>調度車碳排</td><td>${CO2_TRUCK} kg CO2e/km</td><td>3.5 噸級柴油貨車概估，正式數字須引用環境部公告</td></tr>
    <tr><td>車與人的位置</td><td>沿排定路線推算</td><td>未接車機 GPS</td></tr>
    <tr><td>現場回報</td><td>本機示範</td><td>重新整理即消失；正式版應寫入後端與 DynamoDB</td></tr></table>`);
}

/* ---------------- 流程 ---------------- */
function render() {
  initAlloc(); schedule(); renderCity(); renderKpis(); renderGantt(); renderTabs(); chips(); renderList(); drawLive(); detail(false);
}
function sel(id) { S.sel = id; render(); detail(true); }
async function act(id, a) {
  const t = S.tasks.find(x => x.id === id);
  const map = { confirm: 'confirm', dispatch_now: 'dispatch', cancel: 'cancel' };
  const action = map[a] || a;
  try {
    const r = await API.post(`/api/ops/tasks/${id}/${action}`, { version: t ? t.res_version : undefined, actor: '調度員' });
    log(`${id} ${ { confirm: '派工確認，資源轉為已確認', dispatch: '立即出車，資源轉為在途', cancel: '取消，資源已釋放' }[action] }`
        + (r.reservations_changed ? `（${r.reservations_changed} 筆預約）` : ''), 'crew', { tid: id });
  } catch (e) {
    Toasts.show('動作失敗', '版本可能已被更新（409），畫面會重新載入', 'warn');
  }
  await loadTasks(); await loadCycle();
}
async function loadCycle() { try { S.cycle = await API.get('/api/ops/cycle'); } catch (e) { S.cycle = null; } }
function openCycle() {
  const c = S.cycle;
  if (!c) return showModal('資源帳', '<div class="muted small">尚未取得資源帳（伺服器可能還沒載入派車端端點，需重啟一次）。</div>');
  const rows = c.by_station.filter(r => (r.candidate + r.confirmed + r.in_transit) > 0)
    .sort((a, b) => (b.candidate + b.confirmed + b.in_transit) - (a.candidate + a.confirmed + a.in_transit)).slice(0, 25);
  showModal(`規劃週期資源帳｜${c.cycle}`, `
    <div class="small">回放時刻 ${c.ts}・已規劃尺度 ${c.horizons.join('／')} 分・預約 ${c.reservations} 筆</div>
    <div class="xs muted" style="margin:6px 0">可用量＝原始可用量 − 候選 − 已確認 − 在途。取消才會釋放；重算只釋放候選，不會把已確認的承諾吃掉。</div>
    <table><tr><th>站點</th><th>類型</th><th>候選</th><th>已確認</th><th>在途</th><th>已釋放</th></tr>
    ${rows.map(r => `<tr><td>${r.station || r.sid}</td><td>${r.kind === 'supply' ? '抽車' : '送車'}</td>
      <td class="mono">${r.candidate || 0}</td><td class="mono">${r.confirmed || 0}</td>
      <td class="mono">${r.in_transit || 0}</td><td class="mono muted">${r.released || 0}</td></tr>`).join('')}</table>
    <div class="xs muted" style="margin-top:8px">GET 這份快照不會改變任何狀態。</div>`);
}
async function loadTasks() { const d = await API.get('/api/tasks'); S.tasks = d.tasks; S.A = d.assumptions; render(); }
async function loadStations() { try { const d = await API.get('/api/stations?adjusted=1'); S.stations = d.stations; renderCity(); if (S.leftTab === 'empty') renderList(); } catch (e) { } }
async function loadTickets() { try { const d = await API.get('/api/tickets'); S.tickets = d.tickets; S.flow = d.flow; render(); } catch (e) { } }
async function loadAlerts() { try { const d = await API.get('/api/alerts'); S.alerts = d.alerts.filter(a => a.route === '調度端' && a.status !== 'resolved'); renderAlerts(); } catch (e) { } }
function alertAge(a) { return MIN(NOW() - T(a.opened)); }
function renderAlerts() {
  const list = S.alerts.slice().sort((x, y) => alertAge(y) - alertAge(x));
  const over = list.filter(a => alertAge(a) >= SERVICE_TARGET_MIN && !a.acked);
  const persistent = list.filter(a => String(a.type).startsWith('persistent'));
  $('sudden').innerHTML = `<div class="row between"><b>服務事件</b><span class="xs muted">${list.length} 件・已觀測到空滿 ${persistent.length} 件</span></div>`
    + `<div class="xs" style="margin:2px 0 4px">${over.length ? `<b style="color:var(--bad)">${over.length} 件已超過 ${SERVICE_TARGET_MIN} 分鐘服務目標未確認</b>` : `<span class="muted">沒有超過 ${SERVICE_TARGET_MIN} 分鐘未確認的事件</span>`}<span class="muted">（30 分鐘為主管訪談輸入，非官方 SLA）</span></div>`
    + (list.length ? list.slice(0, 3).map(a => `<div class="a"><span style="flex:1">${a.message}<br><span class="xs ${alertAge(a) >= SERVICE_TARGET_MIN ? '' : 'muted'}" style="${alertAge(a) >= SERVICE_TARGET_MIN ? 'color:var(--bad)' : ''}">已開啟 ${alertAge(a)} 分${a.acked ? `・${a.acked.slice(11, 16)} 已確認` : ''}</span></span>${a.acked ? '' : `<button class="small" onclick="API.post('/api/alerts/${a.id}/ack').then(loadAlerts)">確認</button>`}</div>`).join('')
      + (list.length > 3 ? `<div class="xs muted">另有 ${list.length - 3} 件，見政府端事件清單</div>` : '')
      : `<div class="xs muted">目前沒有需要立即處理的事件</div>`);
}
function setPeak(p) { S.peak = p; S.alloc = {}; initAlloc(); render(); }

connectEvents({
  tick: p => { S.clock = p.clock; $('clock').textContent = '回放時間 ' + p.clock.ts; loadTasks(); loadStations(); loadAlerts(); loadCycle(); },
  task: () => loadTasks(), ticket: () => loadTickets(), alert: () => loadAlerts(),
  notify: n => { if (n.channel === 'ops') Toasts.show(n.title, n.body, n.kind, n.ts); },
  scenario: s => { S.sel = null; S.manual = []; S.actuals = {}; S.reports = []; S.routes = {}; S.scen = s && s.label ? s.label : ''; $('scen').textContent = S.scen ? '情境：' + S.scen : ''; loadTasks(); loadStations(); loadAlerts(); },
});
(async () => {
  initMap();
  try { S.base = await API.get('/static/ops_baseline.json'); } catch (e) { console.warn('ops_baseline.json 讀取失敗', e); }
  const st = await API.get('/api/state'); S.clock = st.clock; $('clock').textContent = '回放時間 ' + st.clock.ts;
  $('scen').textContent = st.scenario && st.scenario.label ? '情境：' + st.scenario.label : '';
  await loadTasks(); await loadStations(); await loadTickets(); await loadAlerts(); await loadCycle();
  const first = truckTrips().sort((a, b) => T(a.depart_by) - T(b.depart_by))[0]; if (first) sel(first.id);
})();
