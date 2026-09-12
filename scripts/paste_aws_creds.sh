#!/bin/zsh
# 把 Workshop Studio 複製來的憑證寫進 ~/.aws/credentials 的 [hackathon] 段。
#
# 用法（擇一）：
#   1. 先在 Workshop Studio 按複製，然後直接跑：
#        ./scripts/paste_aws_creds.sh
#      它會讀剪貼簿。
#
#   2. 或者貼到終端機裡：
#        ./scripts/paste_aws_creds.sh -
#      貼上之後按 Ctrl-D 結束。
#
# 三種格式都吃：
#   export AWS_ACCESS_KEY_ID=...            （bash/zsh 那種）
#   [default]\naws_access_key_id=...        （credentials 檔那種）
#   AWS_ACCESS_KEY_ID=...                   （沒有 export 的）
#
# 全程不會把金鑰印出來，也不會寫進任何記錄檔。
set -e
export PATH=$HOME/Library/Python/3.9/bin:$PATH

if [[ "$1" == "-" ]]; then
  echo "請貼上憑證，貼完按 Ctrl-D："
  BLOB=$(cat)
else
  command -v pbpaste >/dev/null || { echo "讀不到剪貼簿，改用： $0 -"; exit 1; }
  BLOB=$(pbpaste)
  echo "已讀取剪貼簿（${#BLOB} 個字元）"
fi

printf '%s' "$BLOB" | python3 - <<'PY'
import os, re, subprocess, sys, configparser

blob = sys.stdin.read()
want = {
    "aws_access_key_id":     r"(?:AWS_ACCESS_KEY_ID|aws_access_key_id)",
    "aws_secret_access_key": r"(?:AWS_SECRET_ACCESS_KEY|aws_secret_access_key)",
    "aws_session_token":     r"(?:AWS_SESSION_TOKEN|aws_session_token)",
}
got = {}
for key, pat in want.items():
    m = re.search(pat + r"\s*=\s*[\"']?([A-Za-z0-9/+=_\-.]+)[\"']?", blob)
    if m:
        got[key] = m.group(1)

missing = [k for k in ("aws_access_key_id", "aws_secret_access_key") if k not in got]
if missing:
    print(f"✗ 這段內容裡找不到：{'、'.join(missing)}")
    print("  確認複製的是 AWS 憑證那一段（含 AWS_ACCESS_KEY_ID 與 AWS_SECRET_ACCESS_KEY）。")
    sys.exit(1)
if "aws_session_token" not in got:
    print("⚠ 沒有 session token。Workshop Studio 的臨時憑證一定會有，")
    print("  沒有的話通常是只複製到一半，請重新複製整段。")

path = os.path.expanduser("~/.aws/credentials")
os.makedirs(os.path.dirname(path), exist_ok=True)
cp = configparser.ConfigParser()
if os.path.exists(path):
    cp.read(path)
if not cp.has_section("hackathon"):
    cp.add_section("hackathon")
for k, v in got.items():
    cp.set("hackathon", k, v)
with open(path, "w") as f:
    cp.write(f)
os.chmod(path, 0o600)

# 只印出遮罩後的樣子，確認寫進去的是不是你以為的那一組
kid = got["aws_access_key_id"]
print(f"✓ 已寫入 {path} 的 [hackathon]")
print(f"  access key：{kid[:4]}…{kid[-4:]}（{len(kid)} 字元）")
print(f"  secret：已設定（{len(got['aws_secret_access_key'])} 字元）")
print(f"  session token：{'已設定（%d 字元）' % len(got['aws_session_token']) if 'aws_session_token' in got else '未提供'}")
PY

echo
echo "驗證中…"
export AWS_PROFILE=hackathon
if OUT=$(aws sts get-caller-identity --output json 2>&1); then
  python3 -c "
import json,sys
d=json.loads('''$OUT''')
print('✓ 憑證有效')
print('  帳號:',d['Account'])
arn=d['Arn']; print('  身分:',arn.split('/')[-1] if '/' in arn else arn)"
  echo
  echo "可以了。回去跟 Claude 說一聲，就能直接跑部署。"
else
  echo "✗ 驗證失敗："
  echo "$OUT" | head -3
  echo
  echo "常見原因：憑證已過期（Workshop Studio 的有時效，要回去重新複製一份）。"
  exit 2
fi
