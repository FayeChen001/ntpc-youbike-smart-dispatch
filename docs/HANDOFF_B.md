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

## 7. 給 C 的串接清單

1. 送 `request_id`（重送用同一個），逾時先 `GET /api/ops/tickets/{id}` 查狀態，不要直接重建。
2. `dock_no` 可以照舊送，但建議改 `dock_id`；兩者都會結構化保留。
3. 明確機械問題不必附圖即可送出；不確定時把 AI 結果放 `evidence[]` + `certainty:"observed"`。
4. 坐墊教學顯示在**受理成功之後**，呼叫 `/saddle`；使用者略過不要撤銷工單。
5. 只有 `status` 是工單流程；不要用它表示「使用者已離開」，那是 C 自己的回報狀態。

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
| I01 | 修復不等於驗收 | HTTP | **通過** recovered→asset_state=repaired，未到 verified |
| I02 | 同站兩台不同車不合併 | fixture＋HTTP（跨兩個入口） | **通過** 兩張單，同車跨入口才合併 |
| I02 | `dock_no`→`dock_id` 結構化保留 | fixture＋HTTP | **通過** 同站不同柱不合併 |
| I03 | GET 無副作用 | HTTP 前後比對 | **通過** |
| N07 | 舊 version 回 409、重送不重派 | HTTP | **通過** |
| — | reset 清掉冪等表與資源帳 | HTTP | **通過** |
| — | 待診斷分流 | HTTP | **通過** `pending=1` 撈得到 |

重現：
```bash
python3 /private/tmp/.../scratchpad/test_tickets.py    # 38 項 fixture 單元測試
python3 /private/tmp/.../scratchpad/test_http.py       # 24 項 HTTP 整合測試（需 8789 測試機）
```
（測試檔在 scratchpad，未進版控；要保留再說。）

## 10. 尚未驗證／已知限制

- **需重啟 8787 才生效**。目前共用機還是舊碼；以上實測跑在臨時的 8789。
- `service_state` 目前只有欄位與預設值，尚未接到站點恢復判定。
- 沒有遠端停租介接：工單只會標記「本工具排除推薦／待查」，**不會聲稱已鎖車**。
- 官方已扣除的故障車不重複扣庫存——這條在資源帳那邊處理，本輪尚未完成。
- `evidence[]` 原樣保留但未做內容驗證；圖片本身不進 SSE、不進日誌、不進 Git。
