# v2 接手指南

寫給下一個 session。**先讀這一份，再讀 [V2.md](V2.md)（架構與端點）與 [POSITIONING.md](POSITIONING.md)（對外說法）。**

---

## 0. 三十秒版本

分支 `v2`（已推上 GitHub），`main` 沒動過，隨時可回退。
線上網址 **https://d2gvisqxis9sbc.cloudfront.net** ——任何人都讀得到，只有現場白名單寫得了。
根網址 `/` 是一站式單頁，四個角色分頁；情境回放在 `/replay`。
測試 `python3 tests/test_v2.py` 273 項，全過才算數。

---

## 1. 現在長什麼樣子

| 角色分頁 | 內部分頁 | 重點 |
|---|---|---|
| **政府端** | 決策台／區域／成效與優化／歷史分析 | 決策台是預設：待決事項＋推薦處置＋可改的命令稿＋搜尋框 |
| **微笑單車端** | 監控台／調度人員 | 監控台批次派工；調度人員選區域、排路線、載量守恆、進度回報、班次交接 |
| **民眾端** | 規劃／站況／接案／積分板 | 時間感知推薦、打字找地點、真實導航、賞金獵人接案區 |
| **資料與方法** | — | 模型卡、校準表、資料稽核、與臺北對照、不能宣稱的事 |

---

## 2. 開發環境（照做就能跑）

```bash
cd /Users/chenhongfei/CC/ntpc-youbike
export PATH=$HOME/Library/Python/3.9/bin:$PATH
export AWS_PROFILE=hackathon          # 少了這個，地理編碼與路線會靜默失敗
AWS_PROFILE=hackathon nohup python3 -m uvicorn app.server:app \
  --host 127.0.0.1 --port 8790 > /tmp/v2srv.log 2>&1 &
sleep 55                              # 載模型約 45 秒
python3 tests/test_v2.py              # 273 項
open http://127.0.0.1:8790/
```

**憑證過期時**（Workshop Studio 的臨時憑證會過期）：

```bash
./scripts/paste_aws_creds.sh          # 讀剪貼簿，自動辨認三種格式並驗證
```

---

## 3. 改東西之前一定要知道的五件事

### 3.1 改 `v2.html` 之後一定要驗語法

整份是 inline script，一個全形括號就能讓整頁 JS 掛掉，而且畫面看起來只是「沒反應」。

```bash
python3 -c "import re;s=open('app/static/v2.html',encoding='utf-8').read();open('/tmp/v2.js','w').write(re.findall(r'<script>(.*?)</script>',s,re.S)[-1])" && node --check /tmp/v2.js
```

### 3.2 不要在請求處理函式裡 `import server`

uvicorn 載入的是 `app.server`。用裸名 `import server` 會產生**第二份模組實例**，
重跑整個模組、建立第二個 Predictor 與 LiveStore，而且它的 `V2.init()` 會把 `_CTX` 蓋掉。
症狀是任務突然全部消失、即時層變成 `ok=0 fetched_at=None`。
需要 server 的東西一律在 `V2.init()` 傳進來（`submit_ticket` 就是這樣處理的）。

### 3.3 字串裡不要寫 markdown

前端用 `esc()` 把字串直接塞進 HTML，`**粗體**` 只會顯示成星號。
這個錯逐條修過三次還再犯，現在 `_StripMarkdownRoute` 會在回應邊界統一剝掉，
但**新寫的地方還是別寫**，因為那層只處理 API 回應，不處理前端自己的字串。

### 3.4 迴圈裡不要呼叫會建 DataFrame 的函式

`gov/decisions` 曾經要跑 4.58 秒，因為在逐站迴圈裡呼叫 `risk_ranking()`（每次過濾＋排序 1,500 列）。
改成 `fc.prob_map(120)` 整批查表一次之後變成 0.03 秒。
**加新端點前先量**：`/usr/bin/time -p curl -s -o /dev/null <url>`。

### 3.5 資料裡有四類站會汙染任何排名

| 類別 | 怎麼認 | 為什麼要排除 |
|---|---|---|
| 雙零 | `bikes==0 且 spaces==0` | 語意未定，可能是整站服務中斷也可能是資料中斷 |
| 六月整月零車 | 退場或未投車 25 站 | 那不是調度失敗 |
| 期間內車數完全沒變 | `min==max` | 資料停滯，會同時衝上閒置車與閒置柱兩張榜 |
| 官方暫停營運 | `act != 1` | 整站容量都會算進容量落差 |

這四個坑我每一個都踩過至少一次，排名前幾名被它們佔滿。

---

## 4. 資料與口徑（對外講話前先看這裡）

- 所有歷史比例都是**半小時快照比例**，不是連續中斷時長，也不是旅次成功率。
- 即時模式的風險有兩種，畫面上分開標：**歷史同時段分布**與**模型即時推論**。
- 模型的落後特徵靠 `data/live_history.parquet` 自行累積，要 5 個分箱（2.5 小時）才齊全。
- 我們**沒有**悠遊卡交易、信令人口、微笑單車班表與車隊位置。
  所以不做未設站需求預測、不產生 ETA、不宣稱分析比臺北準。
- 臺北的 +16%／+7%／1,192 輛／5.13 輛是**他們公布的**，引用要標出處。

完整清單見 [V2.md](V2.md) 第五節。

---

## 5. 部署

```bash
git status --short                    # 一定要乾淨，deploy.sh 打包的是工作目錄不是 HEAD
./deploy.sh                           # 約 5–8 分鐘，會比對 md5
```

md5 三個檔（`v2api.py`／`live.py`／`v2.html`）全部相同才算真的上去了。
部署會重啟服務，**清掉事件台帳、任務、獎勵狀態**；即時站況與即時歷史緩衝不受影響。

**WAF 的真實設定**（文件先前寫錯，已更正）：`DefaultAction` 是 **Allow**，
所以網站一直都是公開的。現在多一條 `BlockPublicWrites` 擋掉非白名單的
POST/PUT/PATCH/DELETE。要收掉：`./scripts/setup_public_demo.sh --remove`。

---

## 6. 還沒做的

1. **重新產生分析**：改口徑時要跑 `analytics/build_analytics.py`（約 51 秒）與
   `analytics/build_optimization.py`（約 25 秒）。兩者只讀凍結資料。
2. **離線暫存刻意不做**：要做對需要 service worker 送出佇列與衝突處理，
   做半套比沒有更危險（人以為回報送出去了其實沒有）。畫面上明講沒有離線功能。
3. **`/` 首頁 2.9 秒**：HTML 144 KB（壓縮後 43 KB），CloudFront 目前不快取任何東西。
   HTML 其實是靜態的，可以給短 TTL，但要小心部署後的過期問題。
4. **`test_c_vision_fail` 的 7 項**在憑證過期時會失敗（`NoCredentialsError`），補憑證即恢復。
5. **PPTX 簡報**尚未產出。展示腳本在 [DEMO_V2.md](DEMO_V2.md)。

---

## 7. 這一輪推翻過的判斷（別再犯）

1. 「臺北沒處理還不了」——**錯**，見位率存在、即時圖紅色就是無可還車位且涵蓋新北。
2. 「零位時 45.7% 找不到替代站」——**錯**，那是雙零灌水，真值 3.2%。
3. 「WAF 預設 Block、只有四個 IP 打得開」——**錯**，預設是 Allow，一直都公開。
4. 「閒置柱 5,552 柱」——**錯**，用可還柱位的低百分位算的，大部分站本來就常有空位。
5. 「每日調度 24,815 輛」——**錯**，全新北才約 1.7 萬台車，門檻太鬆把借還潮當貨車。

每一條都是先做出來、再自己驗算才發現的。**新數字先問「這個量級合理嗎」再放上畫面。**
