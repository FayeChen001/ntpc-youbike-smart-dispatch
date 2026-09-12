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
  { k: 'dock_blocked', t: '車柱被占用', icon: 'ti-parking-off' },
  { k: 'construction', t: '站點施工', icon: 'ti-traffic-cone' },
  { k: 'broken_bike', t: '車輛故障待回收', icon: 'ti-tools' },
  { k: 'stuck', t: '車輛卡樁', icon: 'ti-lock' },
  { k: 'no_parking', t: '現場無法停車', icon: 'ti-ban' },
  { k: 'traffic', t: '交通壅塞', icon: 'ti-traffic-lights' },
  { k: 'weather', t: '天候中斷', icon: 'ti-cloud-storm' }];
const CREW_ST = { on: '在勤', meal: '用餐', rest: '休息', off: '下班' };

const S = { base: null, stations: [], tasks: [], A: null, tickets: [], flow: [], clock: null, scen: '',
  view: 'ops', dist: '', sel: null, alloc: {}, manual: [], reports: [], actuals: {}, crewState: {},
  routes: {}, sched: null, briefs: {}, mseq: 0, alerts: [], leftTab: 'tasks', peak: 'am' };

const T = s => s ? new Date(String(s).replace(' ', 'T')).getTime() : 0;
const NOW = () => S.clock ? T(S.clock.ts) : Date.now();
const MIN = ms => Math.round(ms / 60000);
const hm = ms => { const d = new Date(ms); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };
const cap = () => S.A ? S.A.truck_capacity : 20;
const short = d => String(d).replace('區', '');

/* ---------------- 編制：全市公開數字 → 三區配置 ---------------- */
function baseDistrict(d) { return (S.base ? S.base.districts : []).find(x => x.district === d) || {}; }
function initAlloc() {
  if (Object.keys(S.alloc).length) return;
  FOCUS.forEach(d => { S.alloc[d] = baseDistrict(d)['trucks_' + S.peak] || 1; });
}
function crewsOf(d) { return S.alloc[d] || 0; }
function moverGroups(d) { return Math.max(1, Math.round((baseDistrict(d).movers || 0) / MOVER_PER_GROUP)); }
function repairGroups(d) { return Math.max(1, Math.round((baseDistrict(d).repair || 0) / MOVER_PER_GROUP)); }
function crewId(d, n) { return `${short(d)}${n}車`; }
function crewStatus(id) { return S.crewState[id] || 'on'; }
function setCrew(id, v) { S.crewState[id] = v; render(); log(`${id} 狀態改為 ${CREW_ST[v]}`, 'crew'); }

/* ---------------- 任務池 ---------------- */
function truckTrips() { return S.tasks.filter(t => t.stops.length > 0 && ['planned', 'dispatched', 'en_route'].includes(t.status)); }
function moving(t) { return t.status === 'dispatched' || t.status === 'en_route'; }
function moverTasks() { return S.manual.filter(m => m.status !== 'done' && m.status !== 'cancelled'); }
function repairTasks() { return S.tickets.filter(t => t.status !== 'closed'); }

/* ---------------- 排班：把每一趟指派到在勤車組 ---------------- */
function scheduleDistrict(trips, crewIds, now) {
  const veh = crewIds.map(id => ({ id, free: now, trips: [] }));
  const out = { veh, late: 0, lateLoad: 0, onTime: 0, busy: 0 };
  const sorted = trips.slice().sort((a, b) => (moving(a) ? 0 : 1) - (moving(b) ? 0 : 1) || T(a.depart_by) - T(b.depart_by));
  for (const t of sorted) {
    if (!veh.length) { out.late++; out.lateLoad += t.load; continue; }
    const v = veh.reduce((a, b) => b.free < a.free ? b : a);
    const start = moving(t) ? Math.min(T(t.depart_by), now) : Math.max(v.free, now);
    const end = start + t.route_minutes * 60000, late = start > T(t.depart_by) + 60000;
    v.trips.push({ t, start, end, late, slack: MIN(T(t.depart_by) - start) });
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
function reportIssue(tid, i, key) {
  const t = findTask(tid), s = t.stops[i], a = actual(tid), def = ISSUES.find(x => x.k === key);
  a[i] = { ...(a[i] || {}), issue: key, issue_ts: NOW() };
  log(`${schedOf(tid) ? schedOf(tid).crew : tid} 於 ${s.name} 回報「${def.t}」`, 'issue', { tid, i, key });
  if (key === 'broken_bike' && s.sid > 0) {
    API.post('/api/tickets', { sid: s.sid, issue: '調度員現場回報：車輛故障待回收', note: `由 ${schedOf(tid) ? schedOf(tid).crew : tid} 於派工途中回報`, bike_no: '' })
      .then(tk => { Toasts.show('已開維修工單 ' + tk.id, `${s.name}｜車輛故障待回收`, 'warn'); loadTickets(); })
      .catch(() => Toasts.show('工單建立失敗', '請稍後再試', 'warn'));
  }
  render();
}
function findTask(id) { return S.tasks.find(t => t.id === id) || S.manual.find(t => t.id === id); }

/* ---------------- 人力調度任務（就近補車，不出動貨車） ---------------- */
function emptyStations() {
  return S.stations.filter(s => FOCUS.includes(s.district) && (!S.dist || s.district === S.dist)
    && ['normal', 'empty', 'full'].includes(s.status) && (s.bikes === 0 || (s.pe_60 != null && s.pe_60 >= 0.6)))
    .sort((a, b) => (b.pe_60 || 0) - (a.pe_60 || 0)).slice(0, 40);
}
async function openMover(sid) {
  const d = await API.get('/api/station/' + sid);
  const st = d.station, nbs = (d.neighbors || []).filter(n => n.dist_m <= 500 && n.bikes >= 3).slice(0, 5);
  const body = `<b>${st.name}</b> <span class="pill">${statusLabel(st.status)}</span>
    <div class="small" style="margin-top:4px">現況 ${st.bikes ?? '–'} 輛／柱 ${st.cap}・60 分後零車機率 ${fmtP(st.pe_60)}</div>
    <div class="xs muted" style="margin:8px 0 4px">500 公尺內還有車的站（早尖峰有 99.1% 的缺車站符合這個條件，所以第一手段是人力就近補，不是出動貨車）</div>
    ${nbs.length ? nbs.map(n => `<div class="row between" style="padding:5px 0;border-bottom:1px solid var(--line)">
        <span class="small">${n.name}<br><span class="xs muted">${Math.round(n.dist_m)} m・步行 ${n.walk_min} 分・現有 ${n.bikes} 輛</span></span>
        <span class="row"><input type="number" id="mq${n.sid}" value="${Math.min(5, Math.max(1, Math.floor((n.bikes - 3) / 2) || 1))}" min="1" max="10" style="width:52px;font:inherit;padding:2px 4px;border:1px solid var(--line);border-radius:6px">
        <button class="small primary" onclick="createMover(${sid},${n.sid})">派人力</button></span></div>`).join('')
      : '<div class="muted small">500 公尺內沒有可供給的鄰站，需改由車組處理或跨區支援。</div>'}`;
  showModal('派人力調度員就近補車', body);
  S._mover = { to: st, nbs };
}
function createMover(toSid, fromSid) {
  const n = S._mover.nbs.find(x => x.sid === fromSid), to = S._mover.to;
  const qty = Math.max(1, Number(($('mq' + fromSid) || {}).value || 2));
  const ride = Math.max(3, Math.round(n.dist_m / 1000 / 12 * 60));         // 騎乘 12 km/h（與 planner 同）
  const mins = ride * 2 + 2 * qty;                                          // 來回＋每輛 2 分
  S.mseq++;
  const id = 'M' + String(S.mseq).padStart(3, '0');
  const group = `${short(to.district)}人力${(S.mseq % moverGroups(to.district)) + 1}組`;
  S.manual.push({ id, kind: 'mover', district: to.district, group, from: { sid: n.sid, name: n.name, lat: n.lat, lon: n.lon, bikes: n.bikes },
    to: { sid: to.sid, name: to.name, lat: to.lat, lon: to.lon, bikes: to.bikes, cap: to.cap, pe_60: to.pe_60 },
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
    for (let i = 1; i <= crewsOf(d); i++) {
      const id = crewId(d, i), st = crewStatus(id), v = r.veh.find(x => x.id === id);
      const trs = v ? v.trips : [];
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
    ? `以目前在勤車組：<b class="bad">${late} 趟趕不上目標時間</b>，合計 <b class="bad">${lateLoad} 輛</b>無法如期到位。建議 ${fix.join('、')}${spare.length ? `；${spare.join('、')}可支援` : ''}。<span class="xs muted">最遲出發＝目標時間 − 到第一個送車站的行駛與作業時間；車輛連續作業、趟次間整備 ${HANDOVER} 分為情境假設。</span>`
    : `以目前在勤車組：<b class="ok">所有趟次都能在目標時間前到位</b>${spare.length ? `，且 ${spare.join('、')}可支援其他區或留作突發備援` : ''}。<span class="xs muted">車輛連續作業、趟次間整備 ${HANDOVER} 分為情境假設。</span>`;
}
