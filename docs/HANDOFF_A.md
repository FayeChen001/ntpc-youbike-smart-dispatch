# HANDOFF_A — 政府端（A 主線）第二輪交付

基準 `a1c6dbb`。本輪 commit：`12ca2d8`、`9d64929`，以及本文件所屬的接線 commit。
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
| 事件台帳與原因時間線（第一輪） | 沿用 | `app/events.py` |
| 驗收指標面板（第一輪） | 沿用並修正口徑 | `app/metrics.py` |

## 二、API 差異

| 端點 | 變更 |
|---|---|
| `GET /api/ledger` | 新增 `overview`（供需／新鮮度／設備／任務）、`actions`、`acked_not_assigned`、`ack_note`；事件帶 `version` |
| `GET /api/ledger/{id}` | 回傳完整事件含 `version`、`timeline`、`observed`（`span_min`／`upper_min`／`gap_inside`） |
| `POST /api/ledger/{id}/action` | **新增**。`{action, version?, request_id?, owner?, note?, districts?}`，動作為 `ack`／`assign`／`request_ops`／`track`／`coordinate`／`note`。版本過期回 **409**，同 `request_id` 重送回上次結果不重做 |
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
| **I01 三端同事件閉環** | **未驗證** | 只驗到「B 建單 → A 顯示同一 ticket 與狀態差異」。C 的 `report_id` → B 的 `ticket_id` → A 三欄的完整串接尚未跑過 |
| **AI 圖片觀察實際顯示** | **未驗證** | C 的 `report_image.py` 尚未把 `evidence` 裡的圖片辨識結果寫進工單。A 端已實作讀取與降級，但沒有真資料跑過 |
| **B 端是否顯示政府端要求** | **未驗證** | A 已送出帶 `event_id`／`event_version` 的 ops 通知並確認進入 `/api/notifications?channel=ops`；B 畫面是否呈現、是否回填處理方案，需 B 確認 |
| **task 的 `version`／`cycle_id`／`event_id`／`operator_owner`／`uncovered_gap`** | **阻擋** | `GET /api/ledger` 的 `overview.tasks.contract_missing` 會如實列出。A 顯示「未提供」，不自行推算 |
| **`GET /api/ops/cycle` 的資源帳** | **未串接** | B 已有端點，A 尚未把 planning cycle 與候選/確認/在途顯示到儀表板 |
| **獨立把關兩輪** | **未執行** | 本文件只是 A 端自測，不等於整合驗收 |
| **真實背景推播** | **不做** | 依任務書本輪接受明示模擬 |

### 需要 B 提供

1. task 的 `version`、`cycle_id`、`event_id`（回連 A 事件）、`operator_owner`、逐站 `uncovered_gap`
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
