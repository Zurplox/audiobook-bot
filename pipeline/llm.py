"""Unified LLM caller with the 4-rung fallback chain.
Quota-type errors jump to the next rung (pooled keys share limits).
Garbage/truncation/transient errors retry in place, then fall through.
Every success is length-validated (catches NIM's silent truncation)."""
import json
import time
import urllib.request
import urllib.error

from . import config

QUOTA_HINTS = ("429", "rate", "quota", "limit", "credit", "balance", "402", "insufficient")


def _post(base, model, key, messages, max_tokens, timeout=300):
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _text_of(resp):
    try:
        ch = (resp.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


def _is_quota(err_text):
    low = err_text.lower()
    return any(h in low for h in QUOTA_HINTS)


def call_chain(messages, max_tokens, min_len, tag="chunk"):
    """Returns (text, rung_used, fail_counts). Raises RuntimeError if all rungs fail."""
    fail_counts = {}
    for name, base, model, key, tries, waits in config.CHAIN:
        if not key:
            print(f"{tag}: rung {name} has no key, skipping")
            continue
        for a in range(tries):
            try:
                out = _text_of(_post(base, model, key, messages, max_tokens))
                if len(out) >= min_len:
                    if a or fail_counts:
                        print(f"{tag}: {name} ok after {fail_counts}")
                    return out, name, fail_counts
                print(f"{tag}: {name} short output ({len(out)}<{min_len}), attempt {a + 1}/{tries}")
            except urllib.error.HTTPError as e:
                detail = f"HTTP {e.code} " + e.read()[:200].decode("utf-8", "ignore")
                print(f"{tag}: {name} {detail[:120]}, attempt {a + 1}/{tries}")
                if e.code in (400, 401, 403, 404):
                    print(f"{tag}: hard error on {name} (retries won't fix it) -> next rung")
                    fail_counts[name] = fail_counts.get(name, 0) + 1
                    break
                if _is_quota(detail):
                    print(f"{tag}: quota-type error on {name} -> next rung")
                    break
            except Exception as e:
                print(f"{tag}: {name} error {str(e)[:120]}, attempt {a + 1}/{tries}")
            fail_counts[name] = fail_counts.get(name, 0) + 1
            if a < len(waits):
                time.sleep(waits[a])
    raise RuntimeError(f"all rungs failed for {tag}: {fail_counts}")


def health_check():
    """Micro-probe every rung (minute-0 discovery of a dead fallback). Never raises."""
    ok = {}
    for name, base, model, key, _, _ in config.CHAIN:
        if not key:
            ok[name] = "no-key"; continue
        try:
            out = _text_of(_post(base, model, key,
                                 [{"role": "user", "content": "reply OK"}], 5, timeout=120))
            ok[name] = "ok"
        except urllib.error.HTTPError as e:
            ok[name] = f"HTTP {e.code}"
        except Exception as e:
            ok[name] = str(e)[:60]
    print("health:", ok)
    return ok
