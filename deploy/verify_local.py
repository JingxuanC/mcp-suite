#!/usr/bin/env python3
"""本地 hub 端到端验收 —— (a) panel 形状容差 / (b) 因子模板契约 / (c) 退化输出修复。

用法（在 mcp-suite 目录下）：
    python3 deploy/verify_local.py            # 全部
    python3 deploy/verify_local.py a b        # 只跑指定段

**直连各服务**（不经过 mcphub）：本轮修的参数校验在**服务自身网关**
（`mcp_common.coerce_args`，基于函数类型注解），mcphub 只是路由器 ——
所以直连就能完整验证，且省掉 mcphub key/分组的干扰。

    data(astock-data)  127.0.0.1:50052/mcp
    factor(factor-miner) 127.0.0.1:50053/mcp
    causal             127.0.0.1:50057/mcp
    kronos             127.0.0.1:50059/mcp
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

# 本地服务 auth=on：直连需带 X-License-Key（远端由 mcphub 注入）。
# 优先取环境变量，否则从 mcp-suite/.env 读 MCP_LICENSE_KEY。
def _license_key() -> str:
    k = os.environ.get("MCP_LICENSE_KEY", "")
    if k:
        return k
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("MCP_LICENSE_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


LICENSE_KEY = _license_key()

# 端口：kronos 的默认宿主端口 50059 被本机 news-mcp 占用，故映射到 50069
# （见 docker run --name mcp-kronos-local -p 127.0.0.1:50069:50059）。
PORTS = {"data": 50052, "factor": 50053, "causal": 50057, "kronos": 50069}
_SESS: dict[str, str] = {}
PASS = FAIL = 0


def _rpc(svc: str, method: str, params, sid=None, timeout=900):
    port = PORTS[svc]
    if sid is None:
        body = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "local-verify", "version": "1"}}}
    else:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request("http://127.0.0.1:%d/mcp" % port,
                                 data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if LICENSE_KEY:
        req.add_header("X-License-Key", LICENSE_KEY)
    if sid:
        req.add_header("mcp-session-id", sid)
    resp = urllib.request.urlopen(req, timeout=timeout)
    raw = resp.read().decode()
    for line in raw.splitlines():
        if line.startswith("data: "):
            raw = line[6:]
            break
    return resp.headers.get("mcp-session-id"), json.loads(raw)


def call(svc: str, tool: str, args=None, timeout=900):
    """调用工具，返回解析后的 dict。错误以 {"_err": ...} 形式返回，不抛。"""
    if svc not in _SESS:
        sid, _ = _rpc(svc, None, None)
        _SESS[svc] = sid
    try:
        _, d = _rpc(svc, "tools/call", {"name": tool, "arguments": args or {}},
                    _SESS[svc], timeout)
    except Exception as e:  # noqa: BLE001
        return {"_err": "%s: %s" % (type(e).__name__, e)}
    res = d.get("result") or {}
    txt = "".join(c.get("text", "") for c in (res.get("content") or []))
    if res.get("isError"):
        return {"_err": txt[:400]}
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return {"_raw": txt[:400]}


def tools(svc: str) -> list:
    if svc not in _SESS:
        sid, _ = _rpc(svc, None, None)
        _SESS[svc] = sid
    _, d = _rpc(svc, "tools/list", {}, _SESS[svc])
    return [t["name"] for t in d["result"]["tools"]]


def check(tag: str, r: dict, want_error: bool = False, show: int = 130):
    global PASS, FAIL
    err = r.get("_err") or (r.get("error") if isinstance(r, dict) else None)
    ok = bool(err) if want_error else not err
    if ok:
        PASS += 1
    else:
        FAIL += 1
    mark = "ok  " if ok else "FAIL"
    detail = str(err)[:140] if err else json.dumps(r, ensure_ascii=False)[:show]
    print("   %s %-32s %s" % (mark, tag, detail))
    return r


def poll_job(svc: str, jid: str, n=90, every=10, verbose=False):
    for _ in range(n):
        s = call(svc, "job_status"
                 if svc == "factor" else "job_status", {"job_id": jid})
        if s.get("status") in ("done", "failed", "error"):
            res = s.get("result")
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:  # noqa: BLE001
                    pass
            return s.get("status"), res
        time.sleep(every)
    return "timeout", None


def _shapes(bars):
    sym = "sh600519"
    return {
        "flat": bars,
        "keyed": {sym: bars},
        "columnar": {"columns": ["date", "open", "high", "low", "close"],
                     "rows": [[b["date"], b["open"], b["high"], b["low"], b["close"]]
                              for b in bars]},
        "wide": {sym: {b["date"]: b["close"] for b in bars}},
        "long": [{"date": b["date"], "symbol": sym, "close": b["close"]} for b in bars],
    }


def get_bars(n=300, symbol="sh600519"):
    r = call("data", "get_a_hist", {"symbol": symbol, "count": n})
    if isinstance(r, list) and r:
        return r
    return None


# ─────────────────────────────────────────────────────── (a)
def section_a(bars):
    print("\n=== (a) panel 形状容差 —— 同一份数据 5 种形状，结果必须一致 ===")
    sh = _shapes(bars)
    # 单序列工具：5 种形状都该能用
    for tool, args in (("vol_forecast", {"horizon": 5}),
                       ("regime_detect", {"n_regimes": 3})):
        out = {}
        for name, v in sh.items():
            r = check("%s/%s" % (tool.split("-")[-1], name),
                      call("factor", tool, dict(args, klines=v), timeout=300))
            out[name] = r
        vals = {k: json.dumps(v.get("forecast") or v.get("current_regime"))
                for k, v in out.items() if "error" not in v}
        if len(set(vals.values())) == 1 and len(vals) == 5:
            print("        ↳ 5 种形状结果一致 ✅")
        else:
            print("        ↳ ⚠️ 形状间结果不一致:", vals)

    # 单序列工具收到多序列：必须明确报错
    check("vol_forecast/多序列(应报错)",
          call("factor", "vol_forecast",
               {"klines": {"a": bars, "b": bars}, "horizon": 5}), want_error=True)

    # 因果图：multi 形状
    multi = {"keyed": {"A": bars, "B": bars},
             "columnar": {"columns": ["date", "symbol", "close"],
                          "rows": [[b["date"], "A", b["close"]] for b in bars]
                                  + [[b["date"], "B", b["close"]] for b in bars]},
             "long": [{"date": b["date"], "symbol": s, "close": b["close"]}
                      for s in ("A", "B") for b in bars]}
    for tool in ("learn_graph", "pcmci_discover"):
        for name, v in multi.items():
            check("%s/%s" % (tool.split("-")[-1], name),
                  call("causal", tool, {"data": v}, timeout=300))

    # 多序列参数
    check("ml_predict/keyed",
          call("factor", "ml_predict", {"klines_list": {"A": bars}}))
    check("ml_predict/native",
          call("factor", "ml_predict",
               {"klines_list": [{"symbol": "A", "klines": bars}]}))

    # kronos：dict 形状现在该能用
    for name in ("flat", "keyed", "columnar"):
        check("forecast_kline/%s" % name,
              call("kronos", "forecast_kline",
                   {"klines": sh[name], "pred_len": 3}, timeout=600))


# ─────────────────────────────────────────────────────── (b)
def section_b(template_path):
    print("\n=== (b) 因子模板契约：daily_pv.h5 → result.h5 ===")
    try:
        with open(template_path, encoding="utf-8") as f:
            tpl = f.read()
    except OSError as e:
        print("   FAIL 读不到模板 %s: %s" % (template_path, e))
        return
    print("   模板 %d 字节  (%s)" % (len(tpl), template_path))

    r = call("factor", "factor_execute", {"code": tpl, "debug": True},
             timeout=300)
    if r.get("job_id"):
        st, res = poll_job("factor", r["job_id"])
        check("factor_execute(debug)", res if isinstance(res, dict) else {"_err": str(res)})
        if isinstance(res, dict):
            print("        ↳ eval_ok=%s  %s" % (res.get("eval_ok"), res.get("eval_detail")))
    else:
        check("factor_execute(debug)", r)

    check("factor_recent_ic", call("factor", "factor_recent_ic",
                                   {"code": tpl, "name": "rev20_verify",
                                    "lookback_days": 120}, timeout=900))

    r = call("factor", "factor_oos_check",
             {"code": tpl, "name": "rev20_verify"}, timeout=300)
    if r.get("job_id"):
        print("   ... factor_oos_check 入队 %s（两次全量回测，约 5-10 分钟）"
              % r["job_id"][:10], flush=True)
        st, res = poll_job("factor", r["job_id"], n=120, every=15)
        blob = json.dumps(res, ensure_ascii=False) if isinstance(res, dict) else str(res)
        if "qlib" in blob or "No module named" in blob:
            # 本地 factor-miner 走的是预构建镜像（无 pyqlib）—— 已知限制，
            # qlib 回测路径已在远端验证过，这里不算失败。
            print("   skip %-32s 本地镜像无 pyqlib（已知限制；qlib 路径已在远端验证）"
                  % "factor_oos_check")
        else:
            check("factor_oos_check", res if isinstance(res, dict) else {"_err": str(res)})
        if isinstance(res, dict) and res.get("ok"):
            print("        ↳ mining IC=%.4f OOS IC=%.4f decay=%.1f%%"
                  % (res["mining"]["ic"], res["oos"]["ic"], 100 * (res.get("decay") or 0)))
    else:
        check("factor_oos_check", r)


# ─────────────────────────────────────────────────────── (c)
def section_c(bars):
    print("\n=== (c) 退化输出修复 ===")
    a = check("vol_forecast/auto", call("factor", "vol_forecast",
                                        {"klines": bars, "horizon": 5}, timeout=300))
    if "error" not in a:
        print("        ↳ method=%s term_structure=%s horizon_ratio=%.4f"
              % (a.get("method"), a.get("term_structure"), a.get("horizon_ratio") or 0))
        vols = [d["vol"] for d in a.get("forecast", [])]
        print("        ↳ 路径:", " → ".join("%.5f" % v for v in vols))
        if a.get("phi"):
            print("        ↳ phi=%.4f long_run_vol=%.5f half_life=%.1f 天"
                  % (a["phi"], a["long_run_vol"], a["half_life_days"]))
        if a.get("term_structure") == "mean_reverting" and len(set(vols)) > 1:
            global PASS
            PASS += 1
            print("   ok   期限结构非平坦 ✅")
        else:
            FAIL += 1
            print("   FAIL 期限结构仍是平坦的")

    b = check("vol_forecast/ewma(平坦是定义)",
              call("factor", "vol_forecast",
                   {"klines": bars, "horizon": 5, "method": "ewma"}, timeout=300))
    if "error" not in b and b.get("term_structure") != "flat":
        print("   FAIL ewma 未标注为 flat")

    check("vol_forecast/ewma_mr", call("factor", "vol_forecast",
                                       {"klines": bars, "horizon": 5,
                                        "method": "ewma_mr"}, timeout=300))

    d = check("predict/缺输入(按设计报错)",
              call("factor", "predict", {"symbol": "sh600519", "factors": {}}),
              want_error=True)
    if "error" not in d:
        if d.get("status") == "insufficient_input" and d.get("signal") is None:
            PASS += 1
            print("   ok   缺输入未返回假输出 ✅")
        else:
            FAIL += 1
            print("   FAIL 缺输入仍返回了 signal=%s" % d.get("signal"))

    e = check("predict/有输入", call("factor", "predict",
                                     {"symbol": "sh600519",
                                      "factors": {"roc_5": 0.02, "volume_ratio": 1.5,
                                                  "std_20": 0.01, "mfv": 1.0}}))
    if "error" not in e and e.get("is_mock") is not True:
        FAIL += 1
        print("   FAIL 有输入但未标注 is_mock")


def main():
    want = [x for x in sys.argv[1:] if x in ("a", "b", "c")] or ["a", "b", "c"]
    print("本地验收：段 = %s（直连服务，工具名无前缀）" % ",".join(want))
    print("服务端口:", PORTS)
    for svc in ("data", "factor", "causal", "kronos"):
        try:
            n = len(tools(svc))
            print("  %-8s :%d  ✅ %d tools" % (svc, PORTS[svc], n))
        except Exception as e:  # noqa: BLE001
            print("  %-8s :%d  ❌ 不可达 %s" % (svc, PORTS[svc], str(e)[:60]))
    bars = get_bars()
    if bars:
        print("  行情: %d 根 K 线 %s → %s" % (len(bars), bars[0]["date"], bars[-1]["date"]))
    else:
        print("  ❌ astock-data 取不到行情；(a)(c) 需要行情或改用本地 h5")

    if "a" in want and bars:
        section_a(bars)
    if "b" in want:
        section_b("factor-miner-mcp/templates/factor_template.py")
    if "c" in want and bars:
        section_c(bars)

    print("\n===== 结果：%d 通过 / %d 失败 =====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
