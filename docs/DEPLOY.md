# 部署（AWS，us-west-2）

對外只開放使用者指定的四個現場位址，見 [DEPLOY_IPS.md](DEPLOY_IPS.md)。

## 現況

| 項目 | 值 |
|---|---|
| EC2 執行個體 | `i-08076f512f308cce9`（t4g.large、Amazon Linux 2023、arm64） |
| 直連位址 | http://35.93.221.246 |
| HTTPS 位址 | https://d2gvisqxis9sbc.cloudfront.net |
| CloudFront 發佈 | `E2YHJ9G5P1S29W` |
| WAF Web ACL | `ntpc-youbike-acl`，預設 Block，允許 IPSet `ntpc-youbike-venue`（IPv4）與 `ntpc-youbike-venue-v6`（IPv6） |
| 安全群組 | `sg-08f4c61e785f6517b` 四個現場 IP 的 80/443/8787；`-` CloudFront 回源 |
| 執行個體角色 | `NTPCYouBikeAppRole`，程式用執行個體角色取得 AWS 權限，機器上沒有任何金鑰 |
| 服務 | systemd `youbike.service`，uvicorn 監聽 80 |

## v2 一站式要多帶的檔案

`deploy.sh` 已經包含，這裡列出來是為了出事時知道要查什麼：

| 檔案 | 少了會怎樣 |
|---|---|
| `data/analytics/{slot_risk.parquet,summary.json,stations.json}` | `/api/v2/analytics` 回 503，整個一站式頁面載不出來 |
| `data/live_history.parquet` | 不會壞，但線上要從零累積 2.5 小時才有完整的模型落後特徵 |
| `analytics/`、`tests/` | 線上無法重算與複驗 |

部署後根網址 `/` 是一站式；情境回放在 `/replay`。

## 公開的唯讀 demo 網址

現況只有四個現場 IP 打得開。要讓評審與主辦也能看：

```bash
./scripts/setup_public_demo.sh            # dry-run，先看會怎麼改
./scripts/setup_public_demo.sh --apply    # 套用
./scripts/setup_public_demo.sh --remove   # 收回
```

它在既有 Web ACL 上**只新增一條**規則：`GET`／`HEAD` 到 `/`、`/v2`、`/api/v2/*`、`/static/*`
一律放行；其餘維持原本的預設 Block。也就是**任何人都能讀，但寫不了**——
建單、指派、結案、領取獎勵這些 POST，以及 `/gov`、`/ops`、`/citizen`、`/replay`，
仍然只有白名單四個現場 IP 進得來。

WAF 規則傳播到全部邊緣節點要幾分鐘。驗證要用**不在白名單的網路**（例如手機 4G）。

## 重新部署

```bash
./deploy.sh
```

打包本機的程式、模型與資料（約 98 MB）上傳 S3，再用 Systems Manager 在執行個體上解壓、安裝相依套件並重啟服務。全程不需要 SSH，也不用開放 22 埠。

## 排錯

```bash
aws ssm send-command --instance-ids i-08076f512f308cce9 --document-name AWS-RunShellScript \
  --parameters 'commands=["journalctl -u youbike -n 60 --no-pager"]'
```

## 權限設計

執行個體角色只給這個專案需要的動作：Bedrock 的 Converse、Location Service 的三個子服務、專案自己的 DynamoDB 資料表、專案自己的 S3 儲存貯體、Athena 與 Glue 的唯讀查詢。另掛 SSM 受管政策以便免 SSH 維運。

## 部署前務必確認

- **`git status --short` 要乾淨。** `deploy.sh` 打包的是工作目錄的當下狀態，不是 HEAD。
  三條線共用同一個工作目錄，別人改到一半還沒提交的檔案會一起被打包上線。
- **新相依套件要寫進 `requirements.txt`。** 本機有裝不代表機器上有。
  2026-09-12 就因為 `python-multipart` 沒列進去，`/api/c/image` 的 `UploadFile`／`Form`
  讓 FastAPI 在 import 時直接 RuntimeError，服務起不來，站台整個掛掉。
- 部署會重啟服務，**線上的回放狀態會被清掉**。

## 注意

- 本機開發用 `AWS_PROFILE=hackathon`；EC2 上不設這個環境變數，走執行個體角色。設成空字串會讓 botocore 把空字串當 profile 名稱而失敗。
- CloudFront 停用快取並轉送全部標頭，因為預測與回放狀態不能被快取。
- 即時事件走 Server-Sent Events；若經 CDN 被緩衝，前端會自動改為每四秒輪詢。

## 部署後怎麼確認真的上去了

**服務跑在 port 80，不是 8787。** 在機器上查健康狀態要 `curl http://127.0.0.1/api/state`，
用 8787 會得到 000 而誤判成掛掉（2026-09-13 踩過）。

安全群組只開四個現場 IP，本機直連可能不通，這時用 SSM 從機器內部查：

```bash
export AWS_PROFILE=hackathon
aws ssm send-command --instance-ids i-08076f512f308cce9 --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl is-active youbike","ss -lntp | head","cd /opt/youbike && md5sum app/server.py app/events.py app/static/gov.html app/static/ops.js"]' \
  --query 'Command.CommandId' --output text
# 再用 aws ssm get-command-invocation --command-id <id> --instance-id i-08076f512f308cce9 \
#   --query StandardOutputContent --output text 取回輸出
```

**驗收方式是比對 md5，不是看時間戳。** 把上面的雜湊跟本機 `md5 -q <檔>` 對，
全部相同才算這一版真的上去了。`deploy.sh` 最後那行 curl 若因來源 IP 被擋而失敗，
不代表部署失敗，要用雜湊確認。

部署會重啟服務、清掉回放狀態（告警、任務、意向、工單、獎勵、情境）。
**重啟前先確認沒人在用**（`journalctl -u youbike --since "-2 min" | grep -c "HTTP/1.1"`），
跑完用 `curl -X POST http://<host>/api/scenario/commute_am -H 'content-type: application/json' -d '{}'` 復原。
