# 對外開放連線的允許來源 IP

使用者於 2026-09-12 指定，AWS 部署時所有對外入口只允許以下四個位址連入，其餘一律拒絕。

```
60.250.71.45
61.222.117.53
59.125.121.41
60.250.71.43
49.216.50.19          # 2026-09-12 追加：使用者個人熱點的 IPv4 出口
```

IPv6（2026-09-12 追加）：

```
2402:7500:588:6262::/64   # 同一個個人熱點的 IPv6 出口，用 /64 前綴
```

**IPv6 是必要的，不是可選的。** WAF 的 IPSet 分 IPv4 與 IPv6 兩種，一組只能放一種。
只放 IPv4 的話，現代瀏覽器與 curl 預設會優先走 IPv6 連 CloudFront，結果照樣被擋成 403，
而且錯誤畫面跟「IP 不在白名單」完全一樣，很難看出是版本問題。2026-09-12 就踩到這個：
`curl -4` 回 200、`curl -6` 回 403，才確認是這個原因。

目前的 WAF 設定：

| 元件 | 名稱 | 內容 |
|---|---|---|
| Web ACL | `ntpc-youbike-acl` | 預設 Block，兩條 Allow 規則 |
| 規則 0 | `AllowVenueIPs` | IPSet `ntpc-youbike-venue`（IPv4） |
| 規則 1 | `AllowVenueIPv6` | IPSet `ntpc-youbike-venue-v6`（IPv6） |
| 安全群組 | `sg-08f4c61e785f6517b` | 五個 IPv4 的 80／443／8787 |

安全群組目前只有 IPv4 規則，所以**直連 `http://35.93.221.246` 只走得通 IPv4**；
IPv6 的連線請走 CloudFront。

## 要套用的位置

| 元件 | 套用方式 |
|---|---|
| Application Load Balancer 或 EC2 | Security Group 入站規則，443 與 80 只開這四個 /32 |
| CloudFront | AWS WAF Web ACL 的 IPSet 比對規則，非清單內來源回 403 |
| API Gateway | Resource Policy 的 `aws:SourceIp` 條件，或 WAF IPSet |
| S3 靜態網站 | Bucket Policy 的 `aws:SourceIp` 條件 |
| App Runner | 前面掛 CloudFront 加 WAF，服務本身設為私有 |

## 建立 IPSet 與 WAF 的指令

```bash
aws wafv2 create-ip-set --name ntpc-youbike-allow --scope CLOUDFRONT --region us-east-1 \
  --ip-address-version IPV4 \
  --addresses 60.250.71.45/32 61.222.117.53/32 59.125.121.41/32 60.250.71.43/32
```

Security Group 版本：

```bash
for ip in 60.250.71.45 61.222.117.53 59.125.121.41 60.250.71.43; do
  aws ec2 authorize-security-group-ingress --group-id <SG_ID> \
    --protocol tcp --port 443 --cidr $ip/32
done
```

## 注意

- 這四個位址看起來是固定的辦公室或機房出口，若現場改用行動網路或會場 Wi-Fi，來源位址會不同，demo 當天會連不上。請確認簡報當下的出口 IP 是否在清單內，必要時臨時加入。
- 本機 demo（127.0.0.1:8787）不受此限制，不需要開放任何對外連線。
