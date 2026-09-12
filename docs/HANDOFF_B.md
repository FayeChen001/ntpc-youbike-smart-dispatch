# HANDOFF_B：微笑單車營運中控端對外契約

版本 `b-1`。基準 main `a1c6dbb` 之後。這份文件給 A（政府端）與 C（用戶端）串接用。
線上可讀版本：`GET /api/ops/contract`。

## 0. 一句話

**建單只有一個入口。** `POST /api/tickets`（C 既有路徑）與 `POST /api/ops/tickets` 都委派
`server.submit_ticket()` → `app/tickets.py`。`server.py` 內部呼叫（`api_trip_report_bike`、
C 的症狀回報）也走同一個函式，不存在第二套去重規則。

> 修正前的狀況：`api_ticket_create` 用「站點＋問題類別＋2 小時」去重、**完全丟掉 `dock_no`**，
> 而 `/api/ops/tickets` 用資產識別去重。C 的 HTTP 與內部呼叫都走舊那套，所以同站兩台不同車會被
> 合併成一張、柱號結構化欄位遺失、重送會重建。已於本輪收斂成單一服務。

## 1. 識別與命名

| 欄位 | 規則 |
|---|---|
| `dock_id` | **對外統一用這個**。相容輸入 `dock_no`，在 `tickets.normalize()` 正規化一次，之後全系統只有 `dock_id` |
| `bike_no` | 字串，去前後空白 |
| **空值** | 沒有就是 `null`，**不用空字串冒充識別**。`"  "` 會被收斂成 `null` |
| 可追蹤 id | `ticket_id`(R###)、`report_id`(C 的回報)、`request_id`(冪等)、`sid`(站)、`task_id`(派車任務)、`event_id`(A 的台帳) |

## 2. 建單

```
POST /api/ops/tickets        （或沿用 POST /api/tickets，同一個實作）
{
  "sid": 0,                       // 必填
  "issue": "鏈條掉落",
  "bike_no": "YB-12345",          // 可選
  "dock_no": "07",                // 可選，會被正規化成 dock_id
  "error_code": "E12",            // 可選
  "asset_type": "bike",           // 可選，不給就依 bike_no/dock_id 推斷
  "request_id": "demo-1",         // 建議帶：重送冪等
  "report_id": "CR001",           // C 的回報 id，會存進 report_ids[]
  "source": "citizen",
  "symptom_keys": ["chain"],      // 明確機械問題 → diagnosis=direct_repair
  "certainty": "stated",          // stated / observed / confirmed
  "evidence": []                  // AI 圖片 observations 等，原樣保留
}
```

實測回應（8789 測試機，2026-06-16 07:00 回放時鐘）：

```json
{"id":"R001","version":1,"sid":0,"station":"三峽中山公園",
 "issue":"鏈條掉落","bike_no":"YB-12345","dock_id":"07","error_code":"E12",
 "asset_type":"bike","dedup_key":"bike:YB-12345","dedup_basis":"bike",
 "diagnosis":"direct_repair","status":"reported","service_state":"unknown",
 "asset_state":"suspect","saddle_marker":{"status":"unknown","source":null,"ts":null},
 "reports":1,"report_ids":["CR001"],"related_ids":[],"error_codes":["E12"],
 "assignee":null,"eta":null,"crew":null,"action":"created"}
```

`action` ∈ `created` / `merged` / `idempotent`。

### 去重規則（優先序）

| 順序 | 依據 | 鍵 | 時間窗 |
|---|---|---|---|
| 1 | 車號 | `bike:{bike_no}` | 無（同一台壞車隔天回報還是同一台） |
| 2 | 柱號 | `dock:{sid}:{dock_id}` | 無 |
| 3 | 站點＋問題類別 | `station:{sid}:{issue}` | 2 小時，**且雙方都必須沒有資產識別** |

沒有資產識別的新回報**不會**併進「已經有車號」的工單（無法證明是同一台），改成把同站未結案工單
列在 `related_ids`，只關聯不合併。

### 明確 vs 待診斷

- `diagnosis=direct_repair`：有資產識別，或 `symptom_keys` 命中機械類（tire/brake/chain/seat/frame/pedal/light/kickstand）。可直接派修。
- `diagnosis=pending_triage`：「借不到／刷卡沒反應／扣款怪怪的」這類。**不判車柱壞掉**，進待診斷清單。
  用 `GET /api/ops/tickets?pending=1` 撈。

> AI 圖片觀察請放 `evidence[]` 與 `certainty="observed"`，**不要**寫進 `asset_state`。
> 圖片是證據，不是現場驗收。

## 3. 狀態分四組（不可共用一個欄位）

| 欄位 | 值 | 意義 |
|---|---|---|
| `status` | reported → accepted → on_site → recovered → verified → closed | **工單處理流程** |
| `service_state` | unknown / degraded / restored | 站點服務是否恢復 |
| `asset_state` | suspect / confirmed_faulty / repaired / verified_ok / not_applicable | 設備驗收 |
| `saddle_marker.status` | unknown / done / skipped / not_applicable | 用戶現場標記，**非維修確認** |

`recovered` 只把 `asset_state` 推到 `repaired`；**要到 `verified` 才是 `verified_ok`**。修復不等於自動驗收。

## 4. 坐墊標記（C → B）

```
POST /api/ops/tickets/{ticket_id}/saddle
{"status": "done|skipped|not_applicable|unknown", "source": "user_report"}
```

實測：略過後 `status` 仍為 `reported`、`asset_state` 仍為 `suspect`，只有 `saddle_marker` 與
`version` 變動。**不結案、不改工單狀態、不代表修好、不影響工單處理。**
坐墊動作晚到也沒關係，先收工單即可。

## 5. 工單推進（B 主責，A 可追蹤）

```
POST /api/ops/tickets/{ticket_id}/transition
{"to":"accepted","version":2,"crew":"板橋維修一組","eta":"08:40","actor":"派車端","note":""}
```

- 帶 `version` 做樂觀鎖。舊版本回 **409**：
  `{"error":"version_conflict","current_version":3,"current_status":"accepted","hint":"先 GET 取回最新版本再重送"}`
- 同一個決策重送不會重複推進（實測第二次 409，狀態仍是 `accepted`）。
- 不可倒退（回 400 `backwards`）；推進到同一狀態是 `noop`。
- 推進過的單會標 `manual:true`，**模擬流程不再自動推進它**（避免「修復自動變驗收」）。

## 6. 唯讀端點（GET 一律無副作用）

| 端點 | 用途 |
|---|---|
| `GET /api/ops/contract` | 契約摘要（本文件的機器可讀版） |
| `GET /api/ops/tickets?state=open&pending=0` | 工單清單＋flow 標籤 |
| `GET /api/ops/tickets/{id}` | 單張工單（重連補狀態用） |
| `GET /api/ops/tickets/dedup` | 每張單是靠什麼識別去重的（驗收用） |
| `GET /api/tickets` | C 既有路徑，維持相容 |

回應帶 `clock_source:"replay"` 與 `ts`（回放時鐘）。

## 6b. 派車任務與資源帳（A 追蹤用）

規劃週期（planning cycle）把每一筆承諾記成預約：

| 狀態 | 意義 | 是否占用資源 |
|---|---|---|
| `candidate` | 規劃產生、調度員尚未確認 | 是 |
| `confirmed` | 調度員確認派工 | 是 |
| `in_transit` | 已出車 | 是 |
| `released` | 取消或重算釋放 | **否** |

可用量 ＝ 原始可用量 − 候選 − 已確認 − 在途。重算只釋放**同一尺度的候選**，已確認與在途不會被吃掉。

| 端點 | 說明 |
|---|---|
| `GET /api/ops/cycle` | 唯讀資源帳，逐站列候選／已確認／在途／已釋放 |
| `POST /api/ops/tasks/{id}/confirm` | → confirmed |
| `POST /api/ops/tasks/{id}/dispatch` | → in_transit |
| `POST /api/ops/tasks/{id}/cancel` | → released（真的釋放） |
| `POST /api/ops/tasks/{id}/escalate` | 跨區／逾時由營運端主責 |

任務動作可帶 `version`（任務的 `res_version`），舊版本回 409。

### 跨區與逾時（B 主責，A 追蹤）

```
POST /api/ops/tasks/{id}/escalate
{"plan":"cross_district|accept_delay|divert_only","eta":"08:40","reason":"...","owner":"微笑單車調度中心"}
```

- `cross_district` **沒帶 `eta` 會被擋下（400）**：沒有可派資源就不准生成 ETA。
- `divert_only` 的 `eta` 是 `null`，代表「無可派人車，改民眾分流」。
- 三種方案都會 `notify("gov", ...)` 帶 `{task_id, plan, eta, owner}`。
- **政府端請不要據此另建派車任務**；派工由 B 執行，A 追蹤與協調。

### 每個送車站有自己的服務時限

`stops[]` 裡的 dropoff 會帶 `due_min`／`due_ts`／`on_time`／`late_min`；
任務層有 `late_stops[]`、`on_time_stops`、`tightest_due_min`。
一趟裡只要還有站來得及就照送，趕不上的站標紅並改走人力就近補或分流；
**全部站都趕不上才判定整趟來不及**。

## 7. 給 C 的串接清單

1. 送 `request_id`（重送用同一個），逾時先 `GET /api/ops/tickets/{id}` 查狀態，不要直接重建。
2. `dock_no` 可以照舊送，但建議改 `dock_id`；兩者都會結構化保留。
3. 明確機械問題不必附圖即可送出；不確定時把 AI 結果放 `evidence[]` + `certainty:"observed"`。
4. 坐墊教學顯示在**受理成功之後**，呼叫 `/saddle`；使用者略過不要撤銷工單。
5. 只有 `status` 是工單流程；不要用它表示「使用者已離開」，那是 C 自己的回報狀態。

## 6c. 服務可用性（A 的「官方可借 N／已確認不可用 X／疑似異常 Y」）

```
GET /api/ops/availability?district=板橋區&sid=123&only_flagged=1
```

| 欄位 | 意義 |
|---|---|
| `official_bikes` | 站點快照的可借車數，**未扣除本工具的工單** |
| `known_unusable_bikes` / `_bike_nos` | 本工具有未結案工單且**帶車號**的不同車輛數 |
| `known_unusable_docks` / `_dock_ids` | 同上，車柱 |
| `suspected_tickets` | 證據不足或沒有資產識別的工單，**不計入不可用** |
| `excluded_from_recommendation` | 本工具不再推薦這些設備 |
| `may_need_service_event` | **僅建議旗標**，需人工確認才可開服務中斷事件 |

三條硬界線（端點本身就不做這些事）：
1. **不重扣**：不把工單從官方數字扣掉。沒有庫存介接，不知道官方是否已扣除，兩個數字並列，**不可相加也不可相減**。
2. **不整站判不可用**：只有「已知不可用車數 ≥ 官方可借數且每台都有車號」時才給建議旗標。
3. **不聲稱鎖車**：沒有遠端停租介接，只代表本工具排除推薦。

工單推進到 `verified` 後會自動從不可用名單移除（實測：2 台 → 驗收 1 台 → 剩 1 台）。

## 7b. 跨端接線狀況（2026-09-13 重啟後複驗）

### (1) ~~`api_reset` 沒有清掉 `CREPORTS`~~ — **C 已修（`f8184e1`），複驗全過**

`python3 tests/test_b_reset_crossend.py` 現在 9 項全過：reset 後舊工單與舊回報都回 404、
坐墊標記不留殘影、重送同一個 `request_id` 會建出**全新的單**
（`version=1`、`reports=1`、時間戳是重送當下）。

**複驗時修掉我自己一條錯的判準**：原本用「重送後的 `ticket_id` 應該不同」判定殘影，
但 reset 之後編號從 R001 重新開始，新單本來就會撞到舊 id，這樣根本分不出新舊。
改成「舊 id 要 404、新 id 要解析得到、且 `version/reports` 是全新值、時間戳不同」。

### (2) ~~坐墊標記還沒寫進工單~~ — **C 已改成直接寫權威欄位，複驗通過**

C 端呼叫 `/api/c/report/{rid}/saddle` 之後，工單上的 `saddle_marker` 會直接變成
`{status:"skipped", source:"user_report", ts:"..."}`，而 `status` 仍為 `reported`、
`asset_state` 仍為 `suspect`、`version` 前進——**標記不結案、不代表修好**，符合契約。

`saddle_marker_external` 唯讀橋接保留為 fallback（權威欄位有值時不會出現）。
A 端若看到它，代表那是用戶端回報清單裡的資料、**不是**工單上的已確認資訊。

### (3) `merged` 欄位

C 的程式讀 `tk.get("merged")`，我原本只回 `action`。已補上 `merged: true/false` 相容欄位，
C 不必改碼。

## 8. 給 A 的串接清單

1. 事件台帳用 `ticket_id` 對齊，`GET /api/ops/tickets` 可取得 `crew`、`eta`、`version`、`status`。
2. `evidence[]` 與 `certainty` 區分「用戶自述／AI 觀察／現場確認」，**AI 觀察不可放進「已確認原因」**。
3. 工單推進會 `notify("gov", ...)` 並帶 `{ticket_id, sid, status, version}`。
4. 政府端請**不要**自行建立第二套派車任務；要協調請用追蹤動作，派工由 B 主責。

## 9. 測試證據

| 編號 | 項目 | 方式 | 結果 |
|---|---|---|---|
| C01 | 同 request_id 重送只建一張 | fixture＋HTTP（8789） | **通過** 兩次同 id、工單數 1 |
| C01 | 無圖片可直接建單 | fixture | **通過** `diagnosis=direct_repair` |
| C03 | 略過坐墊不結案 | fixture＋HTTP | **通過** status 仍 reported、asset_state 仍 suspect |
| C03 | 坐墊標記寫進工單權威欄位 | HTTP 閉環（2026-09-13） | **通過** C 端呼叫後 `saddle_marker` 直接生效 |
| — | 三端 reset 一致 | HTTP（2026-09-13 重啟後） | **通過** 9 項，舊工單／回報／坐墊標記都不留殘影 |
| — | only_flagged 列出有故障設備的站 | HTTP（2026-09-13） | **通過** 官方可借 5、已知不可用 1 的站會被列出 |
| I01 | 修復不等於驗收 | HTTP | **通過** recovered→asset_state=repaired，未到 verified |
| I02 | 同站兩台不同車不合併 | fixture＋HTTP（跨兩個入口） | **通過** 兩張單，同車跨入口才合併 |
| I02 | `dock_no`→`dock_id` 結構化保留 | fixture＋HTTP | **通過** 同站不同柱不合併 |
| I03 | GET 無副作用 | HTTP 前後比對 | **通過** |
| N07 | 舊 version 回 409、重送不重派 | HTTP | **通過** |
| — | reset 清掉冪等表與資源帳 | HTTP | **通過** |
| — | 待診斷分流 | HTTP | **通過** `pending=1` 撈得到 |
| N03 | 來源可供 10、兩站各缺 8 | fixture（手算） | **通過** 抽走 10、送出 10、帳上 ≤10、剩餘缺口明示 6 |
| N04 | 同週期重算不重扣 | fixture | **通過** 10→10 而非 20；舊候選標 released |
| N04 | 已確認不被重算釋放 | fixture | **通過** confirmed 保留，合計仍 ≤10 |
| N04 | 取消釋放、GET 無副作用 | fixture＋HTTP | **通過** 取消後歸零；快照重讀不變 |
| N05 | 第二站超時、第一站準時 | fixture | **通過** late_stops 只列遠站、on_time_stops=1、理由不宣稱全部準時 |
| N07 | 任務動作舊版本 409 | HTTP | **通過** |
| — | 無可行 ETA 時擋下跨區支援 | HTTP＋UI | **通過** 後端 400、前端也擋 |
| — | 無人可派改分流不產生假 ETA | HTTP＋UI | **通過** `eta=null` |
| — | 維修派查 → 現場 → 處理 → 驗收 分開 | UI 實操 | **通過** recovered 後 asset_state=repaired 且未結案 |
| — | 1000×770 版面不破 | UI 量測 | **通過** scrollHeight=clientHeight=770，無水平捲動 |
| I01 | C 回報 → B 同一 ticket_id → 派查 → 處理 → 驗收 | HTTP 閉環 | **通過** 19 項，見 `test_i01.py` |
| I01 | 修復不等於自動驗收 | HTTP 閉環 | **通過** recovered→repaired，verified 才 verified_ok |
| I03 | 重連重複 GET 不重複建單 | HTTP 閉環 | **通過** |
| — | 官方可借數不被工單扣掉（不重扣） | HTTP | **通過** 開 2 張工單後官方數仍為 7 |
| — | 無資產識別不計入不可用 | HTTP | **通過** 只列為疑似異常 |
| — | 未確認不整站判不可用 | HTTP | **通過** 僅給建議旗標 |
| — | 逐段載量守恆 | fixture | **通過** 路途中不超上限不為負、回場清空 |
| — | 新動員／在勤改道前置分開 | fixture＋UI | **通過** 15 分 vs 3 分，皆標情境值非實測 |

重現（測試已進版控，見 `tests/README_B.md`）：
```bash
python3 tests/test_b_tickets.py        # 38 項 fixture：建單服務
python3 tests/test_b_ledger.py         # 27 項 fixture：N03/N04/N05 資源帳與逐站期限
python3 tests/test_b_dispatch.py       # 11 項：組趟與跨尺度資源帳
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8789
python3 tests/test_b_http_tickets.py   # 24 項：兩個入口同一服務
python3 tests/test_b_http_cycle.py     # 20 項：規劃週期與任務動作
python3 tests/test_b_closed_loop.py    # 19 項：三端閉環
python3 tests/test_b_availability.py   # 17 項：服務可用性不重扣、不整站判定
```
六套連跑全數 exit 0。**HTTP 套件會呼叫 `/api/reset`，不要對共用的 8787 跑。**

## 10. 尚未驗證／已知限制

### 仍未完成
- **需重啟 8787 才生效**。共用機還是舊碼；所有 HTTP 實測跑在臨時的 8789（測完已關）。
- **`service_state` 只有欄位，恆為 `unknown`**，尚未接到站點恢復判定。
  也就是說「已處理但未恢復」與「已恢復但維修未結案」目前只能靠 `status` 與 `asset_state` 推，
  沒有獨立的服務恢復訊號。
- **`evidence[]` 原樣保留但未做內容驗證**。圖片本身不進 SSE、不進日誌、不進 Git。
- **C 的坐墊標記尚未寫進工單**（見 7b），目前靠唯讀橋接。

### 已完成但要講清楚界線
- **官方已扣除的故障車不重複扣庫存**：`/api/ops/availability` 把官方可借數與本工具的已知不可用
  **並列不相減**，所以不會重扣。但我們也**沒有**「官方是否已扣除」的判定來源——
  這不是已解決的問題，是刻意不做推斷。
- **未確認不整站判不可用**：沒有任何地方會把整站標成不可用；只在「已知不可用車數 ≥ 官方可借數
  且每台都有車號」時給 `may_need_service_event` 建議旗標，仍需人工確認。
- **沒有遠端停租介接**：工單只代表本工具排除推薦，**不會聲稱已鎖車**。
- **前置時間**：新動員 15 分、在勤改道 3 分，**兩個都是情境假設，不是實測**。
  訪談的「決策到抵達 60 分」是總量參考，保留在 `lead_time_min` 給三端顯示。
- **維修班組的數量與位置**來自 `ops_baseline.json` 的情境拆分；公開資料只有全市 350 人總數，
  職務怎麼分沒有來源。
- **派車執行進度是模擬**：出車、抵達、完成不是真實車機回報。
- **快照不是交易**：零車快照不等於有人借不到，工單數不等於實際故障台數。

---

# 下一個 session 接手 B 主線從這裡開始

## 0. 先做這三件事

```bash
cd /Users/chenhongfei/CC/ntpc-youbike
cat docs/WORKSTREAMS.md          # 檔案歸屬與 git 硬規則，先讀
git log --oneline -15 && git status --short
curl -s http://127.0.0.1:8787/api/ops/contract | python3 -m json.tool   # 確認共用機跑的是哪版
```

## 1. B 線擁有哪些檔案

| 類別 | 檔案 |
|---|---|
| 前端 | `app/static/ops.html`、`app/static/ops.js`、`app/static/ops_baseline.json` |
| 後端模組 | `app/tickets.py`（建單服務，純邏輯） |
| 共用檔中的 B 區塊 | `app/server.py` 的 `submit_ticket` / `api_ticket_create` 委派 / `api_reset` 裡的 `TK.SERVICE.reset(); PL.ledger_reset()` / 檔尾「B 線（派車端）附加端點」整塊 |
| 排程 | `app/planner.py` 的 `ASSUMPTIONS`、資源帳（`cycle_*`）、`gaps`、`plan_dispatch`。**`plan_trip` 是 C 的，不要碰** |
| 文件與測試 | `docs/HANDOFF_B.md`、`docs/DATA_SOURCES.md`、`tests/test_b_*.py`、`tests/README_B.md` |

**不要改**：`gov.html`、`metrics.py`、`events.py`、`docs/METRICS.md`、`docs/HANDOFF_A.md`（A 的）；
`citizen.html`、`sw.js`、`manifest.webmanifest`、`icons/`、`report_image.py`、`docs/REWARDS.md`、
`docs/HANDOFF_C.md`、`tests/test_c_*.py`（C 的）。`pipeline/`、`models/`、`data/processed/`、
`predict.py` 凍結。

## 2. 提交共用檔的作法（踩過坑，照做）

`app/server.py` 三線都在改。**絕對不要 `git add app/server.py` 就了事**——2026-09-12 發生過
一次，某線 commit 時掃走政府端還沒寫完的區塊，導致 main 上 NameError。

```bash
git diff -U0 app/server.py | grep "^@@"      # 先看有幾個 hunk
git diff app/server.py > /tmp/full.patch     # 再逐 hunk 確認哪些是自己的
# 只留自己的 hunk 存成 mine.patch，然後：
git apply --cached --check /tmp/mine.patch && git apply --cached /tmp/mine.patch
git show :app/server.py > /tmp/staged.py && python3 -c "import ast;ast.parse(open('/tmp/staged.py').read())"
```
確認暫存版語法過、且不含別人的新東西，才 commit。

## 3. 現在做到哪

已完成並驗證（`tests/` 共 8 套，B 自己的 7 套全過）：
建單單一入口、資產識別去重、`request_id` 冪等、四種狀態分離、坐墊標記、
顯式規劃週期資源帳（候選／已確認／在途／已釋放）、逐站服務時限、
前置拆成新動員 15 分與在勤改道 3 分、逐段載量守恆、服務可用性彙總、
維修派查 UI、跨區逾時主責、三端閉環 I01。

## 4. 接手後第一件事：重啟 8787 並複驗兩項

**2026-09-13 已重啟並複驗完畢，這一節保留作為下次重啟的操作範本。**
當時補驗的兩項都已通過：`only_flagged` 修正（有已知不可用設備但未達整站門檻的站會列出來了）、
跨端 reset 一致（9 項全過）。八套測試對 8787 連跑全數 exit 0。

```bash
# 重啟前一定要在 chat 喊一聲，會清掉三端的回放狀態
lsof -nP -iTCP:8787 -sTCP:LISTEN            # 找 pid，只關這個，別碰 8788/8791
kill <pid> && python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8787 &
# 約 45 秒載入完成後：
YB_BASE=http://127.0.0.1:8787 python3 tests/test_b_reset_crossend.py   # 應該從 3 fail 變全過
curl -s "http://127.0.0.1:8787/api/ops/availability?only_flagged=1" | python3 -m json.tool | head -30
# 跑完把情境復原：
curl -X POST http://127.0.0.1:8787/api/scenario/commute_am -H 'content-type: application/json' -d '{}'
```

## 5. 還沒做的（依重要性）

1. **`service_state` 恆為 `unknown`**，沒接站點恢復判定。「已處理但未恢復」「已恢復但維修未結案」
   目前只能從 `status` 與 `asset_state` 推。這是驗收矩陣裡明列的差異項。
2. **`evidence[]` 未做內容驗證**（C 送什麼就存什麼）。
3. 人力調度任務目前只能從「缺車站」分頁手動建，沒有自動從 `minor_gap` 產生。
4. `ops_baseline.json` 的職務拆分（車組 80／人力 200／維修 70）是情境假設，公開資料只有
   全市 350 人總數——**介面上已標示，不要偷偷把它講成實測**。

## 6. 測試

```bash
python3 tests/test_b_tickets.py       # 38 項 fixture，不需伺服器
python3 tests/test_b_ledger.py        # 32 項 fixture，不需伺服器
python3 tests/test_b_dispatch.py      # 11 項，需要伺服器提供真實站況
# 需要伺服器的，預設打 8789；要對 8787 跑要設 YB_BASE，而且會 /api/reset
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8789 &
python3 tests/test_b_http_tickets.py tests/test_b_http_cycle.py tests/test_b_closed_loop.py
python3 tests/test_b_availability.py
python3 tests/test_b_reset_crossend.py
```
**每個 server 吃約 1.5GB，測完記得關。**

## 7. 講話的界線（介面與文件都要守）

- 派車執行進度、車與人的位置、現場回報內容、車隊編制拆分 → **模擬／情境**，不能講成實測。
- AI 圖片觀察是**證據**，不是現場驗收；不可放進「已確認原因」。
- 建單不等於已確認根因、不等於遠端停租、不等於官方庫存已扣除、不等於安全復役。
- 沒有可派資源時**不准生成 ETA**（`cross_district` 沒帶 `eta` 後端會回 400）。
- 官方可借數與本工具的已知不可用**並列不相減**，兩者不可相加。
- 快照零車不等於有人借不到；工單數不等於實際故障台數。
