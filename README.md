# 新北 YouBike 雙端協同服務（2026 新北市 AI 智慧城市黑客松・交通局組）

命題：新北市公共自行車營運調度數據視覺化及預測模型。
一句話：用六個月真實站點快照，預測 30／60／120／180 分鐘後的空滿風險，讓政府端提前預警、營運端提前 120／180 分鐘安排人車、民眾端在出發前拿到「最快／較穩／順路集點」三方案並回饋意向，三端共用同一份預測。

## v2 一站式（分支 `v2`）

**根網址 `/` 就是一站式入口**（`/v2` 同頁），三端在同一個網址切換；
既有的情境回放角色選單移到 `/replay`，`/gov`、`/ops`、`/citizen`、`/stage` 與所有舊端點都沒動。
資料分三種模式並在畫面上逐塊標示：

| 標籤 | 來源 | 更新 |
|---|---|---|
| **即時** | 新北市政府資料開放平臺 YouBike2.0 官方 API（1,605 站） | 官方每 5 分鐘，我們每 2 分鐘取一次 |
| **歷史** | 2026-01~06 共 13,324,945 筆半小時快照的預算分析 | 靜態，重跑 `analytics/build_analytics.py` 才會變 |
| **即時推論** | 訓練好的模型跑在官方即時站況上（`app/liveforecast.py`） | 隨即時資料；落後特徵靠自行累積，齊全需約 2.5 小時 |
| **回放** | 既有的情境回放與訓練好的模型 | 走 `/replay`、`/gov`、`/ops`、`/citizen`、`/stage` |

```
app/live.py                 官方即時站況接入（背景輪詢、站鍵對應、容量落差偵測、即時歷史緩衝）
app/liveforecast.py         把訓練好的模型跑在即時資料上（重建 FeatureContext，共用 make_X）
app/v2api.py                /api/v2/* 全部端點（即時、預測、分析、建議、事件、行程、獎勵）
app/static/v2.html          一站式單頁；自繪 SVG 地圖，零外部依賴
analytics/build_analytics.py  歷史預算（只讀凍結資料，約 51 秒）
data/analytics/             預算結果：站級半小時風險查表、彙總、站點清單
data/live_history.parquet   即時歷史緩衝（模型落後特徵用，重啟不歸零）
tests/test_v2.py            164 項驗收（離線 + HTTP）
```

```bash
python3 analytics/build_analytics.py                      # 只在改口徑時才需要重跑
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8790
python3 tests/test_v2.py                                  # 預設打 8790，別對 8787 跑
# 開 http://127.0.0.1:8790/v2
```

**v2 新增的三個判斷，都是這一輪調查出來的：**

1. **容量落差** ＝ 官方的總停車格數 −（可借＋可還）。此刻新北 543 站、2,134 個車柱名目上存在，
   但既借不到也還不了。見車率與見位率都照不到它，因為兩者各自只看單邊數量。
2. **月平均會把尖峰稀釋掉。** 捷運江子翠站 2 號出口的六月見位率 93.4%，在官方分級裡是「高」，
   但平日早峰有 31.8% 的時間還不了車。驗收指標若只看日間平均，會漏掉通勤者真正遇到的那兩小時。
3. **借還同時為 0 不是缺車，是整站無服務。** 調度看板把這類站獨立列出，不混進送車候選——
   派車過去也沒有柱位可用。不這樣分，野柳、猴硐這種設備離線站會直接霸佔調度優先序。

完整架構、端點表與誠實邊界見 [docs/V2.md](docs/V2.md)；展示腳本見 [docs/DEMO_V2.md](docs/DEMO_V2.md)。
定位與對照見 [docs/POSITIONING.md](docs/POSITIONING.md)；
開源調查、資料可得性與三端痛點盤點見 [docs/RESEARCH_2026-09-13.md](docs/RESEARCH_2026-09-13.md)；
數字重算腳本 `reports/gap_vs_taipei.py`。

## 目錄
```
pipeline/01_ingest.py      12 份 CSV → parquet（原始檔不修改；容量矛盾隔離；缺行政區僅用全期唯一同名站補）
pipeline/features.py       訓練與線上共用特徵（半小時分箱 × 站點矩陣；只用 t 以前的觀測）
pipeline/02_train_eval.py  1–4 月訓練、5 月驗證、6 月一次測試；基準 vs 梯度提升樹；輸出 reports/model_eval.json
app/predict.py             全站預測服務（模型缺尺度時退回基準，並標示來源）
app/planner.py             調度排程（缺口、貪婪組趟、最遲出發、來不及判定）與民眾三方案；假設集中在 ASSUMPTIONS
app/llm.py                 Amazon Bedrock Converse（Claude）；失敗退回模板並標示
app/metrics.py             驗收指標：由觀測快照算服務中斷事件（下界／上界）、告警門檻取捨、流程時效
app/server.py              FastAPI：回放時鐘、SSE 即時事件、告警規則、任務、意向、工單、獎勵、情境、即時 API、活動 API、驗收指標 API
app/static/                stage.html（舞台）gov.html（政府端）ops.html（微笑單車調度端）citizen.html（民眾端，手機框）
data/processed/            obs.parquet、stations.parquet、neighbors_800m.parquet（亦上傳 S3）
models/                    hgb_h{30,60,120,180}_{reg_bikes,reg_spaces,cls_empty,cls_full}.joblib、context.npz
reports/                   model_eval.json、訓練與伺服器日誌
```

## 執行
```bash
export PATH=$HOME/Library/Python/3.9/bin:$PATH AWS_PROFILE=hackathon
python3 pipeline/01_ingest.py          # 約 2 分鐘
python3 pipeline/02_train_eval.py      # 約 40 分鐘（8 核）
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8787
# 開 http://127.0.0.1:8787/  （舞台：左手機框＝民眾端、右側切換政府端／調度端）
```
AWS 憑證由使用者自行以 `aws configure set --profile hackathon ...` 寫入，程式只讀 profile 名稱。

## AWS 使用（帳號 us-west-2，Workshop Studio 提供）
- Amazon Bedrock（Converse API）：`us.anthropic.claude-haiku-4-5`（推薦理由、調度簡報、主管摘要）；備用 `us.anthropic.claude-sonnet-4-6`。符合「僅限 Bedrock／SageMaker AI 基礎模型」規範。LLM 只改寫已算好的數字，不產生數量、風險或派車。
- Amazon S3：`ntpc-youbike-hackathon-329899784315/processed/` 存放正規化資料與原始檔清單（SHA-256）。
- Amazon DynamoDB：`yb_intents`、`yb_tickets`、`yb_alerts`、`yb_tasks`（on-demand），意向／工單／告警／任務盡力寫入，失敗不影響 demo 並在狀態列顯示。
- 預測模型為 scikit-learn HistGradientBoosting（非基礎模型，不受該條限制）；可移至 SageMaker 訓練作業，本次時間內於本機訓練並將成品上傳 S3。

## 真實計算 vs 明示模擬
| 真實計算 | 明示模擬（介面上標示） |
|---|---|
| 13,324,945 筆快照的正規化與品質隔離 | 派車出車、抵達、完成的進度 |
| 30／60／120／180 分鐘可借／可還與零車／零位機率 | 授權店家接單、維修、驗收復役 |
| 站群鄰站（直線 500 m 候選）與到站風險 | 獎勵兌付、優惠券、合作商家 |
| 缺口、供給、組趟、載量、最遲出發、來不及判定 | 通知送達（頁內即時，不是手機推播） |
| 三方案總時間（OSRM 公開路網距離換算）與風險罰時 | 活動出席人數（情境參數，非實到人數） |
| 意向修正（×0.7 轉換率，不重複計算歷史需求） | 調度中心／跨區補給整車（倉儲庫存未提供） |
| 新北即時 API 現況、文化部活動 API | — |
| 服務中斷事件數、超標站分鐘、恢復時間上下界、告警門檻取捨 | 流程各段耗時（回放時鐘＋模擬派工狀態） |

## 不能宣稱的事
快照空滿率不是失敗旅次；候選鄰站存在不是到站成功率；回放與模擬不是實測成效；雙零與容量矛盾未判定根因；學生不是可保證到位的人力；派車、借還介接、商家合作尚未取得。
