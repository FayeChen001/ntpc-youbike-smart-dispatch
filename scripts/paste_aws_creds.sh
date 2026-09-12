#!/bin/zsh
# 把 Workshop Studio 複製來的憑證寫進 ~/.aws/credentials 的 [hackathon] 段。
#
# 用法（擇一）：
#   ./scripts/paste_aws_creds.sh       讀剪貼簿
#   ./scripts/paste_aws_creds.sh -     貼到終端機，貼完按 Ctrl-D
#
# 三種格式都吃：
#   export AWS_ACCESS_KEY_ID=...            （bash/zsh 那種）
#   [default] + aws_access_key_id=...       （credentials 檔那種）
#   AWS_ACCESS_KEY_ID=...                   （沒有 export 的）
#
# 全程不會把金鑰印出來。內容先落到一個 600 權限的暫存檔，結束時一定刪掉。
set -e
export PATH=$HOME/Library/Python/3.9/bin:$PATH

TMP=$(mktemp /tmp/awscreds.XXXXXX)
chmod 600 "$TMP"
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT INT TERM

if [[ "$1" == "-" ]]; then
  echo "請貼上憑證，貼完按 Ctrl-D："
  cat > "$TMP"
else
  command -v pbpaste >/dev/null || { echo "讀不到剪貼簿，改用： $0 -"; exit 1; }
  pbpaste > "$TMP"
  echo "已讀取剪貼簿（$(wc -c < "$TMP" | tr -d ' ') 個位元組）"
fi

# 注意：Python 程式碼走 heredoc，憑證內容走檔案。
# 兩者都用 stdin 會互相蓋掉——第一版就是這樣壞的。
BLOB_FILE="$TMP" python3 <<'PY'
import configparser
import os
import re
import sys

blob = open(os.environ["BLOB_FILE"], encoding="utf-8", errors="replace").read()

# 最常見的誤複製：Workshop Studio 的主控台登入連結，不是憑證
if "signin.aws.amazon.com" in blob or "SigninToken=" in blob:
    print("✗ 你複製到的是 AWS 主控台的登入連結，不是 CLI 憑證。")
    print()
    print("  在 Workshop Studio 的左側面板找「Get AWS CLI credentials」")
    print("  （有些版本叫 AWS CLI / Command line access / 終端機存取）。")
    print("  點開會看到三行 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN，")
    print("  複製那一整段之後再跑一次這個指令。")
    sys.exit(1)

want = {
    "aws_access_key_id":     r"(?:AWS_ACCESS_KEY_ID|aws_access_key_id)",
    "aws_secret_access_key": r"(?:AWS_SECRET_ACCESS_KEY|aws_secret_access_key)",
    "aws_session_token":     r"(?:AWS_SESSION_TOKEN|aws_session_token)",
}
got = {}
for key, pat in want.items():
    m = re.search(pat + r"\s*[=:]\s*[\"']?([A-Za-z0-9/+=_\-.]+)[\"']?", blob)
    if m:
        got[key] = m.group(1)

missing = [k for k in ("aws_access_key_id", "aws_secret_access_key") if k not in got]
if missing:
    print(f"✗ 這段內容裡找不到：{'、'.join(missing)}")
    print(f"  （讀到 {len(blob)} 個字元，開頭是：{blob[:60].strip()!r}…）")
    print("  要複製的是含 AWS_ACCESS_KEY_ID 與 AWS_SECRET_ACCESS_KEY 的那一段。")
    sys.exit(1)

if "aws_session_token" not in got:
    print("⚠ 沒有 session token。Workshop Studio 的臨時憑證一定會有，")
    print("  沒有的話通常是只複製到一半，建議重新複製整段。")

path = os.environ.get("AWS_CREDS_PATH") or os.path.expanduser("~/.aws/credentials")
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

kid = got["aws_access_key_id"]
print(f"✓ 已寫入 {path} 的 [hackathon]")
print(f"  access key：{kid[:4]}…{kid[-4:]}（{len(kid)} 字元）")
print(f"  secret：已設定（{len(got['aws_secret_access_key'])} 字元）")
tok = got.get("aws_session_token")
print(f"  session token：{f'已設定（{len(tok)} 字元）' if tok else '未提供'}")
PY

echo
echo "驗證中…"
export AWS_PROFILE=hackathon
if OUT=$(aws sts get-caller-identity --output text --query '[Account,Arn]' 2>&1); then
  echo "✓ 憑證有效"
  echo "  $OUT"
  echo
  echo "可以了。回去跟 Claude 說一聲，就能直接跑部署。"
else
  echo "✗ 驗證失敗："
  echo "$OUT" | head -3
  echo
  echo "最常見原因：這組憑證本身已經過期。Workshop Studio 的憑證有時效，"
  echo "回去重新複製一份最新的，再跑一次這個指令。"
  exit 2
fi
