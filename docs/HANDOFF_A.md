# HANDOFF_A — 政府端（A 主線）第二輪交付

基準 `a1c6dbb`。本輪 commit：`12ca2d8`、`9d64929`、`320547c`、`7bca386`、`381de3e`、`78fd521`、`c523bc0`。
時間戳一律台灣時間；畫面上的時間是**回放時鐘**（2026 年 1–6 月歷史快照），不是真實時鐘。

## 一、這輪做了什麼

| 項目 | 狀態 | 檔案 |
|---|---|---|
| 數值口徑修正（觀測跨度／未知上界／候選量／ack≠指派） | 完成並測 | `app/metrics.py`、`app/events.py` |
| 事件版本、request_id 冪等、舊版本 409 | 完成並測 | `app/events.py`、`app/server.py` |
| 政府端動作（追蹤／要求處理／跨區協調），不建立派車任務 | 完成並測 | 同上 |
| 全域儀表板（供需／資料新鮮度／設備待查／任務與未覆蓋缺口／責任進度） | 完成 | `app/events.py`、`app/static/gov.html` |
| 設備事件三欄分開（用戶描述／AI 觀察／現場確認） | 完成（AI 欄位待 C 提供） | 同上 |
| 狀態差異判定（已處理未恢復／已恢復未結案／仍是疑似／無資產識別） | 完成並測 | 同上 |
| 通知情境預覽（明示模擬）＋深連結 | 完成並測 | `app/static/gov.html` |
| 缺車/dropoff、缺位/pickup 分開判定覆蓋 | 完成並測（含真資料） | `app/events.py` |
| 服務可用性（消費 B 的 `/api/ops/availability`） | 完成 | `app/server.py`、`app/static/gov.html` |
| 任務欄位對齊 B（cycle／reservation_state／逐站期限） | 完成 | `app/events.py`、`app/static/gov.html` |
| **主管決策卡**（方案只採營運端資料、決策留痕、防雙重派工） | 完成並測 | `app/events.py`、`app/static/gov.html` |
| **I01 三端閉環驗證**（C 回報 → B 工單 → A 顯示 → 修復 → 驗收） | 完成並測 | `tests/test_a_i01_loop.py` |
| **AI 圖片觀察欄位**（對齊 C 的 evidence 種類，觀察與不確定分開） | 完成並測 | `app/events.py`、`app/static/gov.html` |
| **營運端資源帳**（候選／確認／在途／已釋放） | 完成 | `app/events.py`、`app/server.py`、`app/static/gov.html` |
| 事件台帳與原因時間線（第一輪） | 沿用 | `app/events.py` |
| 驗收指標面板（第一輪） | 沿用並修正口徑 | `app/metrics.py` |

## 二、API 差異

| 端點 | 變更 |
|---|---|
| `GET /api/ledger` | 新增 `overview`（供需／新鮮度／設備／任務）、`actions`、`acked_not_assigned`、`ack_note`；事件帶 `version` |
| `GET /api/ledger/{id}` | 回傳完整事件含 `version`、`timeline`、`observed`（`span_min`／`upper_min`／`gap_inside`） |
| `POST /api/ledger/{id}/action` | **新增**。`{action, version?, request_id?, owner?, note?, districts?, option?, supersede?}`，動作為 `ack`／`assign`／`request_ops`／`track`／`coordinate`／`note`／`decide`。版本過期回 **409**，同 `request_id` 重送回上次結果不重做 |
| 事件物件 | 新增 `decision_options`（P0／P1 才有，由 `refresh` 算好）、`decisions[]`；`_summary` 帶 `decision` |
| `POST /api/ledger/{id}/assign`、`/note` | 相容保留，內部改走同一個 `apply_action`，不是第二套實作 |
| `GET /api/metrics/service` | 欄位改名：`counts.span_ge*`、`upper.{known,unknown,ge*,gap_inside}`、`zero_snapshot_station_min`、`duration.*`、`longest[].span_min`。**移除** `lower_min`、`counts_upper`、`over_target_station_min_upper` |
| `GET /api/metrics/threshold` | `alerts` → `candidate_cells`，新增 `unit`、`dedup_warning` |
| `GET /api/metrics/flow` | funnel 新增 `assigned`、`acked_not_assigned`、`unacked_ratio`；`unassigned_ratio` 改以 owner 計算；新增 `definitions` |

政府端動作**一律不建立也不修改派車任務**；`request_ops` 與 `coordinate` 只送通知給營運端，
通知 payload 帶同一個 `event_id` 與 `event_version`。

## 三、測試結果

### 手算 fixture（期望值寫在測試檔內，不取被測程式輸出）

```bash
python3 tests/test_a_numeric.py    # 42 項
python3 tests/test_a_actions.py    # 38 項
```

| 編號 | 內容 | 結果 |
|---|---|---|
| N01 | 08:00／08:30／09:00 三筆零快照 → 觀測跨度 **60 分**，零值快照 3 筆＝90 站‧分鐘；欄位不得叫 `lower_min` | **通過** |
| N02 | 零→缺測→零、前後無確認正常觀測 → `upper_min` 為 `None`，且不等於 `span+30` | **通過** |
| N02 | 正常→缺測→零零→正常 → 上界手算 120 分且標記 `gap_inside` | **通過** |
| N02 | 上界分佈只統計已知者，未知件數另列 | **通過** |
| N07 | ack 只寫 `acked`，`owner`／`assigned_at` 仍為空；未指派比例以 owner 計（手算 0.667，若誤用 ack 會是 0.333） | **通過** |
| — | 候選觸發量欄位改名並標示不是通知量、不提供通知量估計 | **通過** |
| — | 政府端動作後任務清單 `repr` 完全未變 | **通過** |
| — | 400 防呆（無 owner／空白紀錄／未知動作）後版本不變 | **通過** |

### HTTP 實測（port 8788，回放時鐘 2026-06-16）

| 編號 | 動作 | 結果 |
|---|---|---|
| N07-1 | `POST /action {"action":"ack","version":1}` | 200，`acked=2026-06-16 10:30`、`owner=None`、`assigned_at=None`、version 1→2 **通過** |
| N07-2 | 同 `request_id=REQ-1` 送兩次 `request_ops` | `ops_requests` 1 筆、時間線 `request_ops` 1 次 **通過** |
| N07-3 | 帶 `version:1`（已是 3）送 assign | **HTTP 409**，`expected_version=3`、`your_version=1`，事件未變 **通過** |
| 跨端 | 上述 `request_ops` 後查 `GET /api/notifications?channel=ops` | 營運端收到「政府端要求處理」，`extra.event_id=54496d77`、`event_version=3` **通過** |
| I03 | 連續 6 次 `GET /api/ledger` 後查單一事件 | version 與 timeline 筆數皆不變（1／3 筆）；`tasks` 數量 31→31 **通過** |
| I03 | `GET /api/alerts?all=1` | 回傳含已解除事件共 144 筆，重連可補回 **通過** |
| I02（A 端可見部分） | 同站兩台不同車號建單 | R001／R002 未合併；同車號重送合併為同一張 **通過** |
| A-5 | 將 R001 推進到 `recovered` | A 端顯示差異「已處理但服務未恢復」；另偵測到 R003「沒有資產識別」 **通過** |
| A-3 | 通知情境卡點擊 | 關閉手機框並開啟該事件抽屜；`/gov?event=<id>` 深連結亦可 **通過** |

### 第二批（`7bca386`）

```bash
python3 tests/test_a_coverage.py   # 15 項
```

| 項目 | 結果 |
|---|---|
| 缺車事件只認 dropoff 任務、缺位事件只認 pickup 任務，兩者不互認 | **通過** |
| 兩種任務並存時各自對到正確那一張 | **通過** |
| 缺位理由文字用「運出騰位」、缺車用「送車補給」，但書仍保留 | **通過** |
| 真資料：回放時鐘設到 `2026-06-01 01:30`（該時點有連續滿站），8 件缺位事件全部 `cover_action=pickup` | **通過** |
| 真資料：分級理由可解釋（捷運竹圍站替代站 0 且無計畫→P0；新市一路兩站站群失效→P0） | **通過** |
| 服務可用性由 `/api/ops/availability` 取得，A 不另算（實測 新北市立圖書館三重分館 官方可借 0、已確認不可用 2 台 YB-A1／YB-A2、`may_need_service_event=true`） | **通過** |
| 任務欄位讀到 B 的 `cycle`（5 個 cycle）、`reservation_state`（candidate 20）、逐站期限（準時 26／來不及 55） | **通過** |

### 第三批 — 主管決策卡（`381de3e`）

```bash
python3 tests/test_a_decision.py   # 38 項
```

| 項目 | 結果 |
|---|---|
| 方案只採營運端既有任務；沒有任務時兩個方案都標未提供，且不得帶 eta | **通過** |
| 跨區任務的代價照抄營運端理由，ETA 保持 `None` 並說明政府端不自行估算 | **通過** |
| 決策必須帶方案（不接受自由輸入）與理由，缺任一回 400 且版本不變 | **通過** |
| 決策留痕保存方案、理由、事件版本、營運端任務版本、時間 | **通過** |
| 已有有效決策時再決策回 **409** 並附既有決策；409 後仍只有一筆決策 | **通過** |
| 帶 `supersede` 才能改決策，舊決策標記 `superseded` 並記錄取代原因 | **通過** |
| 同 `request_id` 重送決策為冪等，決策筆數不變 | **通過** |
| 決策不得動到營運端任務（比對 `repr` 完全未變） | **通過** |
| HTTP 實測：送出決策 200 → 重複 409 → 重送冪等 → `tasks` 數 34 未變 | **通過** |
| 瀏覽器實測：卡片渲染，顯示營運端真實代價（「2 站趕不上自己的服務時限，最緊 30 分鐘」）與「跨區支援 未提供」 | **通過** |

### 第四批 — I01 三端閉環（`78fd521`）

```bash
YB_BASE=http://127.0.0.1:8788 python3 tests/test_a_i01_loop.py   # 41 項，打真實 HTTP
```

從 A 的視角跑完整閉環，不繞過任何一端的實作。測試對伺服器既有狀態無依賴
（每次用唯一車號），連跑兩次都 ALL PASS。

| 步驟 | 驗到什麼 | 結果 |
|---|---|---|
| 1 | C 送出機械症狀回報 → 直接開出工單，回傳 `report_id` 與 `ticket_id` | **通過** |
| 1b | 同 `request_id` 重送 → 冪等，仍是同一個 `ticket_id`，不重複建單 | **通過** |
| 2 | A 端看到同一個 `ticket_id`、車號、`dock_id`；三欄分開（①有②無③無）；設備＝疑似故障、服務＝未知；`report_ids` 有回連 | **通過** |
| 3 | B 推進 `accepted`→`on_site`：A 端流程狀態同步、看到營運負責人、③現場確認出現 | **通過** |
| 4 | B 推進 `recovered`：設備＝已維修但**不是驗收正常**、服務仍未知、A 端標出「已處理但服務未恢復」 | **通過** |
| 5 | B 推進 `verified` 才是 `verified_ok` | **通過** |
| 6 | 全程同一個 `ticket_id` 只出現一次，去重依據是車號 | **通過** |
| 7 | 同站不同車另開一張單，A 端兩張並存（I02） | **通過** |
| 8 | 重複 GET `/api/ledger` 後工單列數與事件數不變（I03） | **通過** |
| AI | 附圖回報：AI 欄顯示觀察兩項、「照片無法確認」獨立一項、標示模型來源、附界線說明；設備狀態仍是疑似（**AI 推測不得升格為已確認**） | **通過** |

### 第五批 — 營運端資源帳（`c523bc0`）

直接呼叫 `GET /api/ops/cycle`，政府端只呈現不重算。實測 cycle `CY-2026-06-16T07:00-1`：
預約 68、候選 341 輛、需求站 45／供給站 10。畫面上寫明候選／確認／在途／已釋放
四者都不代表車已經到站。

### 瀏覽器實測

- `node --check` 對 `gov.html` 的 inline script 語法檢查：**通過**
- 全域儀表板四個區塊渲染、三欄證據 3 張、狀態差異警示 2 則：**通過**

## 四、修正過的自有缺陷

1. **`upper_eff = lower + 30`**（第一輪 `b717fe5` 引入）：捏造未知上界，違反 N02。已移除。
2. **六處跳脫反引號**（本輪 `12ca2d8` 引入）：`gov.html` 整頁 JS 無法解析，等於政府端全白。
   由瀏覽器 console 抓到，已修並把 `node --check` 納入每次驗證流程。
3. **分級促級不冪等**（第一輪）：站群促級寫在留痕之後，導致每次重算都記一次假的 `P0→P1`。
   改成三段式：先算基準級 → 再認定站群 → 最後定案並留痕。
4. **站群以行政區認定**（第一輪）：一個行政區一兩百站，三件同級就全升 P0（29 件全 P0）。
   改以 500 公尺鄰站關係認定後降為 4 件。

## 四之二、發現的共用程式缺陷（不是 A 的檔案，未自行修改）

**`app/server.py` 的 `progress_tickets()` 在回放時鐘往回跳時會 `IndexError`，整個 `on_tick` 回 500。**

```python
steps = int((now_ts() - pd.Timestamp(tk["ts"])).total_seconds() / 1800)
want = TICKET_FLOW[min(len(TICKET_FLOW) - 1, steps)]   # steps 為負數時索引越界
```

重現：先建立任何工單，再 `POST /api/clock {"action":"set","ts":"<比工單建立時間更早的時刻>"}`。
時鐘實際上會前進，但回應是 500，且該次 tick 的後續步驟（`check_reminders` 等）不會執行。

影響三端。修法是把 `steps` 夾在 0 以上（例如 `max(0, steps)`），或在時鐘回捲時重置工單時間戳。
依分工規則這屬於共用邏輯，我沒有自行修改，請 B 或當輪整合者處理。
我的驗證改以「重啟後先設時鐘、再建工單」繞過。

## 四之三、環境觀察（未判定為程式缺陷）

`/gov`、`/ops`、`/citizen` 三頁在預覽瀏覽器都出現同一則 console 錯誤
（`SyntaxError: missing ) after argument list` 加上「An unknown error occurred when fetching the script」），
且 `navigator.serviceWorker.getRegistrations()` 回傳 0 筆。

已排除的可能：`app/static/sw.js`、`common.js` 與 `gov.html` 的 inline script 都通過 `node --check`；
伺服器送出的 `sw.js` 與磁碟內容一致。三端同時出現、且完全沒有 SW 註冊成功，
比較像是預覽瀏覽器沙箱擋掉 Service Worker 註冊。**沒有證據指向任何一端的程式缺陷，也沒有排除**，
需要在一般瀏覽器再測一次才能判定。頁面功能本身不受影響（DOM 實測正常）。

## 五、模擬與界線（畫面上都有標示）

- **通知情境預覽是應用內示意**。沒有 Web Push、沒有 APNs、沒有背景推播；關掉分頁不會收到任何東西。
  畫面上直接寫明「不是真的 iOS 鎖定畫面」。
- **派工進度是模擬**：出車、抵達、完成由假設推進，非真實派工系統。
- **時鐘是回放**：`overview.clock_source = "replay"`，畫面標示「不是真實時鐘」。
- **AI 圖片觀察是推測**：獨立一欄，不寫進「已確認原因」；C 尚未提供時顯示「未提供」與原因。
- **坐墊標記**只是給下一位使用者的現場提醒，不代表維修完成，也不結束工單。
- **不能宣稱**：零值快照不等於有人借不到；跨度不等於連續中斷；候選觸發量不等於通知量；
  ack 不等於指派；任務清單沒有這一站只代表排程沒涵蓋，**不能推論沒有人到過現場**
  （本系統沒有巡查或人員定位紀錄）。

## 六、未驗證 / 阻擋

| 項目 | 狀態 | 說明 |
|---|---|---|
| ~~I01 三端同事件閉環~~ | **已驗證**（`78fd521`） | 見第四批。41 項全過，可重複執行 |
| ~~AI 圖片觀察實際顯示~~ | **已驗證**（`78fd521`） | 以 C 的 `image_analysis` 介面送入測試樁資料驗過欄位切分。**尚未用真實 Bedrock 視覺模型跑過** |
| **真實視覺模型** | **未驗證** | 上述測試用的是測試樁；C 的 `report_image` 接真模型後需再驗一次 |
| **B 端是否顯示政府端要求** | **未驗證** | A 已送出帶 `event_id`／`event_version` 的 ops 通知並確認進入 `/api/notifications?channel=ops`；B 畫面是否呈現、是否回填處理方案，需 B 確認 |
| **task 的 `version`／`event_id`／`operator_owner`** | **阻擋（已縮小）** | B 已提供 `cycle`、`reservation_state`、`late_stops`、`on_time_stops`，A 已接。剩下三個欄位 `overview.tasks.contract_missing` 會如實列出，A 顯示「未提供」不自行推算 |
| **`app/server.py` 的 availability 接線** | **未提交** | 該檔目前有 C 主線未提交的改動，依規則不能 `git add`。介面對 availability 缺失有降級處理。待 C 提交後我再串行提交這一段 |
| **獨立把關兩輪** | **未執行** | 本文件是 A 端自測加一條真實三端閉環，仍不等於獨立把關 |
| **B 端畫面是否呈現政府端決策** | **未驗證** | A 的決策留在事件上並送 ops 通知，B 是否顯示需 B 確認 |
| **真實背景推播** | **不做** | 依任務書本輪接受明示模擬 |

### 需要 B 提供

0. **改派方案端點**（決策卡最大的缺口）：就近車隊改道與跨區支援各自的抵達時間、原任務延後幾分鐘、
   增加里程、車源來自哪一區、受影響站點。政府端不會自行估算，沒有這支端點決策卡只能顯示既有任務。
1. task 的 `version`、`event_id`（回連 A 事件）、`operator_owner`
2. `POST /api/ops/tickets` 接受並保存 `event_id`，讓 A 能把工單掛回事件
3. 工單 `eta` 實際填值（目前多為 `null`）

### 需要 C 提供

4. 工單 `evidence[]` 內放入圖片辨識結果，`kind` 用 `image` 或 `image_observation`，
   每筆至少有 `text`；另建議帶 `model_source`、`requires_manual_review`、`uncertainties[]`
5. `report_id` 寫進工單 `report_ids[]`，讓 A 能從事件追到原始回報
6. 對外輸出統一 `dock_id`（輸入仍相容 `dock_no`）

## 七、重現步驟

```bash
export PATH=$HOME/Library/Python/3.9/bin:$PATH
python3 tests/test_a_numeric.py
python3 tests/test_a_actions.py
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8787
```

開 `http://127.0.0.1:8787/gov`，把回放時鐘往前推幾步讓事件累積，然後：

1. 側欄「事件台帳」→ 點任一事件「詳情」→ 看觀測跨度、可能上界、原因與證據、時間線
2. 頁首「全域儀表板」→ 四個區塊；設備待查需先有未結案工單
3. 頁首「通知情境」→ P0 通知卡 → 點卡片 → 直接開該事件
4. 頁首「驗收指標」→ 期間／類型／預測尺度／值班可處理量

指標來源：`reports/model_eval.json`（六月測試期一次評估）、`data/processed/` 的回放矩陣、
`reports/ANALYSIS.md`（500 公尺替代站比例）。定義見 `docs/METRICS.md`。

---

# 接手段（2026-09-13 更新）

## 1. 現況

A 主線本輪要做的都做完了，對目前 main 重驗過：五套測試全過（離線 fixture 四套不需伺服器，
`test_a_i01_loop.py` 需要伺服器）。8787 重啟復原後以唯讀 GET 確認跑的是最新程式：
台帳帶 `version`、`overview` 八個區塊齊全、資源帳 `available: true`、契約缺口如實回報。

**沒有我能獨立推進的待辦了**，剩下的都卡在別人的契約或需要授權碰共用檔。

## 2. 檔案歸屬（A 的東西只有這些）

```
app/events.py          事件台帳、分級、原因與證據、決策卡、全域儀表板資料層
app/metrics.py         驗收指標（服務中斷事件、告警門檻取捨、流程時效）
app/static/gov.html    政府端整頁
tests/test_a_*.py      五套測試
docs/METRICS.md        指標定義
docs/HANDOFF_A.md      本檔
app/server.py          只有 `# A 主線（政府端）` 那一段與 api_ledger 內的兩行接線
```

**動 `app/server.py` 前一定要先 `git diff app/server.py` 逐 hunk 確認。**
這個工作目錄三個 session 共用，path-scoped `git add` 對共用檔沒有保護作用——
本輪已經發生過兩次「我的區塊被別人連帶提交」，其中一次讓 main 上的 `/api/ledger` 直接 NameError。
若別人有 staged 未 commit 的檔案，用 `git commit -- <你的路徑>`（新檔要先 `git add`）。

## 3. 測試

```bash
export PATH=$HOME/Library/Python/3.9/bin:$PATH
# 離線，不需伺服器
python3 tests/test_a_numeric.py     # 42 項　數值口徑 N01/N02/ack≠指派
python3 tests/test_a_actions.py     # 38 項　版本、冪等、409、不建任務
python3 tests/test_a_coverage.py    # 15 項　缺車 dropoff／缺位 pickup
python3 tests/test_a_decision.py    # 38 項　決策卡、防雙重派工
# 需要伺服器。會建測試工單，別對 8787 跑
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8788 &
YB_BASE=http://127.0.0.1:8788 python3 tests/test_a_i01_loop.py   # 41 項　三端閉環
```

期望值一律寫在測試檔內手算，不取被測程式輸出。`test_a_i01_loop.py` 每次用唯一車號，
可重複執行。**每個 server 吃約 1.5GB，測完記得關。**

改 `gov.html` 後一定要驗語法，本輪被這個坑過一次（整頁 JS 掛掉還進了 commit）：

```bash
python3 -c "import re;s=open('app/static/gov.html',encoding='utf-8').read();open('/tmp/g.js','w').write(re.findall(r'<script>(.*?)</script>',s,re.S)[-1])" && node --check /tmp/g.js
```

## 4. 還沒做的（依重要性）

1. **主管決策卡的方案比較不完整** — 卡在 B 沒有改派方案端點（`/api/ops/contract` 仍是 `b-1`，
   只涵蓋工單）。需要 B 提供：就近改道與跨區支援各自的抵達時間、原任務延後幾分鐘、增加里程、
   車源來自哪一區、受影響站點。**政府端不會自行估算，沒有這支端點卡片只能顯示既有任務。**
2. **task 缺 `version`／`event_id`／`operator_owner`** — `overview.tasks.contract_missing` 會如實列出。
3. **真實視覺模型未驗** — AI 欄位是用 C 的 `image_analysis` 介面送測試樁資料驗的，
   C 接上 Bedrock 後要再驗一次。
4. **B 端是否呈現政府端的決策與要求未驗** — A 這邊送出並留痕了，也確認進入 ops channel，
   B 畫面需 B 確認。
5. **獨立把關兩輪未執行** — 本檔是 A 端自測加一條真實三端閉環，不等於獨立把關。

## 5. 仍未修的共用程式缺陷（已回報三次，不是 A 的檔案）

`app/server.py:269-270` 的 `progress_tickets()`，回放時鐘往回跳時 `steps` 為負數，
`TICKET_FLOW[min(len-1, steps)]` 索引越界，整個 `on_tick` 回 **HTTP 500**。
截至 2026-09-13 仍是原樣（已再次以程式碼確認）。

重現：先建任何工單，再 `POST /api/clock {"action":"set","ts":"<比工單建立時間更早的時刻>"}`。
時鐘會前進但回應 500，該次 tick 的後續步驟不執行。**demo 當天有人把時鐘往回拉就會踩到。**
修法一行 `steps = max(0, steps)`。屬共用邏輯，依分工規則我沒有自行修改。

另有一個環境現象未判定：三端頁面在預覽瀏覽器都出現同一則 SyntaxError 且
`navigator.serviceWorker.getRegistrations()` 回 0 筆。已排除 `sw.js`／`common.js`／`gov.html`
的語法問題（都過 `node --check`，伺服器送出的與磁碟一致），比較像預覽瀏覽器沙箱擋掉 SW，
但**沒有證據下定論**，需在一般瀏覽器再測。

## 6. 講話的界線（介面與文件都要守）

- **零值快照跨度不是確定的連續中斷時間**。08:00／08:30／09:00 三筆零快照的跨度是 60 分鐘，
  不能說成確定連續 60 分鐘，更不是 90 分鐘。
- **未知上界就是未知**，不准用跨度加一格之類的數字代替。
- **候選觸發量不是通知量**。去重後的通知量無法從校準分箱推算，所以不提供估計值。
- **ack 不等於指派**。未指派比例以 `owner` 計，不用 ack 代替。
- **任務清單沒有這一站，只代表排程沒涵蓋**，不能推論沒有人到過現場——本系統沒有巡查或人員定位紀錄。
- **AI 圖片觀察是推測**，獨立一欄，不進「已確認原因」；「照片無法確認」要跟觀察分開顯示。
- **工單流程、服務是否恢復、設備是否修好是三件事**。已處理不等於服務恢復；站點恢復有車也不等於那台壞車已修好或驗收。
- **官方可借與本工具的已知不可用並列不相減**，兩者不可相加。
- **政府端不建立也不修改派車任務**，不生成任何 ETA——沒有真實車隊位置、班表與載量，算出來的都是虛構的。
- **通知情境是應用內示意**，不是 iOS 鎖定畫面，沒有 Web Push／APNs，關掉分頁不會收到任何東西。
- 快照零車不等於有人借不到，也不等於失敗旅次；候選鄰站存在不等於使用者走得到。
