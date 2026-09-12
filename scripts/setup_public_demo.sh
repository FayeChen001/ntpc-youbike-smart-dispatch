#!/bin/zsh
# 在既有的 WAF Web ACL 上加一條「公開唯讀」規則：
#   任何來源的 GET / HEAD 到 一站式頁面與 /api/v2/* 讀取端點 → 允許
#   其他一律維持原本的預設 Block（只有白名單四個現場 IP 進得來）
#
# 也就是說：評審與主辦可以打開網站看，但建單、指派、結案、領取獎勵這些寫入動作
# 仍然只有現場白名單能做。
#
# 用法：
#   ./scripts/setup_public_demo.sh            # dry-run，只印出會怎麼改，不動任何東西
#   ./scripts/setup_public_demo.sh --apply    # 真的套用
#   ./scripts/setup_public_demo.sh --remove   # 移除這條規則，回到全白名單
#
# 這個腳本只新增／移除自己這一條規則（名稱 PublicReadOnlyV2），不碰既有規則。
set -e
export PATH=$HOME/Library/Python/3.9/bin:$PATH
export AWS_PROFILE=hackathon
REGION=us-west-2
ACL_NAME=ntpc-youbike-acl
RULE_NAME=PublicReadOnlyV2
MODE=${1:-dry}

command -v aws >/dev/null || { echo "找不到 aws cli"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "AWS 憑證無效或已過期。請先重貼 hackathon profile 的憑證："
  echo "  aws configure set --profile hackathon aws_access_key_id ..."
  echo "  aws configure set --profile hackathon aws_secret_access_key ..."
  echo "  aws configure set --profile hackathon aws_session_token ..."
  exit 2; }

# CloudFront 用的 WAF 一律在 us-east-1 的 CLOUDFRONT scope
SCOPE=CLOUDFRONT
CF_REGION=us-east-1
ID=$(aws wafv2 list-web-acls --scope $SCOPE --region $CF_REGION \
      --query "WebACLs[?Name=='$ACL_NAME'].Id | [0]" --output text 2>/dev/null || echo "None")
if [[ "$ID" == "None" || -z "$ID" ]]; then
  SCOPE=REGIONAL; CF_REGION=$REGION
  ID=$(aws wafv2 list-web-acls --scope $SCOPE --region $CF_REGION \
        --query "WebACLs[?Name=='$ACL_NAME'].Id | [0]" --output text)
fi
[[ "$ID" == "None" || -z "$ID" ]] && { echo "找不到 Web ACL $ACL_NAME"; exit 1; }
echo "Web ACL: $ACL_NAME ($SCOPE, $CF_REGION) id=$ID"

aws wafv2 get-web-acl --name $ACL_NAME --scope $SCOPE --id $ID --region $CF_REGION > /tmp/acl.json

python3 - "$MODE" <<'PY'
import json, sys
mode = sys.argv[1]
d = json.load(open('/tmp/acl.json'))
acl, lock = d['WebACL'], d['LockToken']
rules = acl.get('Rules', [])
RULE = 'PublicReadOnlyV2'
existing = [r for r in rules if r['Name'] == RULE]
others = [r for r in rules if r['Name'] != RULE]

print(f"\n現有規則（{len(rules)} 條）：")
for r in rules:
    act = next(iter(r.get('Action', {})), '—')
    print(f"  優先序 {r['Priority']:>3}  {r['Name']}  動作={act}")
print(f"預設動作：{next(iter(acl.get('DefaultAction', {})), '—')}")

if mode == '--remove':
    new_rules = others
    print(f"\n[移除] 會拿掉 {RULE}，剩 {len(new_rules)} 條，回到全白名單。")
else:
    prio = max([r['Priority'] for r in others], default=0) + 10
    # 只放行讀取：GET/HEAD，且路徑限定在 v2 的頁面與讀取端點
    starts = ['/v2', '/api/v2/', '/static/', '/manifest.webmanifest']
    rule = {
        'Name': RULE, 'Priority': prio,
        'Action': {'Allow': {}},
        'VisibilityConfig': {'SampledRequestsEnabled': True,
                             'CloudWatchMetricsEnabled': True,
                             'MetricName': RULE},
        'Statement': {'AndStatement': {'Statements': [
            {'OrStatement': {'Statements': [
                {'ByteMatchStatement': {
                    'FieldToMatch': {'Method': {}},
                    'PositionalConstraint': 'EXACTLY',
                    'SearchString': m,
                    'TextTransformations': [{'Priority': 0, 'Type': 'UPPERCASE'}]}}
                for m in ('GET', 'HEAD')]}},
            {'OrStatement': {'Statements': [
                {'ByteMatchStatement': {
                    'FieldToMatch': {'UriPath': {}},
                    'PositionalConstraint': 'STARTS_WITH',
                    'SearchString': p,
                    'TextTransformations': [{'Priority': 0, 'Type': 'NONE'}]}}
                for p in starts] + [
                {'ByteMatchStatement': {
                    'FieldToMatch': {'UriPath': {}},
                    'PositionalConstraint': 'EXACTLY',
                    'SearchString': '/',
                    'TextTransformations': [{'Priority': 0, 'Type': 'NONE'}]}}]}},
        ]}},
    }
    new_rules = others + [rule]
    print(f"\n[新增] {RULE}（優先序 {prio}，動作 Allow）")
    print("  條件：Method ∈ {GET, HEAD}　且　路徑為 / 或以下列開頭：")
    for p in starts:
        print(f"          {p}")
    print("  效果：任何人可以讀 v2 一站式與 /api/v2/* 的 GET 端點；")
    print("        POST（建單、指派、結案、領獎勵）與 /gov /ops /citizen /replay 仍只有白名單進得來。")

out = dict(acl)
out['Rules'] = new_rules
for k in ('ARN', 'Id', 'Name', 'Capacity', 'LabelNamespace', 'ManagedByFirewallManager'):
    out.pop(k, None)
json.dump({'rules': new_rules, 'lock': lock,
           'default': acl.get('DefaultAction'),
           'visibility': acl.get('VisibilityConfig'),
           'description': acl.get('Description', '')},
          open('/tmp/acl_new.json', 'w'), ensure_ascii=False)
PY

if [[ "$MODE" != "--apply" && "$MODE" != "--remove" ]]; then
  echo "\n這是 dry-run，什麼都沒有改。確認上面的內容沒問題後，再跑："
  echo "  ./scripts/setup_public_demo.sh --apply"
  exit 0
fi

LOCK=$(python3 -c "import json;print(json.load(open('/tmp/acl_new.json'))['lock'])")
RULES=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['rules']))")
DEF=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['default']))")
VIS=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['visibility']))")

aws wafv2 update-web-acl --name $ACL_NAME --scope $SCOPE --id $ID --region $CF_REGION \
  --lock-token "$LOCK" --default-action "$DEF" --visibility-config "$VIS" --rules "$RULES"
echo "\n已套用。WAF 規則傳播到全部邊緣節點通常要幾分鐘。"
echo "公開網址：https://d2gvisqxis9sbc.cloudfront.net/"
echo "驗證（用不在白名單的網路，例如手機 4G）："
echo "  開 https://d2gvisqxis9sbc.cloudfront.net/            → 應該看得到一站式"
echo "  curl -X POST https://d2gvisqxis9sbc.cloudfront.net/api/v2/events  → 應該被擋（403）"
