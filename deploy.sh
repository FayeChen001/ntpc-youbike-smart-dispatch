#!/bin/zsh
# 一鍵重新部署到 EC2。用法： ./deploy.sh
set -e
export PATH=$HOME/Library/Python/3.9/bin:$PATH
export AWS_PROFILE=hackathon
ROOT=/Users/chenhongfei/CC/ntpc-youbike
B=$(mktemp -d)/ybdeploy
INSTANCE=i-08076f512f308cce9
BUCKET=ntpc-youbike-hackathon-329899784315

mkdir -p "$B/reports" "$B/models" "$B/data/processed" "$B/data/analytics"
cd "$ROOT"
cp -r app pipeline docs analytics tests "$B/"
cp README.md requirements.txt "$B/"
cp reports/model_eval.json reports/data_audit.json reports/utilization_analysis.json reports/dispatch_plan.json "$B/reports/"
cp models/context.npz models/meta.json "$B/models/"
cp -r models/sagemaker "$B/models/"
cp data/processed/obs.parquet data/processed/stations.parquet data/processed/neighbors_800m.parquet data/processed/ingest_log.json "$B/data/processed/"
# v2 一站式需要的分析預算結果；缺了 /api/v2/analytics 會回 503，整頁載不出來
cp data/analytics/slot_risk.parquet data/analytics/summary.json data/analytics/stations.json "$B/data/analytics/"
# 即時歷史緩衝：模型的落後特徵靠它，帶上去讓線上不用從零累積兩小時
[[ -f data/live_history.parquet ]] && cp data/live_history.parquet "$B/data/"
find "$B" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$B" -name ".DS_Store" -delete 2>/dev/null || true
cd "$(dirname "$B")" && tar -czf ybdeploy.tar.gz ybdeploy
echo "bundle: $(du -h ybdeploy.tar.gz | cut -f1)"
aws s3 cp ybdeploy.tar.gz s3://$BUCKET/deploy/ybdeploy.tar.gz --only-show-errors
echo "uploaded, deploying..."
CMD=$(aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript --timeout-seconds 1800 \
  --parameters 'commands=["systemctl stop youbike || true","rm -rf /opt/youbike /opt/ybdeploy","aws s3 cp s3://ntpc-youbike-hackathon-329899784315/deploy/ybdeploy.tar.gz /opt/ybdeploy.tar.gz --region us-west-2 --quiet","tar -xzf /opt/ybdeploy.tar.gz -C /opt","mv /opt/ybdeploy /opt/youbike","python3.11 -m pip install -q -r /opt/youbike/requirements.txt","systemctl start youbike","sleep 30","systemctl is-active youbike"]' \
  --query 'Command.CommandId' --output text)
for i in $(seq 1 40); do
  ST=$(aws ssm get-command-invocation --command-id $CMD --instance-id $INSTANCE --query Status --output text 2>/dev/null || echo Pending)
  [[ "$ST" != "InProgress" && "$ST" != "Pending" ]] && break; sleep 15
done
echo "SSM: $ST"
echo "--- 驗收（來源 IP 被擋時改用 SSM 從機器內部查）---"
V=$(aws ssm send-command --instance-ids $INSTANCE --document-name AWS-RunShellScript \
  --parameters 'commands=["curl -s -m 20 http://127.0.0.1/api/state | head -c 200","echo","curl -s -m 25 http://127.0.0.1/api/v2/live | head -c 400","echo","md5sum /opt/youbike/app/v2api.py /opt/youbike/app/live.py /opt/youbike/app/static/v2.html"]' \
  --query 'Command.CommandId' --output text)
sleep 25
aws ssm get-command-invocation --command-id $V --instance-id $INSTANCE --query StandardOutputContent --output text
echo "--- 本機雜湊（要與上面相同才算這一版真的上去了）---"
# 前面 cd 到打包目錄了，這裡要用絕對路徑，不然 md5 會找不到檔案
md5 -q "$ROOT/app/v2api.py" "$ROOT/app/live.py" "$ROOT/app/static/v2.html"
