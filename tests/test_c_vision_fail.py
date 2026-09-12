"""
視覺模型連線層失效的真實重現（不是 mock，也不是假的例外注入）。

把 Bedrock 的 endpoint 指到一個不可路由的位址（10.255.255.1，RFC1918 保留段內
沒有主機會回應），封包是真的被丟掉，botocore 真的會卡在 TCP 連線。
兩種失效路徑用同一個手法、只改 VISION_TIMEOUT_S 就能分別踩到：

  V1  VISION_TIMEOUT_S=1   → join(1+3=4s) 比 connect_timeout(5s) 早到期
                             → 執行緒還活著 → 走「模型未回應」的逾時降級
  V2  VISION_TIMEOUT_S=20  → join(23s) 等得夠久，botocore 自己拋 ConnectTimeoutError
                             → 走「模型呼叫失敗」的例外降級

接著把降級結果照前端的流程送進 /api/c/report，驗證模型掛掉不會擋住人工通報。

用法：YB_BASE=http://127.0.0.1:8791 python3 tests/test_c_vision_fail.py
      （V1/V2 不需要伺服器；V3 之後需要）
"""
import json, os, struct, subprocess, sys, tempfile, time, urllib.request, urllib.error, zlib

BASE = os.environ.get("YB_BASE", "http://127.0.0.1:8791")
if BASE.rstrip("/").endswith(":8787"):
    print("拒絕執行：8787 是三端共用的展示伺服器，本測試會呼叫 /api/reset。請改用其他 port。")
    sys.exit(2)

BLACKHOLE = "https://10.255.255.1:443"     # 不可路由：封包真的被丟棄，不是假的錯誤注入
fails, passed = [], [0]


def check(name, cond, evidence=""):
    ok = bool(cond)
    print(("  PASS " if ok else "  FAIL ") + name + (f"  [{evidence}]" if evidence else ""))
    if ok:
        passed[0] += 1
    else:
        fails.append(name)


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


PROBE = r'''
import os, sys, time, zlib, struct, json
os.environ["AWS_ENDPOINT_URL_BEDROCK_RUNTIME"] = sys.argv[1]
os.environ["VISION_TIMEOUT_S"] = sys.argv[2]
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(sys.argv[3])), "app"))
import report_image as R
def png(w, h, fn):
    raw = b"".join(b"\x00" + b"".join(bytes(fn(x, y)) for x in range(w)) for y in range(h))
    def ch(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw, 9)) + ch(b"IEND", b""))
img = png(420, 320, lambda x, y: (120 + (x % 5), 130, 140))
t0 = time.time()
r = R.analyze(img, "輪胎好像沒氣")
r["_elapsed_s"] = round(time.time() - t0, 1)
r["_status"] = R.STATUS
print("@@JSON@@" + json.dumps(r, ensure_ascii=False))
'''

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def probe(timeout_s, endpoint=BLACKHOLE):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(PROBE)
        path = f.name
    try:
        out = subprocess.run([sys.executable, path, endpoint, str(timeout_s), os.path.join(ROOT, "x")],
                             capture_output=True, text=True, timeout=180, cwd=ROOT)
        line = [l for l in out.stdout.splitlines() if l.startswith("@@JSON@@")]
        if not line:
            print(out.stdout[-800:], out.stderr[-800:])
            return None
        return json.loads(line[0][len("@@JSON@@"):])
    finally:
        os.unlink(path)


FORBIDDEN = ("正常", "沒問題", "可安全", "安全騎乘", "無異常", "車況良好")

print(f"\n[V1] 連線層真實逾時（endpoint={BLACKHOLE}，VISION_TIMEOUT_S=1）")
r1 = probe(1)
check("V1-0 有拿到降級結果", r1 is not None)
if r1:
    check("V1a 模型未回應時 ok 為 false", r1.get("ok") is False, f"ok={r1.get('ok')}")
    check("V1b 降級理由寫明是逾時", "未回應" in (r1.get("degraded_reason") or ""), r1.get("degraded_reason"))
    check("V1c 逾時不產生任何觀察", r1.get("observations") == [], f"observations={r1.get('observations')}")
    check("V1d 逾時標為需人工判讀", r1.get("requires_manual_review") is True)
    check("V1e 逾時不冒充有模型來源", r1.get("model_source") is None, f"model_source={r1.get('model_source')}")
    check("V1f 逾時不推測設備類型", r1.get("suspected_asset_type") == "unknown")
    check("V1g 逾時不得出現「正常／安全」字樣",
          not any(w in json.dumps(r1, ensure_ascii=False) for w in FORBIDDEN))
    check("V1h 計為降級而非成功呼叫",
          (r1.get("_status") or {}).get("degraded") == 1 and (r1.get("_status") or {}).get("calls") == 0,
          f"status={r1.get('_status')}")
    check("V1i 真的等到逾時才回（不是立即失敗）", (r1.get("_elapsed_s") or 0) >= 3.5, f"{r1.get('_elapsed_s')}s")

print(f"\n[V2] 連線層真實失敗（同一個黑洞位址，VISION_TIMEOUT_S=20 讓 botocore 自己拋）")
r2 = probe(20)
check("V2-0 有拿到降級結果", r2 is not None)
if r2:
    check("V2a 呼叫失敗時 ok 為 false", r2.get("ok") is False)
    check("V2b 降級理由是真的連線錯誤，不是假造字串",
          "ConnectTimeout" in (r2.get("degraded_reason") or "") or "Endpoint" in (r2.get("degraded_reason") or ""),
          (r2.get("degraded_reason") or "")[:90])
    check("V2c 呼叫失敗不產生任何觀察", r2.get("observations") == [])
    check("V2d 呼叫失敗標為需人工判讀", r2.get("requires_manual_review") is True)
    check("V2e 呼叫失敗不得出現「正常／安全」字樣",
          not any(w in json.dumps(r2, ensure_ascii=False) for w in FORBIDDEN))

print(f"\n[V3] 模型掛掉不擋人工通報（把降級結果照前端流程送進 /api/c/report）")
s, _ = call("POST", "/api/reset")
_, sts = call("GET", "/api/stations?adjusted=1")
SID = sts["stations"][0]["sid"]
deg = {k: (r1 or {}).get(k) for k in ("ok", "model_source", "observations", "suspected_asset_type",
                                      "extracted_bike_no", "extracted_dock_id", "error_text",
                                      "uncertainties", "requires_manual_review", "degraded_reason", "ms")}

# 3-1 不確定 + 模型掛掉：仍可送出，但不建維修工單
s, rep = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "unsure",
                                        "image_analysis": deg, "free_text": "看起來怪怪的",
                                        "request_id": "vision-down-1"})
check("V3a 模型掛掉時仍可送出回報", s == 200 and rep.get("accepted") is True, f"accepted={rep.get('accepted')}")
check("V3b 送出後狀態是待診斷", rep.get("status") == "pending_triage", rep.get("status"))
check("V3c 未確認的照片不建維修工單", rep.get("ticket_id") is None, f"ticket={rep.get('ticket_id')}")
check("V3d 疑似方向保持 unknown，不猜設備", (rep.get("triage") or {}).get("suspect") == "unknown",
      (rep.get("triage") or {}).get("suspect"))
check("V3e 不適用坐墊提醒（還不知道是哪個設備壞）",
      (rep.get("saddle") or {}).get("applicable") is False, (rep.get("saddle") or {}).get("reason"))
check("V3f 回報內容不得出現「正常／安全」的判定",
      not any(w in json.dumps({k: v for k, v in rep.items() if k != "saddle"}, ensure_ascii=False)
              for w in FORBIDDEN))

# 3-2 明確機械問題 + 模型掛掉：照樣直接建單，不被照片擋住
s, rep2 = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "tire",
                                         "image_analysis": deg, "bike_no": "YB2-VDOWN",
                                         "request_id": "vision-down-2"})
check("V3g 明確機械問題不因模型掛掉而擋住建單", bool(rep2.get("ticket_id")), f"ticket={rep2.get('ticket_id')}")
check("V3h 建出來的單狀態是已受理", (rep2.get("ticket") or {}).get("status") == "reported",
      (rep2.get("ticket") or {}).get("status"))
if rep2.get("ticket_id"):
    s, tk = call("GET", f"/api/ops/tickets/{rep2['ticket_id']}")
    ev = tk.get("evidence") or []
    img_ev = [e for e in ev if (e.get("kind") or e.get("type")) in ("image_observation", "image", "vision")]
    check("V3i 降級的照片不會變成工單上的影像證據", img_ev == [], f"image evidence={len(img_ev)}")
    check("V3j 工單設備狀態仍是疑似", tk.get("asset_state") == "suspect", tk.get("asset_state"))

# 3-3 重送同一個請求，模型掛掉也不會重複建單
s, again = call("POST", "/api/c/report", {"sid": SID, "stage": "before_borrow", "problem": "tire",
                                          "image_analysis": deg, "bike_no": "YB2-VDOWN",
                                          "request_id": "vision-down-2"})
check("V3k 模型掛掉時重送仍然冪等", again.get("idempotent") is True and again.get("ticket_id") == rep2.get("ticket_id"),
      f"idempotent={again.get('idempotent')} ticket={again.get('ticket_id')}")

# 3-4 /api/c/vision/status 要誠實反映降級政策
s, vs = call("GET", "/api/c/vision/status")
pol = vs.get("degrade_policy") or ""
check("V3l 視覺狀態端點公布的政策寫明降級後走人工待判讀，且不回傳假辨識",
      "人工待判讀" in pol and "不回傳假的辨識成功" in pol, pol[:50])
check("V3m 視覺狀態端點標明供應商與逾時秒數",
      "Bedrock" in (vs.get("provider") or "") and isinstance(vs.get("timeout_s"), (int, float)),
      f"provider={vs.get('provider')} timeout_s={vs.get('timeout_s')}")

call("POST", "/api/reset")
print(f"\n=== 視覺模型失效：通過 {passed[0]}　失敗 {len(fails)} ===")
for f in fails:
    print("  未通過：" + f)
sys.exit(1 if fails else 0)
