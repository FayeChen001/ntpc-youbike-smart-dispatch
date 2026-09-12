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
