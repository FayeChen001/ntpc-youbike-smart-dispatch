# HANDOFF_C：用戶端第二輪交付

基準 `a1c6dbb`，第二次更新基準 `6b7eba5`（已接上 B 的契約 `b-1`）。本輪只動 C 歸屬檔案與 `app/server.py` 的 C 區塊。測試伺服器用 8791；共用的 8787 由使用者重啟，**我沒有自行重啟**。

## 一、改了哪些檔案

| 檔案 | 歸屬 | 動作 |
|---|---|---|
| `app/report_image.py` | C（新增） | Bedrock 視覺判讀與降級策略 |
| `app/server.py` 第 1083 行起的 C 區塊 | 共用檔的 C 區塊 | 改寫為按鈕直報、request_id 冪等、坐墊標記、圖片端點 |
| `app/static/citizen.html` | C | 通報流程改為按鈕直送、照片輔助、受理後坐墊教學、進度追蹤 |
| `app/static/sw.js` | C | `notificationclick` 依角色路由，帶 `focus` 深連結 |
| `tests/test_c_round2.py` | C（新增） | 29 項後端驗收 |

## 二、本輪核心流程

按鈕選問題 → 直接通報 → 不確定才補照片請 AI 判讀 → 後端受理成功才顯示工單號 → 適用時顯示坐墊教學 → 追蹤進度。

六個按鈕：輪胎沒氣／破損、煞車異常、鏈條異常、車身／坐墊異常、借不到／還不了、不確定。照片與文字都非必填。

## 三、建單走單一入口

C 的回報一律呼叫 `api_ops_ticket(...)`，與 `POST /api/ops/tickets` 是同一個實作。上一輪 C 呼叫的是舊的 `api_ticket_create`，與 HTTP 路由走不同實作，本輪已改正。

**仍存在的重複入口**：`POST /api/tickets` → `api_ticket_create` 還在，去重規則是舊的「站點＋問題類別＋2 小時」。C 已不再使用它。是否下架由 B 決定。

## 四、測試結果

命令：`python3 tests/test_c_round2.py`（伺服器 `uvicorn app.server:app --port 8791`）

| 編號 | 項目 | 結果 | 證據 |
|---|---|---|---|
| C01a | 無圖片選輪胎破損可直接受理並建單 | 通過 | ticket=R001 |
| C01b | 同 `request_id` 重送回同一筆 | 通過 | C001 vs C001，`idempotent: true` |
| C01c | 只產生一張工單 | 通過 | 該車號工單數 1 |
| I02a | `dock_no` 正規化為 `dock_id` | 通過 | 送 `dock_no:"07"` → `dock_id:"07"` |
| I02b | 同站不同車不合併 | 通過 | YB2-20001→R002、YB2-20002→R003 |
| C03a | 機械問題適用坐墊提醒 | 通過 | `applicable:true, status:pending` |
| C03b | 略過坐墊後工單仍處理中 | 通過 | ticket status=`reported` |
| C03c | 只記 `marker=skipped`，不動工單 | 通過 | `ticket_unchanged: true` |
| C04a | 借不到／還不了不適用坐墊提醒 | 通過 | `not_applicable` |
| C04b | 借不到不判定車輛故障 | 通過 | suspect=`service`，無工單 |
| C04c | 不適用時拒絕標記完成 | 通過 | HTTP 409 |
| C05a | 還車未確認不宣稱停止計費 | 通過 | 文案含「無法代你停止計費」 |
| C05b | 標記不得發完成獎勵 | 通過 | `no_reward: true` |
| C05c | 還車未確認不引導反轉坐墊 | 通過 | 理由為「先處理還車與計費」 |
| Cr1 | 騎乘中坐墊提醒延後 | 通過 | `status: deferred` |
| Cr2 | 不安全症狀顯示停止騎乘 | 通過 | `safety_stop: true` |
| Cr3 | 坐墊本身損壞不強行轉動 | 通過 | `applicable: false` |
| Cl1 | 離開後工單未結案 | 通過 | ticket 仍存在且非 closed |
| Cl2 | 離開不改回報狀態 | 通過 | 仍為 `routed_repair` |
| I03a | GET 無副作用 | 通過 | 連兩次 7 筆不變 |
| I03b | 重複 GET 不多建工單 | 通過 | 連兩次 5 張不變 |
| N06 | 提早抵達與餘裕手算 | 通過 | 08:50+2+5=08:57，餘裕 3 分 |
| C02a | 模糊圖片不判定正常 | 通過 | observations 為空，無「正常」字樣 |
| C02b | 模糊圖片標為需人工判讀 | 通過 | `requires_manual_review: true` |
| C02c | 非圖片降級而非假成功 | 通過 | `ok:false`，理由「只接受 JPEG 或 PNG」 |
| C02d | 降級時不產生任何觀察 | 通過 | observations=[]，asset=unknown |
| C02e | 未辨識出問題仍可送出 | 通過 | `accepted:true, status:pending_triage` |
| C02f | 未確認的照片不建維修工單 | 通過 | `ticket_id: null` |
| C02g | 模型來源標示為 Bedrock | 通過 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |

**總計 29 項通過、0 失敗。**

## 五、圖片模型真測

視覺模型：Amazon Bedrock Converse，`us.anthropic.claude-haiku-4-5-20251001-v1:0`。帳號可呼叫已實測（Sonnet 4.6、Nova 2 Lite 也可用）。

真實照片（Unsplash 公開圖，四張自行車）判讀結果：

| 輸入 | 觀察 | 資產 | 車號 | 耗時 |
|---|---|---|---|---|
| 白色車架自行車 | 車架白色有橙色條紋、把手橙色皮革、兩輪看起來有氣、車架有文字但看不清 | bike | null | 4.1 秒 |
| 藍色車架自行車 | 車架藍色輪圈橙色、停在人行道、看不到車號、輪胎與鏈條可見 | bike | null | 3.9 秒 |
| 黑色公路車（昏暗） | 側面照、外觀看不出明顯損傷、光線昏暗、看不清車號 | bike | null | 3.8 秒 |
| 銀色車架自行車 | 車架銀色外觀完整、棕色皮革握把、前後輪胎黑色 | bike | null | 4.1 秒 |

四張都正確回報「無法判斷煞車系統」「無法確認電子或電池狀態」「無法判斷是否可正常騎乘」，且**沒有因為外觀正常就說車沒問題**，看不到車號就回 `null` 不亂猜。

降級路徑實測：解析度 80×60 → 不呼叫模型直接降級；非圖片位元組 → 格式檢查失敗；超過 5 MB → 拒收；幾乎空白的大圖 → 模型回「照片過於模糊，無法辨識任何具體內容」，`requires_manual_review: true`。四種情況都不阻擋人工通報。

**未驗證**：模型連線層級的逾時（`VISION_TIMEOUT_S=12`）沒有以真實斷網重現，只驗證了程式路徑存在與其他降級分支。

## 六、手機觸控回歸

視窗 420×880，操作路徑：首頁通勤卡 → 出發 → 導航到借車站 → 通報問題 → 選輪胎 → 送出 → 受理頁（工單 R006） → 提醒下一位 → 我已完成 → 追蹤進度。全程可操作，無 console 錯誤。

**限制**：內嵌瀏覽器送的是滑鼠事件，不是真實觸控事件。真機觸控、`capture="environment"` 相機開啟、iOS Safari 加入主畫面都**未在實機驗證**。

## 七、需要 B 提供的契約

1. **`saddle_marker_status` 欄位與設定端點**。目前 C 把標記存在自己這裡，B 與 A 可讀 `GET /api/c/saddle_markers`。B 在工單加上欄位後請提供 `POST /api/ops/tickets/{id}/saddle`，C 會改為直接寫入。語意：`done` 只代表使用者自述已反轉坐墊，是現場提醒，**不是維修確認、不停止工單、不是安全或故障真值**。
2. **工單狀態詞彙**。C 目前直接顯示 `TICKET_LABEL`。若 B 要把「現場確認／處理／驗收／復役」分開，請提供對照表。
3. **舊入口 `POST /api/tickets` 是否下架**由 B 決定。

## 八、需要 A 提供的契約

1. **通知 payload 帶 `role` 與事件識別**。C 的 sw.js 已依 `extra.role` 或 `channel` 路由到 `/gov`、`/ops`、`/citizen`，並把 `event_id`／`ticket_id`／`report_id` 帶成 `?focus=`。請 A 在 `/gov` 支援 `?focus=` 深連結。
2. **AI 影像推測不得放進「已確認原因」**。C 送出的 `image_analysis` 已標 `source: user_confirmed_image` 與 `confident: false`。

## 九、模擬與界線

- iOS 關頁背景通知：**明示模擬**。`/api/c/push/status` 回報 `background_push.available=false`，介面寫明「關掉頁面後不會收到任何通知」。sw.js 有 `push` 處理器但沒有推播伺服器，檔頭已註明不會被觸發。
- 建單不等於已確認根因、已遠端停租、已扣除官方庫存或安全復役。介面照這樣寫。
- 坐墊反轉是提醒下一位，不是修好車，不結束工單，不延長計費。
- 示範環境的工單不會送到真正的 YouBike 官方，介面已寫明。
- 官方依據：YouBike 2026-04-08 公告故障車坐墊調低並反轉 180 度。UI 把教學放在受理之後是本案降低通報阻力的設計，不聲稱官方要求先通報才能反轉。

## 十、重現步驟

```bash
cd /Users/chenhongfei/CC/ntpc-youbike
export PATH=$HOME/Library/Python/3.9/bin:$PATH AWS_PROFILE=hackathon
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8791 &
curl -s -X POST localhost:8791/api/profile -H 'content-type: application/json' \
  -d '{"role":"worker","home_sid":895,"work_sid":858,"out_time":"07:40","back_time":"18:10","join_rewards":true,"preference":"time","onboarded":true}'
python3 tests/test_c_round2.py
```

畫面：開 `http://127.0.0.1:8791/citizen`。

## 十一、尚未完成

- 三端同一事件的整合驗收（I01）需要 A、B 一起跑，C 這側已可提供 `report_id` ↔ `ticket_id` 對應。
- SSE 斷線重連（I03）只驗證了 GET 補狀態與無副作用，**沒有實際切斷連線重現**。
- 真機觸控與相機、iOS 主畫面安裝未驗證。
- 模型連線逾時未以真實斷網重現。


---

# 第二段（8787 重啟後）：接上 B 契約 b-1、SSE 實測、狀態分離

## 十二、已接上 B 的共同契約

B 發布 `GET /api/ops/contract`（`contract_version: b-1`）之後，C 做了三件接線：

1. **建單帶齊契約欄位**：`request_id`、`report_id`、`evidence[]`、`certainty`、`asset_type`、`dock_id`。
   按鈕選擇送 `certainty: stated`，照片觀察送 `observed`，照片無法確認的事送 `unknown`。
   **AI 影像推測不會被寫成已確認原因。**
2. **坐墊標記直接寫入工單**：改呼叫 `POST /api/ops/tickets/{tid}/saddle`，本地保留鏡像。
   `/api/c/saddle_markers` 仍可讀，作為 B 唯讀橋接的來源。
3. **四種狀態分開顯示**：工單處理進度、設備驗收、站點服務、坐墊標記各自一列，不混用同一個欄位。

## 十三、C → B 閉環實測

| 步驟 | 結果 |
|---|---|
| C 建報 `C001`（鏈條異常，車號 YB2-77001，柱號 11） | 工單 `R001`，`action: created` |
| 同 `request_id` 重送 | `idempotent: true`，同一 report、同一 ticket |
| C 標記坐墊 `done` | 工單狀態 `reported → reported`（未變）、`asset_state` 未變、`version 1 → 2` |
| 從 B 端讀 `GET /api/ops/tickets/R001` | `saddle_marker: {status: done, source: user_report}`、`report_ids: ["C001"]` |
| B 推進到 `accepted` 再到 `on_site` | C 追蹤頁顯示「處理進度 現場檢查中／設備驗收 待現場確認／站點服務 未知／坐墊 已反轉」 |

**修復不等於驗收**這一點在畫面上成立：工單已到現場檢查中，設備驗收仍是「待現場確認」。

## 十四、SSE 斷線重連實測（I03）

新增 `tests/test_c_sse.py`。實際開 SSE 連線、收到事件後 `close()` 強制斷線，斷線期間用 API 建立回報，再重連。

| 編號 | 項目 | 結果 |
|---|---|---|
| I03-0 | SSE 可建立連線並收到事件 | 通過 |
| I03a | 重連後 GET 補回斷線期間的事件 | 通過，`C001` 出現在清單 |
| I03b | 補回的資料含工單與權威狀態 | 通過，`ticket=R001 status=已受理` |
| I03c | 重連後重送同 `request_id` 不重複建單 | 通過，工單數 1 → 1 |
| I03d | 重連後能收到斷線後發生的新事件 | 通過，事件序列 `["hello","notify"]` |

前端 `connectEvents` 在連線失敗兩次後會自動改為每四秒輪詢 `/api/state`，這段在上一輪已實作，本輪未另外以斷網重現。

## 十五、測試防呆

`tests/test_c_round2.py` 與 `tests/test_c_sse.py` 會呼叫 `POST /api/reset`，那會清掉三端共用的回放狀態。
兩個檔案都加了防呆：`YB_BASE` 指向 `:8787` 直接中止並說明原因。已實測會拒絕執行。

## 十六、第二段之後仍未驗證

- 三端同一事件的完整整合驗收（I01）仍需 A、B 同時在場跑一次。C 這側已提供 `report_id ↔ ticket_id` 對應與四狀態顯示。
- 真機觸控、相機 `capture="environment"`、iOS 加入主畫面仍未實機驗證。
- 視覺模型的連線層逾時未以真實斷網重現。
- 前端輪詢降級未以真實斷網重現。


---

# 第三段：抵達期限與餘裕（N06）

驗收矩陣的 N06 原本只有手算通過，畫面上沒有「幾點前要到」與餘裕，本段補齊實作。

## 十七、做了什麼

`plan_trip` 增加選填參數 `arrive_by`（`HH:MM` 或完整時間字串）。每個方案多回三個欄位：

| 欄位 | 意義 |
|---|---|
| `slack_min` | 期限減抵達時間。負值代表會遲到 |
| `late` | 是否晚於期限 |
| `latest_depart` | 期限減總時間，最晚什麼時候出發 |

回傳層再加 `arrive_by`、`any_on_time`、`deadline_note`。排序改為**會遲到的方案一律排在後面**，
所以在有來得及的方案時，遲到方案不會被當成主推薦。全部都會遲到時，用 `deadline_note` 明說
「所有方案都會晚於你設定的抵達時間，下面顯示的是最接近的」，不假裝準時。

畫面：起點列多一個「＋ 設定幾點前要到」，設定後主卡顯示
「在 08:25 前到，餘裕 31.9 分　最晚 08:11 出發」；來不及時改為紅字
「比 07:45 的期限晚 8.1 分」。替代方案那一行也會附上「仍有 X 分餘裕」或「會遲到 X 分」。

## 十八、N06 測試

預期值用獨立的 `datetime` 換算比對，不採用實作輸出當答案。

| 編號 | 項目 | 結果 | 證據 |
|---|---|---|---|
| N06a | 餘裕＝期限減抵達時間 | 通過 | 回傳 46.9，獨立換算 47.0（eta 08:13，期限 09:00） |
| N06b | 來得及時不標遲到 | 通過 | `late:false`、`any_on_time:true` |
| N06c | 最晚出發＝期限減總時間 | 通過 | 08:46:54，總時間 13.1 分，差 0.00 分 |
| N06d | 全部遲到時明確標示，不當準時推薦 | 通過 | `any_on_time:false`，全部 `late:true`，有 `deadline_note` |
| N06e | 遲到方案的餘裕為負 | 通過 | −10.1 分 |
| N06f | 沒設期限就不出現餘裕欄位 | 通過 | `arrive_by:null`，選項無 `slack_min` |

畫面實測：設 08:25 → 「餘裕 31.9 分　最晚 08:11 出發」；設 07:45 → 「⚠️ 比 07:45 的期限晚 8.1 分」
並出現全部遲到的橫幅。

**目前 C 累計：後端驗收 35 項、SSE 5 項，全數通過。**

## 十九、8787 需要再重啟一次

`arrive_by` 是在使用者重啟 8787 之後才提交的，所以共用伺服器上還沒有這個功能。
已在 8787 上驗證的是：C→B 建單、`request_id` 冪等、坐墊寫入工單且狀態不變、B 端看得到
`report_ids` 與 `dock_id`、A 台帳 46 筆。**抵達期限要下一次重啟才會生效。**

8787 目前留有我驗證用的一筆資料：回報 `C001` 對應工單 `R001`（輪胎、車號 YB2-LIVE01、柱號 03、
坐墊標記 done）。這是刻意留下的閉環證據，要清掉隨時可以說。


## 二十、通知深連結參數對齊

各端讀的參數名不同，sw.js 依角色送對應的名稱：

| 目標 | 參數 | 誰實作 | 狀態 |
|---|---|---|---|
| `/gov` | `?event=<id>` | A（`deepLink()` 已讀） | 已對齊 |
| `/ops` | `?ticket=<id>` | B | **B 尚未實作讀取**，送過去會被忽略，不會壞 |
| `/citizen` | `?report=<id>` | C | 已實作，實測 `/citizen?report=C001` 直接開該筆處理進度 |

請 B 在 `/ops` 加上讀 `?ticket=` 並開啟該張工單；C 這側不需再改。
