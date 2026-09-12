# 三條主線分工（2026-09-12 起，交件 9/13 13:00）

三個 Claude session 平行做三端。**共用同一個工作目錄、同一條 `main`**，靠檔案歸屬避免撞車，不開分支。

**名稱一律用中文，不要再用 A／B／C 稱呼。**（檔名與既有 commit 裡的 `HANDOFF_A/B/C.md`、
`test_a_*`、`test_b_*`、`test_c_*` 維持原樣，改名會打斷另外兩條線的引用與歷史，
但講話、文件內文、新寫的說明一律用中文名。）

| 主線 | 路徑 | 對應角色 | 舊代號（僅存在於檔名） |
|---|---|---|---|
| **政府端** | `/gov` | 新北市交通局：監看、事件決策、驗收 | 政府端 |
| **微笑單車** | `/ops` | 營運調度與維修派工 | 微笑單車 |
| **用戶端** | `/citizen` | 民眾（手機框） | 用戶端 |

## 為什麼不開分支

三個 session 共用 `/Users/chenhongfei/CC/ntpc-youbike` 這一個工作目錄，也就是共用同一份 git HEAD 與 index。
任何一方 `git switch` 都會把檔案從另外兩方腳下抽掉。剩不到一天，分支的隔離收益抵不過合併成本。

好處是：本地 `main` 只有一條，別人的 commit 一做完就已經在你的本地了，**push 永遠是 fast-forward，不需要 pull**。

## 檔案歸屬

| 主線 | 這些檔案你說了算 |
|---|---|
| 政府端 | `app/static/gov.html`、`app/metrics.py`、`docs/METRICS.md` |
| 微笑單車 | `app/static/ops.html`、`app/static/ops.js`、`app/static/ops_baseline.json`、`app/planner.py` 的 `plan_dispatch` / `gaps` / 組趟相關 |
| 用戶端 | `app/static/citizen.html`、`app/static/sw.js`、`app/static/manifest.webmanifest`、`app/static/icons/`、`app/planner.py` 的 `plan_trip` 三方案、`docs/REWARDS.md` |

**不要改別人歸屬的檔案。** 需要對方改，在 chat 講，不要自己動手。

## 共用檔案的動土規則

| 檔案 | 規則 |
|---|---|
| `app/server.py` | **只在檔尾自己的區塊附加新端點**，不改別人既有的函式。要改 `evaluate_alerts`、`plan_tasks`、`STATE` 結構、`kpis`、`notify` 這些三端共用邏輯，**先公告再動**。 |
| `app/static/common.js`、`common.css` | 只能新增函式／類別。不改既有函式的行為或既有類名的樣式——那會同時改到三端的畫面。 |
| `app/planner.py` 的 `ASSUMPTIONS` | 改任何一個值都會同時改到三端顯示的假設，先公告。 |
| `README.md` | 只改自己那幾行。 |
| `app/predict.py`、`pipeline/*`、`models/`、`data/processed/` | **凍結**。交件前不重新訓練、不重跑管線。 |
| `app/llm.py`、`weather.py`、`awsloc.py` | 凍結，除非修 bug。 |

## Git 硬規則（共用工作目錄，違反會吃掉別人的進度）

- **禁止** `git add -A` / `git add .` / `git commit -a`。一律 `git add <明確列出自己的檔案>`。
- **`git add app/server.py` 之前一定要先看 `git diff app/server.py`。** path-scoped add 對共用檔沒有保護作用：
  它會把別人寫在同一個檔案裡、還沒完成的區塊一起帶走。
  2026-09-12 就發生過一次——某一線 commit server.py 時掃走了政府端還沒寫完的端點，卻沒有一起帶到對應的
  `app/events.py` 與 import，導致 main 上的 `/api/ledger` 直接 NameError。
  **每個 hunk 都確認是自己的才 add；看到別人的區塊就先在 chat 講，不要自己決定要不要一起 commit。**
- **別人有 staged 但還沒 commit 的檔案時，不要直接 `git commit`。** `git status --short` 第一欄是 `M`
  代表已進 index，直接 commit 會把那些檔案一起帶走。改用 `git commit -- <你的檔案路徑>`，只提交指定路徑、
  不動 index（注意：新檔要先 `git add`，pathspec 對未追蹤檔無效）。
- **禁止** `git stash`、`git checkout .`、`git reset --hard`、`git clean`、`git switch`、`git restore`。
- commit 前先 `git status --short`，確認你 add 的只有自己的檔；別人的 `M` 留在那裡不要碰。
- commit 後直接 `git push origin main`。不需要 pull；若 push 被拒，代表有人在 GitHub 上直接改了，先問過再處理。
- commit message 用中文，講清楚改了什麼、為什麼。

## Server 規則

- 共用 `http://127.0.0.1:8787`（`python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8787`）。
- 程式碼在磁碟上是共用的，所以**任何人重啟就是三方一起拿到最新程式**。
- 但重啟會清掉回放狀態（告警、任務、意向、工單、獎勵、情境）。**重啟前在 chat 喊一聲。**
- 要另開 port 測試可以，但每個 server 會各自載入一份模型與矩陣（約 1.5 GB），測完請關掉。
- 目前 `8788` 是政府端測試用（已含 `/api/metrics`），`8787` 需重啟一次才會有驗收指標端點。

## 三條線各自要做什麼

優先序依 `docs/DIRECTION_REVIEW_2026-09-12.md`：第一優先＝事件台帳＋P0/P1 手機決策＋設備分流工單。

### 政府端

- [x] 驗收指標面板（服務中斷事件上下界、告警門檻取捨、流程時效、量不出來的四項）— `b717fe5`
- [x] **事件台帳**（`376c15f`）：分級 P0／P1／長期／待查、觀測下界與上界、最後資料時間、負責人、ETA、原因與證據、狀態留痕。站群同時失效以 500 公尺鄰站關係認定，不是以行政區。API 是 `/api/ledger`（`/api/events` 已被 SSE 端點佔用）。
- [x] **第二輪數值口徑**（`12ca2d8`）：觀測跨度不等於連續中斷、未知上界保留未知、候選觸發量不等於通知量、ack 不等於指派。手算 fixture 見 `tests/test_a_numeric.py`。
- [x] **全域儀表板、事件版本與冪等、通知情境預覽**（`9d64929`、`320547c`）：供需／資料新鮮度／設備待查三欄／任務與未覆蓋缺口；`POST /api/ledger/{id}/action` 帶 version 與 request_id。
- [ ] **主管決策卡**：方案 A／B 比較（就近改道 vs 跨區支援，各自的抵達時間、受影響站點、延後代價），選完保存決策、理由、任務版本與時間，避免雙重派工。
- [ ] **服務可用性**：官方可借 N／已確認不可用 X／疑似異常 Y／確認時間（需要 C 線的症狀回報彙總）。
- [ ] **接 B 的 planning cycle**：把候選／確認／在途與資源帳顯示到全域儀表板。詳見 `docs/HANDOFF_A.md` 的阻擋清單。

### 微笑單車

- [ ] **資源帳共用**：120 與 180 分鐘的方案目前各自規劃，會重複承諾同一批人車與庫存。要共用一本資源帳。
- [ ] **每個送站各自的服務時限**：確認不是只用第一個送站的抵達時間判定整趟可行。
- [ ] **「決策到抵達 60 分」拆段**：人員準備／取車／行車／卸車分開估，再校準。現在 `lead_time_min=60` 之後還要再加路程，語意跟訪談不同。
- [ ] **工單去重改用車號／柱號**：現在以「站點＋問題類別＋2 小時」去重，會把不同設備合併。缺 `asset_type`、`dock_id`、`error_code`。
- [ ] 保留殘量的邏輯已在 `planner.py` 修過，需要針對性測試確認。

### 用戶端

- [ ] **借不到 ≠ 車壞掉**：回報流程改成「先問可觀察症狀」，不強迫使用者選根因。自動帶入站點、時間、借／還階段。
- [ ] **交易與安全狀態**：借車未成功才建議換設備；還車未確認要保留現場證據並進官方協助，**不能假裝已停止計費**。
- [ ] **回報後的回饋**：帶入處理進度，通知替代站但不保證可用；使用者可以隨時離開問卷。
- [ ] **推播要誠實**：`sw.js` 目前只有快取，沒有 `push` / `notificationclick`。頁內 SSE 不是手機背景推播，介面不可以寫成「已送達」。

## 交會點（動到這些一定要先公告）

| 交會點 | 誰主導 | 影響誰 |
|---|---|---|
| `evaluate_alerts` 改成事件台帳 | 政府端 | 微笑單車（告警來源）、用戶端（通知） |
| `tickets` 結構加車號／柱號／錯誤碼 | 微笑單車 | 用戶端（回報表單要送這些欄位） |
| `notify()` 的 channel 與 payload | 三方共議 | 三端 |
| `STATE` 新增欄位 | 誰加誰負責 `api_reset` 一起清 | 三端 |
