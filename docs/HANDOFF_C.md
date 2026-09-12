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


---

# 二十一、下一個 session 接手 C 主線從這裡開始

## 你是誰

你負責 `/Users/chenhongfei/CC/ntpc-youbike` 的 **C 用戶端主線**。先讀 `docs/WORKSTREAMS.md` 的檔案歸屬與
git 硬規則（三個 session 共用同一個工作目錄與 HEAD，**禁止** `git add -A`、`stash`、`reset --hard`、
`switch`、`checkout .`）。再讀本檔全文與 `docs/DIRECTION_REVIEW_2026-09-12.md`。

任務書在 `/Users/chenhongfei/Documents/Codex/2026-09-12/new-chat/outputs/claude-control/第二輪正式任務書.md`，
最末的「使用者最新修訂」優先於前文。

## C 歸屬的檔案

`app/static/citizen.html`、`app/static/sw.js`、`app/static/manifest.webmanifest`、`app/static/icons/`、
`app/report_image.py`、`app/planner.py` 的 `plan_trip`、`docs/REWARDS.md`、`docs/HANDOFF_C.md`、
`tests/test_c_round2.py`、`tests/test_c_sse.py`，以及 `app/server.py` 中
`# C 主線（用戶端）` 到 `# A 主線（政府端）` 之間的區塊。

**其他檔案一律不要動。** `app/server.py` 提交前務必 `git diff -U0 app/server.py | grep "^@@"`
確認所有 hunk 都落在 C 區塊行號內；若別人也有未提交的改動，用
`git show HEAD:app/server.py` 加自己的區塊重組後以 `git hash-object -w` + `git update-index --cacheinfo` 精準暫存。

## 目前完成度

C 後端驗收 35 項、SSE 5 項，全數通過。最後 commit `24bb00f`。
已完成：按鈕直接通報、照片 AI 輔助與降級、坐墊提醒、進度追蹤、B 契約 b-1 接線、
request_id 冪等、SSE 斷線重連、抵達期限與餘裕、通知深連結依角色路由。

## 怎麼跑

```bash
cd /Users/chenhongfei/CC/ntpc-youbike
export PATH=$HOME/Library/Python/3.9/bin:$PATH AWS_PROFILE=hackathon
# 共用展示用 8787（已在跑，重啟前要在 chat 喊一聲，會清掉三端回放狀態）
# 自己測試另開埠，測完關掉
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8791 &
curl -s -X POST localhost:8791/api/profile -H 'content-type: application/json' \
  -d '{"role":"worker","home_sid":895,"work_sid":858,"out_time":"07:40","back_time":"18:10","join_rewards":true,"preference":"time","onboarded":true}'
YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_round2.py
YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_sse.py
```

測試檔有防呆：`YB_BASE` 指向 `:8787` 會拒絕執行，因為會呼叫 `/api/reset` 清掉三端狀態。

## 還沒做完的（依優先序）

1. **I01 三端同一事件整合驗收**。要 A、B 同時在場跑一次：C 建報 → B 接手 → 修復 → 驗收，
   確認同一個 `ticket_id` 三端狀態一致、修復不等於自動驗收。C 這側已備妥。
2. **請 B 在 `/ops` 加讀 `?ticket=<id>`** 深連結並開啟該張工單。C 已經在送。
3. **真機驗證**：觸控、相機 `capture="environment"`、iOS Safari 加入主畫面。
   目前只在內嵌瀏覽器以滑鼠事件測過。
4. **視覺模型連線層逾時**與**前端輪詢降級**未以真實斷網重現。
5. 若還有時間：民眾端的獎勵流程（`docs/REWARDS.md` 的四層機制）目前只有存摺與集章，
   動態加碼倍率沒有實作。任務書說「保留既有獎勵流程、不擴充」，所以這項不是必做。

## 不要做的事

- 不要重訓模型、不要重跑 `pipeline/`、不要動 `app/predict.py`、`models/`、`data/processed/`（凍結）。
- 不要大改視覺。通勤首頁、推薦卡、三方案切換、存摺已定案。
- 不要在 C 自行計時宣稱工單修好，工單狀態一律以 B 的 `STATE["tickets"]` 為準。
- 不要宣稱背景推播已完成。`/api/c/push/status` 的 `background_push.available` 是 `false`，
  iOS 關頁通知本輪是明示模擬。
- 不要因為照片看不出異常就判定車輛安全或正常。


---

# 第四段：三端整合驗收、斷線重連補完、真實失效重現、原流程回歸

接手基準 `c287235`，本段提交 `039a2f1`、`7a36551`、`ffe4c4f`、`f8184e1`。
只動 C 歸屬檔案，加上 `app/server.py` 的 `api_reset` 一行（共用區塊，理由見下）。

## 二十二、改了哪些檔案

| 檔案 | 歸屬 | 動作 |
|---|---|---|
| `app/server.py` 的 `api_reset` | 共用檔，**C 區塊之外的一行** | 補上重設 `CREPORTS`，修 B 回報的跨端 reset 殘影 |
| `app/static/citizen.html` | C | 加 `connectEventsC`：SSE 斷線後背景持續重試，接回來停輪詢並補狀態 |
| `tests/test_c_i01.py` | C（新增） | 三端同一事件整合驗收 45 項 |
| `tests/test_c_vision_fail.py` | C（新增） | 視覺模型連線層真實失效 29 項 |
| `tests/test_c_regress.py` | C（新增） | 通勤原流程回歸 48 項 |

## 二十三、修掉的跨端缺陷：`/api/reset` 沒清 C 的狀態

B 寫的 `tests/test_b_reset_crossend.py` 實跑有三項失敗：`/api/reset` 清掉工單之後，
C 的 `CREPORTS`（回報、`request_id` 冪等表、坐墊標記）還留著，`GET /api/c/report/{id}`
仍回 200、`/api/c/saddle_markers` 還指向已被刪除的工單、同 `request_id` 重送回舊殘影。
對應任務書「reset 包含所有自有狀態」與最低矩陣「三端 reset 一致」。

修法是在 `api_reset` 就地重設 `CREPORTS`。全檔沒有任何地方把 `CREPORTS["items"]`／
`["saddle"]` 綁成區域變數，都是每次經由 dict 取用，所以換成新容器不會留下舊參照。

**B 的測試還有一項不會過，那是斷言本身不可靠，不是實作問題**：它用
`again["ticket_id"] != 舊 ticket_id` 判斷「有沒有重新建單」，但 reset 之後 B 的工單
序號也從 R001 重新編，新建的單同樣叫 R001，字串比對分不出殘影與新單。正確判準是
`idempotent == False` 且 `ticket_action == "created"`，本輪在 `test_c_i01.py` 的
I01ap 以這個判準驗過。**請 B 改這一行斷言。**

## 二十四、I01 三端同一事件整合驗收（45 項，全過）

原本列為「需要 A、B 同時在場」。三端程式在同一個 server 上，所以改成用各端自己對外的
HTTP 介面走完一遍再交叉比對，不需要等人：C 讀 `/api/c/report/{rid}`、B 讀
`/api/ops/tickets/{tid}`、A 讀 `/api/ledger` 的 `overview.equipment.rows`。

| 驗到什麼 | 結果 |
|---|---|
| 同一個 `ticket_id`／`report_id` 三端都查得到、值一致 | 通過 |
| `dock_no` 正規化後三端都是 `dock_id` | 通過（B=09、A=09） |
| 同 `request_id` 重送：三端工單數都不變 | 通過（1 → 1） |
| 接手後三端 status 與中文標籤一致、負責人與 ETA 一致 | 通過（維修二班／08:40） |
| 坐墊標記三端看得到，但不推進工單、不改設備驗收 | 通過（accepted → accepted，asset_state 仍 suspect） |
| **修復不等於驗收** | 通過：recovered 後設備只到 `repaired`，C／A 顯示內容不含「已驗收／驗收復役／verified」 |
| A 主動標示「已處理但服務未恢復」 | 通過（`divergence=['handled_not_restored']`） |
| 修復後站點服務仍是未知，不自動宣稱恢復 | 通過 |
| 舊 version 推進回 409 且沒被推進 | 通過 |
| 驗收後設備才是 `verified_ok` | 通過 |
| 使用者離開不結案、A 端仍看得到 | 通過 |
| 工單不可倒退 | 通過（HTTP 400 `backwards`） |
| reset 後三端都不留殘影、同 `request_id` 重送是重新建單 | 通過 |

狀態機順序與中文標籤在測試檔開頭另寫一份獨立對照表，不取 `/api/ops/contract` 的輸出
當答案。B 改詞彙這支就會失敗，那是刻意的。

## 二十五、SSE 斷線重連原本只做了一半（真實斷線才看得到）

用 `kill -9` 直接切掉伺服器觀察 `/citizen`：共用的 `connectEvents()` 連三次失敗後改成
每四秒輪詢 `/api/state`，**之後就再也不重試 SSE**。網路紀錄顯示伺服器回來之後完全沒有
新的 `/api/events` 請求。輪詢只補得回時鐘，補不回 `notify`／`c_report`／`trip`／`wallet`，
所以畫面看起來還活著，通知卻再也不會出現，要重新整理才恢復。

`common.js` 是三端共用檔，規則只准新增函式不准改既有函式，所以在 `citizen.html` 裡包一層
`connectEventsC`：輪詢只當過渡，背景每五秒持續重試 SSE，接回來就停掉輪詢並用 GET 補一次
權威狀態。**`common.js` 一個字沒動，A／B 不受影響。**

過程中修掉自己寫的兩個競態，都是實測看到才發現的：

1. `onmessage` 可能早於 `onopen`（hello 先到），只在 `onopen` 停輪詢會變成「SSE 已接上但
   輪詢還在跑」，兩條通道並行。改成兩邊共用 `markConnected`。
2. 斷線期間累積的多次嘗試各自帶一個 8 秒 guard，舊那次的 guard 會在新連線已經健康時才
   觸發，把狀態誤打回輪詢。加 `gen` 世代編號，編號對不上就整個略過。

最後一次實測：

| 時間 | 事件 | 觀察 |
|---|---|---|
| 22:27:28 | `kill -9` 切掉伺服器 | 立刻轉輪詢，時鐘停在最後一筆不假裝前進 |
| 22:28:00 | 伺服器回來 | 約 3 秒接回 SSE，log 顯示「SSE 已重新接上，停止輪詢」 |
| 22:28:16 | 重連後才建立的回報 | 頁面收得到「報修已受理 R001」 |
| 之後 16 秒 | 量測輪詢次數 | 0 次，`/api/events` 只有一條連線 |

**22:28:16 那一項正是舊版永遠收不到的。** 沒有重新整理頁面。

## 二十六、視覺模型連線層失效以真實斷網重現（29 項，全過）

原本列為未驗證。`tests/test_c_vision_fail.py` 把 Bedrock endpoint 指到不可路由的
`10.255.255.1`，封包是真的被丟掉，不是 mock 也不是注入假例外。只改 `VISION_TIMEOUT_S`
就能分別踩到兩條降級路徑：

| 路徑 | 設定 | 實測 |
|---|---|---|
| 逾時 | `VISION_TIMEOUT_S=1`（join 4 秒 < connect_timeout 5 秒） | 4.0 秒後回「視覺模型超過 1 秒未回應」 |
| 呼叫失敗 | `VISION_TIMEOUT_S=20`（等到 botocore 自己拋） | 10.8 秒後回真的 `ConnectTimeoutError` |

兩條都驗證 `observations` 為空、標為需人工判讀、不冒充 `model_source`、不推測設備類型、
不出現「正常／安全」字樣。接著把降級結果照前端流程送進 `/api/c/report`：不確定仍可送出
但不建維修工單、明確機械問題照樣建單、重送仍冪等、降級的照片不會變成工單上的影像證據。

HTTP 層也用同樣的黑洞設定重啟 8791 實跑過一次 `POST /api/c/image`：4.0 秒降級、
回報照樣受理為 `pending_triage`、`ticket_id: null`、`vision/status` 記到 `degraded: 1`。

## 二十七、通勤原流程回歸（48 項，全過）

任務書 C.4 要求「正常通勤、提早到選還車、趕時間與集點原流程須回歸測試」，先前只有一次
瀏覽器手動點擊紀錄，走的還是通報路徑。`tests/test_c_regress.py` 補成自動化：

- **R1 正常通勤**：規劃 → 出發 → 登記意向 → 騎乘 → 還車完成 → 點數入帳、集章、意向轉
  completed、行程結束、存摺公里數增加；獎勵紀錄必須寫明是模擬完成事件。
- **R2 提早到選還車**：餘裕與最晚出發時間用 `datetime` 獨立換算比對；還車替代站標明是
  快照與預測、不是保證、不含原站；換站後仍能完成集章。
- **R3 趕時間**：主推薦確實是全部方案裡最快的；期限設在過去時全部標遲到、餘裕為負、
  明說來不及，不當準時推薦。
- **R4 集點**：偏好集點時排第一；點數用「方案點數 ＋ 5」獨立換算；同一枚章不重複；
  取消釋放名額且不倒扣已入帳點數。
- **R5 途中通報不影響原流程**：騎乘中通報會建單、坐墊提醒延後到停穩、行程不被打斷、
  騎完不會把回報或工單順手結案。

點數照固定規則自己算一次，時間用 `datetime` 自己換算，不拿實作輸出當答案。

## 二十八、手機視窗操作回歸（420×880 之外另跑 375×812）

`/citizen?report=C001` 深連結直接開該筆處理進度，四種狀態分列：處理進度「已受理」、
設備驗收「待現場確認」、站點服務「未知」、坐墊標記「未標記」。
完整路徑重跑：首頁通勤卡 → 出發 → 導航 → 到借車站 →「通報問題（借不到／車況異常）」
→ 選輪胎 → 送出 → 受理頁（C002／工單 R002）→ 提醒下一位（正反向示意與四步驟、
寫明「是標記故障車給下一位看，不是把車修好」）。全程可操作，無 console 錯誤。

**限制不變**：內嵌瀏覽器送的是滑鼠事件，不是真實觸控事件。

## 二十九、目前累計

| 測試檔 | 項數 | 結果 |
|---|---|---|
| `tests/test_c_round2.py` | 35 | 全過 |
| `tests/test_c_sse.py` | 5 | 全過 |
| `tests/test_c_i01.py` | 45 | 全過 |
| `tests/test_c_vision_fail.py` | 29 | 全過 |
| `tests/test_c_regress.py` | 48 | 全過 |
| **合計** | **162** | **全過** |

在 A 提交 `78fd521`、`c523bc0` 之後重跑過一次，仍然全過。

```bash
cd /Users/chenhongfei/CC/ntpc-youbike
export PATH=$HOME/Library/Python/3.9/bin:$PATH AWS_PROFILE=hackathon
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8791 &
curl -s -X POST localhost:8791/api/profile -H 'content-type: application/json' \
  -d '{"role":"worker","home_sid":895,"work_sid":858,"out_time":"07:40","back_time":"18:10","join_rewards":true,"preference":"time","onboarded":true}'
for t in round2 sse i01 vision_fail regress; do YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_$t.py; done
```

五個測試檔都有防呆：`YB_BASE` 指向 `:8787` 會拒絕執行，因為會呼叫 `/api/reset`。

## 三十、仍待他人處理

1. **B 請改 `tests/test_b_reset_crossend.py` 最後一項斷言**，理由見第二十三節。
2. **B 請在 `/ops` 加上讀 `?ticket=<id>`** 深連結並開啟該張工單。C 已經在送，B 尚未實作讀取。
3. **`/api/trip/report_bike`（`server.py:897`）仍呼叫舊入口 `api_ticket_create`**，用舊的
   「站點＋問題類別＋2 小時」去重。`citizen.html` 已經不呼叫它，所以 C 的 UI 沒有雙入口
   問題，但 HTTP 路由還在。要不要下架屬 B。

## 三十一、本段之後仍未驗證

- **真機觸控、相機 `capture="environment"`、iOS Safari 加入主畫面**：仍只在內嵌瀏覽器
  以滑鼠事件測過，沒有實機。
- **視覺模型的讀取逾時（read timeout）**：本輪重現的是連線層（connect）逾時與連線失敗。
  已連上之後模型回應到一半才停住的情況沒有重現。
- **獨立把關兩輪**：任務書要求的第三層驗收還沒做，本段全部是 C 自己跑的。


---

# 第五段：最低矩陣補齊、讀取逾時、測試敏感度反證

提交 `ac32364`。

## 三十二、讀取逾時真實重現（第三十一節原本列為未驗證）

上一段重現的是連線層（connect）失效。這段補「連線通了但回應永遠不來」：本機開一個
TCP listener，完成三次握手、收下請求，然後永遠不回任何位元組。探針會回報已接受的連線數
（實測 2 條），證明連線真的建立過，卡住的是回應。

| 設定 | 走哪條分支 | 實測 |
|---|---|---|
| `VISION_TIMEOUT_S=1` | botocore 的 read_timeout 先到 | 3.0 秒，拋真的 `ReadTimeoutError` |
| `VISION_TIMEOUT_S=3` | botocore 重試後總耗時超過模組的 join | 6.0 秒，走模組自己的看門狗 |

兩條都驗證不產生觀察、標人工判讀、不冒充 `model_source`、不出現「正常／安全」字樣。
`tests/test_c_vision_fail.py` 從 29 項增為 40 項。

## 三十三、最低矩陣補兩格（`tests/test_c_matrix.py`，31 項全過）

| 段 | 驗到什麼 |
|---|---|
| M1 無車號柱號 | 缺識別保留 `null` 不用空字串冒充；同站同問題且**兩邊都沒有識別**才合併；有車號或有柱號的那筆不跟匿名那筆合併；A 端標出 `no_asset_id` 並統計張數 |
| M2 車機無回應 | 刷卡沒反應不建維修工單、不判定車輛故障、走租借與交易待查、不引導去反轉正常車的坐墊；交易不明時不引導反覆試借也不宣稱可安全換車 |
| M3 通知深連結 | 把 `app/static/sw.js` 載進假的 Service Worker 環境**實際執行**，驗真正會開出去的網址而不是看原始碼字串 |

M3 實測結果：`/gov?event=A12`、`/ops?ticket=R007`、`/citizen?report=C003`，沒有識別時是
`/citizen` 不硬加參數，識別 `A 1&x=2` 被編碼成 `A%201%26x%3D2` 不會被注入成第二個參數。
需要 node，沒有 node 會跳過並明說跳過。

## 三十四、營運端通知帶結構化識別

原本「借不到／還不了」與「待診斷」的 ops 通知只帶 `sid` 與 `report_id`，使用者填的柱號與
錯誤碼留在 C 這裡，B 的待查清單要回頭查 C 才知道是哪根柱子。改成通知本文與 `extra` 都帶
`dock_id`／`bike_no`／`error_code`／`txn_state`，並標 `certainty` 與「未判定車輛故障」。
沒有的欄位一律 `null`。改動落在 C 區塊內（`server.py` 1334–1353）。

## 三十五、反向確認測試不是空轉

任務書要求預期值不能拿同一實作的輸出當答案。除了獨立對照表之外，另外做了兩個變異副本，
**完全沒有動到共用工作目錄的檔案**（副本只存在 scratchpad）：

| 變異 | 結果 |
|---|---|
| 讓 `report_image` 降級時謊稱判讀成功、捏造「車輛外觀正常」 | V1a／V1c／V1e／V1f／V1g **5 條全部抓到** |
| 讓工單狀態機把 `recovered` 直接寫成 `verified_ok` | I01x／I01y **2 條都抓到** |

另有現成的敏感度證據：B 的跨端 reset 測試在修好之前確實抓到了 C 的殘影；本輪寫錯的
R1b／R3d／M2j／M3a-f／V3l 都如實報 FAIL，沒有默默通過。

## 三十六、目前累計

| 測試檔 | 項數 | 結果 |
|---|---|---|
| `tests/test_c_round2.py` | 35 | 全過 |
| `tests/test_c_sse.py` | 5 | 全過 |
| `tests/test_c_i01.py` | 45 | 全過 |
| `tests/test_c_vision_fail.py` | 40 | 全過 |
| `tests/test_c_regress.py` | 48 | 全過 |
| `tests/test_c_matrix.py` | 31 | 全過 |
| **合計** | **204** | **全過** |

```bash
for t in round2 sse i01 vision_fail regress matrix; do
  YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_$t.py
done
```

## 三十七、C 這側對照最低驗收矩陣

| 矩陣項目 | 歸屬 | C 的狀態 |
|---|---|---|
| 正常通勤 | C | R1 |
| 明確機械故障 | C/B | C01、I01 |
| 圖片模糊／辨識錯誤 | C | C02 |
| 無車號柱號 | C/B | M1 |
| 車機無回應 | C | M2 |
| 還車交易未知 | C | C05 |
| 報修重送 | C/B | C01b、I01g、V3k |
| 模型／API 失敗 | C | V1～V4 |
| 離開後仍處理 | C | Cl1、I01ag |
| SSE 斷線重連 | A/B/C | I03、第二十五節真實斷線 |
| 模擬通知正確深連結 | A/C | M3、`/citizen?report=` 畫面實測 |
| 三端 reset 一致 | A/B/C | I01al～I01ap |
| 缺測／未知時長 | A | 不歸 C |
| 跨區／逾時 | A/B | 不歸 C |
| 無人可派 | B | 不歸 C |
| 資源重算／取消／版本衝突 | B | C 只驗跨端版本衝突 I01ab |

## 三十八、C 這側仍未驗證（誠實列出）

- **真機**：觸控、相機 `capture="environment"`、iOS Safari 加入主畫面。只有實機能驗，
  內嵌瀏覽器送的是滑鼠事件。
- **第三層獨立把關**：任務書要求 strict-deliver-loop 兩輪，需另開獨立 agent，尚未進行。
  上述全部是 C 自己跑的，第三十五節的變異反證是自證敏感度，不能取代獨立把關。
