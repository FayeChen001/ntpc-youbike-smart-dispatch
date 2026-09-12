# B 線（微笑單車派車端）測試

離線（不需伺服器，fixture 預期值手算）：
```bash
python3 tests/test_b_tickets.py      # 建單服務 38 項：去重、冪等、狀態分離、坐墊、版本鎖
python3 tests/test_b_ledger.py       # N03/N04/N05 27 項：資源帳、取消釋放、逐站期限
python3 tests/test_b_dispatch.py     # 組趟 11 項：需要 8787 或 8789 提供真實站況
```

需要伺服器（預設 `http://127.0.0.1:8789`，避免動到共用的 8787）：
```bash
python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8789   # 載入約 45 秒
python3 tests/test_b_http_tickets.py   # 24 項：兩個建單入口走同一服務
python3 tests/test_b_http_cycle.py     # 20 項：規劃週期、任務動作、跨區逾時
python3 tests/test_b_closed_loop.py    # 19 項：C 回報 → B 派查 → 處理 → 驗收
```

跨端一致性（**目前預期有 3 項失敗，等 C 修**）：
```bash
python3 tests/test_b_reset_crossend.py
```
B 的部分（工單清空、資源帳歸零）通過；失敗的三項都是 `api_reset` 沒有清掉 C 的
`CREPORTS`，導致回報、坐墊標記與 request_id 冪等表在 reset 後仍指向已刪除的工單。
修法見 `docs/HANDOFF_B.md` 的「待 C 調整」。

要對別的 port 跑，設 `YB_BASE`：
```bash
YB_BASE=http://127.0.0.1:8787 python3 tests/test_b_http_tickets.py
```

注意：HTTP 套件會呼叫 `/api/reset`，跑完會清掉回放狀態（告警、任務、意向、工單、獎勵）。
**對共用的 8787 跑之前一定要先在 chat 講**；跑完請用
`curl -X POST http://127.0.0.1:8787/api/scenario/commute_am -d '{}' -H 'content-type: application/json'`
把情境與任務復原。
`test_b_http_cycle.py` 會自己把時鐘推一步讓伺服器重排任務，不依賴前一支測試留下的狀態。
