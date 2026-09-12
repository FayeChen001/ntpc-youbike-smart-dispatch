# AWS 使用說明（帳號 329899784315・區域 us-west-2）

憑證由使用者以 Workshop Studio 的 CLI credentials 寫入本機 profile `hackathon`，程式只讀 profile 名稱，不在程式碼或對話中保存任何金鑰。所有服務均在主辦提供的 Supported AWS Services List 之內。

## 一、生成式 AI：Amazon Bedrock
符合官網「僅限使用 Amazon Bedrock、SageMaker AI 的基礎模型」規範。

| 項目 | 內容 |
|---|---|
| API | `bedrock-runtime` Converse |
| 模型 | `us.anthropic.claude-haiku-4-5-20251001-v1:0`（主）、`us.anthropic.claude-sonnet-4-6`（備） |
| 用途 | 民眾端推薦理由、調度端每趟任務簡報、政府端主管摘要 |
| 護欄 | 系統提示禁止新增任何數字；只能改寫已算好的事實。呼叫失敗自動退回模板文字，介面上以標籤標示「Bedrock 生成」或「模板文字」 |
| 不做 | 數量、風險機率、派車決策、獎勵點數一律由可驗證模型與規則計算，不交給語言模型 |

## 二、預測模型：Amazon SageMaker AI
| 項目 | 內容 |
|---|---|
| 訓練作業 | `ntpc-youbike-forecast-*`，容器 `sagemaker-scikit-learn:1.2-1-cpu-py3`，執行個體 ml.m5.2xlarge |
| 執行角色 | `NTPCYouBikeSageMakerRole`（本專案建立，附掛 SageMakerFullAccess 與 S3FullAccess） |
| 輸入 | `s3://ntpc-youbike-hackathon-329899784315/sagemaker/input/context.npz`（半小時×站點矩陣與訓練期輪廓） |
| 程式 | `s3://.../sagemaker/source/sourcedir.tar.gz`，內含與本機完全相同的 `features.py`，確保特徵定義一致 |
| 產出 | `s3://.../sagemaker/output/<job>/output/model.tar.gz`，含 4 個尺度 × 4 個模型與 `metrics.json` |
| 切分 | 1–4 月訓練、5 月驗證與早停、6 月保留作最終測試 |

## 三、資料湖與查詢：Amazon S3 + AWS Glue + Amazon Athena
| 項目 | 內容 |
|---|---|
| S3 | `ntpc-youbike-hackathon-329899784315`。`lake/` 資料湖、`processed/` 正規化輸出、`project/` 程式與模型備份、`sagemaker/` 訓練輸入與產出、`athena-results/` 查詢結果 |
| Glue Data Catalog | 資料庫 `ntpc_youbike`，資料表 `observations`（13,324,945 筆）、`stations`（1,583 站鍵） |
| Athena | 政府端「資料湖」分頁直接對 S3 執行 SQL，現場可看掃描量、引擎耗時與完整 SQL，不是本機預先算好的數字 |

Athena 查到的一項關鍵事實：平溪、雙溪、坪林、石碇、烏來、石門等區的零車與零位比例同時都接近 50%，正是「雙零」狀態，應列入待查而非直接派車。這條證據支撐了待查清單的設計。

## 四、地圖與路網：Amazon Location Service
| 項目 | 內容 |
|---|---|
| geo-routes | 民眾端步行段用 Pedestrian 模式、騎乘段用 Scooter 模式（無 Bicycle 模式，屬明示近似）、調度車趟次用 Car 模式多點路線。`Languages=["zh-TW"]` 取得繁體中文逐段指示與中文路名 |
| geo-maps | 衛星圖磚由伺服器代簽後經 `/api/tiles/...` 提供，地圖右上可切換。街道圖仍用 OpenStreetMap 並標註來源 |
| geo-places | 地址地理編碼（`/api/places` 的備援） |

改用 Amazon Location 之前是公開 OSRM，只有汽車路網，短程步行常被繞遠一倍以上；換成 Pedestrian 與 Scooter 模式後距離與指示都正確。

## 五、應用狀態：Amazon DynamoDB
四張隨需計費資料表：`yb_intents`（民眾借還意向）、`yb_tickets`（報修工單）、`yb_alerts`（告警）、`yb_tasks`（調度任務）。寫入為非同步，失敗不影響 demo，狀態列會顯示連線結果。

## 六、未使用與原因
- Amazon Forecast 不在清單內。時間序列改用 SageMaker AI 上的梯度提升樹。
- Amazon SNS、Pinpoint 可寄送真實推播，但本案沒有真實使用者與同意流程，通知一律為頁內模擬。
- Amazon Bedrock Agents、Knowledge Bases 未使用，因為本案的決策必須可稽核，不適合交給自動代理。

## 七、成本控制
DynamoDB 隨需計費、Athena 依掃描量計費（單次查詢掃描量在數十 MB 等級）、Bedrock 依 token 計費且有本地快取、Location Service 有路線結果快取、SageMaker 只有單次訓練作業且設有 5,400 秒上限。
