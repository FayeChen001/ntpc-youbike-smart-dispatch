"""
llm.py — 生成式 AI 介面。正式路徑：Amazon Bedrock Converse（Claude）。
沒有憑證或呼叫失敗時退回「模板文字」，並在回傳中標示 source，前端會顯示標籤。
用途限定：推薦理由、調度簡報、告警摘要、工單摘要、活動文本整理。數量與風險不由 LLM 計算。
"""
import os, json, time, hashlib, threading
from concurrent.futures import ThreadPoolExecutor

REGION = os.environ.get("AWS_REGION", "us-west-2")
PROFILE = os.environ.get("AWS_PROFILE", "hackathon")
MODEL_FAST = os.environ.get("BEDROCK_MODEL_FAST", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MODEL_SMART = os.environ.get("BEDROCK_MODEL_SMART", "us.anthropic.claude-sonnet-4-6")

_client = None; _lock = threading.Lock(); _cache = {}; _pool = ThreadPoolExecutor(max_workers=4)
STATUS = {"enabled": True, "last_ok": None, "last_error": None, "calls": 0, "fallbacks": 0}

def _get_client():
    global _client
    with _lock:
        if _client is None:
            import sys as _s, os as _o
            _s.path.insert(0, _o.path.dirname(__file__))
            from awsloc import session as _sess
            _client = _sess().client("bedrock-runtime", region_name=REGION)
        return _client

SYSTEM = ("你是新北市 YouBike 雙端協同服務的文字助理。只能改寫、摘要、說明「已提供的數字與事實」，"
          "不得自行新增數量、時間或成效；沒有的資料要說「未提供」。用繁體中文、口語、精簡，不用表情符號，不用 Markdown 符號（不要星號、井字號、項目符號）。")

def generate(prompt, fallback, smart=False, max_tokens=220, cache_key=None):
    """同步呼叫。回傳 {text, source, model, ms}。失敗即退回 fallback。"""
    key = cache_key or hashlib.md5((("S" if smart else "F") + prompt).encode()).hexdigest()
    if key in _cache: return _cache[key]
    if not STATUS["enabled"]:
        STATUS["fallbacks"] += 1
        return {"text": fallback, "source": "template", "model": None, "ms": 0}
    t = time.time()
    try:
        r = _get_client().converse(modelId=MODEL_SMART if smart else MODEL_FAST, system=[{"text": SYSTEM}],
                                   messages=[{"role": "user", "content": [{"text": prompt}]}],
                                   inferenceConfig={"maxTokens": max_tokens, "temperature": 0.3})
        text = r["output"]["message"]["content"][0]["text"].strip()
        out = {"text": text, "source": "bedrock", "model": MODEL_SMART if smart else MODEL_FAST, "ms": int((time.time()-t)*1000)}
        STATUS["calls"] += 1; STATUS["last_ok"] = time.time()
    except Exception as e:
        STATUS["fallbacks"] += 1; STATUS["last_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out = {"text": fallback, "source": "template", "model": None, "ms": int((time.time()-t)*1000), "error": STATUS["last_error"]}
    _cache[key] = out
    return out

def generate_async(prompt, fallback, smart=False, max_tokens=220, cache_key=None):
    return _pool.submit(generate, prompt, fallback, smart, max_tokens, cache_key)
