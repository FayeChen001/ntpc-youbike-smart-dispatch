#!/bin/zsh
# 讓公開網址維持「任何人可讀、只有現場白名單可寫」。
#
# 重要背景（2026-09-13 實測後改寫）：
#   docs/DEPLOY.md 原本寫「WAF 預設 Block」，那是錯的。
#   實際上 Web ACL 的 DefaultAction 是 **Allow**，那兩條 AllowVenueIPs 規則
#   在預設允許的情況下形同虛設——網站早就對全世界開放，連 POST 也是。
#   已用不在白名單的網路實測 CloudFront，正常回傳完整頁面。
#
#   所以要做的不是「開放唯讀」（已經是公開的），而是**擋掉非白名單的寫入**。
#
# 規則邏輯：
#   優先序 0/1  現場白名單 IP → Allow（命中就終止評估，所以現場不受影響）
#   優先序 11   非白名單 + 寫入方法（POST/PUT/PATCH/DELETE）→ Block
#   其餘        走預設 Allow，也就是任何人都能讀
#
# 用法：
#   ./scripts/setup_public_demo.sh            # dry-run，只印出會怎麼改
#   ./scripts/setup_public_demo.sh --apply    # 真的套用
#   ./scripts/setup_public_demo.sh --remove   # 移除這條規則（回到寫入也公開）
set -e
export PATH=$HOME/Library/Python/3.9/bin:$PATH
export AWS_PROFILE=hackathon
ACL_NAME=ntpc-youbike-acl
RULE_NAME=BlockPublicWrites
MODE=${1:-dry}

command -v aws >/dev/null || { echo "找不到 aws cli"; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 || {
  echo "AWS 憑證無效或已過期，請先跑 ./scripts/paste_aws_creds.sh"; exit 2; }

SCOPE=CLOUDFRONT
CF_REGION=us-east-1
ID=$(aws wafv2 list-web-acls --scope $SCOPE --region $CF_REGION \
      --query "WebACLs[?Name=='$ACL_NAME'].Id | [0]" --output text 2>/dev/null || echo "None")
if [[ "$ID" == "None" || -z "$ID" ]]; then
  SCOPE=REGIONAL; CF_REGION=us-west-2
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
RULE = 'BlockPublicWrites'
others = [r for r in rules if r['Name'] != RULE]
default = next(iter(acl.get('DefaultAction', {})), '—')

print(f"\n現有規則（{len(rules)} 條）：")
for r in rules:
    act = next(iter(r.get('Action', {})), '—')
    ipset = ''
    stmt = r.get('Statement', {})
    if 'IPSetReferenceStatement' in stmt:
        ipset = '  ← 比對 IPSet'
    print(f"  優先序 {r['Priority']:>3}  {r['Name']}  動作={act}{ipset}")
print(f"預設動作：{default}")
if default != 'Allow':
    print("\n⚠ 預設動作不是 Allow，這個腳本的前提不成立，請先確認 ACL 設定。")
    sys.exit(1)

# 找出現有的 IPSet 參照，直接沿用，不另建
ipsets = []
for r in others:
    s = r.get('Statement', {})
    ref = s.get('IPSetReferenceStatement')
    if ref:
        ipsets.append(ref['ARN'])
if not ipsets:
    print("\n⚠ 找不到現場 IP 的 IPSet 參照，無法判斷誰是白名單，中止。")
    sys.exit(1)

if mode == '--remove':
    new_rules = others
    print(f"\n[移除] 拿掉 {RULE}。之後**任何人都能寫入**（建案件、派工、領獎勵）。")
else:
    prio = max([r['Priority'] for r in others], default=0) + 10
    methods = ['POST', 'PUT', 'PATCH', 'DELETE']
    rule = {
        'Name': RULE, 'Priority': prio,
        'Action': {'Block': {}},
        'VisibilityConfig': {'SampledRequestsEnabled': True,
                             'CloudWatchMetricsEnabled': True,
                             'MetricName': RULE},
        'Statement': {'OrStatement': {'Statements': [
            {'ByteMatchStatement': {
                'FieldToMatch': {'Method': {}},
                'PositionalConstraint': 'EXACTLY',
                'SearchString': m,
                'TextTransformations': [{'Priority': 0, 'Type': 'UPPERCASE'}]}}
            for m in methods]}},
    }
    new_rules = others + [rule]
    print(f"\n[新增] {RULE}（優先序 {prio}，動作 Block）")
    print(f"  條件：Method ∈ {{{', '.join(methods)}}}")
    print("  為什麼這樣就夠：前面的白名單規則動作是 Allow，命中就終止評估，")
    print("  所以現場 IP 根本不會走到這一條；只有非白名單的寫入會被擋。")
    print("\n  效果：")
    print("    任何人都可以讀（維持現狀，本來就是公開的）")
    print("    只有現場白名單可以建案件、派工、發命令、領獎勵、回報")

json.dump({'rules': new_rules, 'lock': lock,
           'default': acl.get('DefaultAction'),
           'visibility': acl.get('VisibilityConfig')},
          open('/tmp/acl_new.json', 'w'), ensure_ascii=False)
PY

if [[ "$MODE" != "--apply" && "$MODE" != "--remove" ]]; then
  echo "\n這是 dry-run，什麼都沒有改。要套用："
  echo "  ./scripts/setup_public_demo.sh --apply"
  exit 0
fi

LOCK=$(python3 -c "import json;print(json.load(open('/tmp/acl_new.json'))['lock'])")
RULES=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['rules']))")
DEF=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['default']))")
VIS=$(python3 -c "import json;print(json.dumps(json.load(open('/tmp/acl_new.json'))['visibility']))")

aws wafv2 update-web-acl --name $ACL_NAME --scope $SCOPE --id $ID --region $CF_REGION \
  --lock-token "$LOCK" --default-action "$DEF" --visibility-config "$VIS" --rules "$RULES" >/dev/null
echo "\n已套用。WAF 傳播到全部邊緣節點要幾分鐘。"
echo "驗證（要用不在白名單的網路，例如手機 4G）："
echo "  開 https://d2gvisqxis9sbc.cloudfront.net/            → 應該看得到一站式"
echo "  curl -X POST https://d2gvisqxis9sbc.cloudfront.net/api/v2/events/reset  → 應該回 403"
