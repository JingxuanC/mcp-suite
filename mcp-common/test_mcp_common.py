"""mcp_common 单测 —— 覆盖 5 个服务归并而来的全部公共原语。

运行：``python3 -m pytest test_mcp_common.py -q``
"""
import json
import logging

import pytest

from mcp_common import (
    arg_fail, bare_code, coerce_args, date_arg, date_fail, eastmoney_secid,
    fail, input_schema, is_empty_result, make_coercer, normalize_cn_symbol,
    normalize_date, ok, setup_logging, symbol_fail, tencent_symbol, tool_failed,
)


# ══════════════ 1. 错误出口 ══════════════

def test_fail_always_valid_json_even_with_quotes():
    """异常信息带引号时旧实现产出非法 JSON —— 这是线上真实故障。"""
    out = fail("KeyError:'data'", code="upstream", hint='重试或检查 "data" 字段')
    parsed = json.loads(out)          # 必须能解析
    assert parsed["error"] == "KeyError:'data'"
    assert parsed["code"] == "upstream"
    assert parsed["hint"].startswith("重试")


def test_fail_omits_empty_hint():
    assert "hint" not in json.loads(fail("boom"))


def test_ok_keeps_chinese_unescaped():
    assert "贵州茅台" in ok({"name": "贵州茅台"})


# ══════════════ 2. 空结果 / 失败判定 ══════════════

@pytest.mark.parametrize("value", [
    None, "", "   ", "[]", "{}", "null", "None",
    [], {}, 0, False, (), set(),
    [[], {}], {"a": [], "b": {}},
    json.dumps([]),
])
def test_is_empty_result_true(value):
    assert is_empty_result(value) is True


@pytest.mark.parametrize("value", [
    "NO_DATA", "NO_MATCH", [{"a": 1}], {"a": 1}, 1, True, "600519",
])
def test_is_empty_result_false(value):
    assert is_empty_result(value) is False


def test_is_empty_result_meta_only_is_not_empty():
    """只有元数据键不足以判定为空（否则会把"当天无涨停"错误缓存成空）。"""
    assert is_empty_result({"date": "20260626"}) is False


def test_tool_failed_detects_our_own_error_shape():
    assert tool_failed(fail("upstream down")) is True


def test_tool_failed_detects_broken_json():
    assert tool_failed('{"error": "unterminated') is True


def test_tool_failed_detects_error_prefix():
    assert tool_failed("ERROR: upstream 500") is True


@pytest.mark.parametrize("result", ["NO_DATA", "NO_MATCH", "No stocks found", "[]", '{"a":1}', "ok"])
def test_tool_failed_ignores_legit_empty(result):
    assert tool_failed(result) is False


# ══════════════ 3. schema（falsy 陷阱回归测试） ══════════════

def test_input_schema_explicit_empty_required_stays_empty():
    """回归：``required or list(properties.keys())`` 会把显式 [] 短路成全必填。"""
    s = input_schema({"a": {"type": "string"}, "b": {"type": "integer"}}, required=[])
    assert s["required"] == []


def test_input_schema_default_is_empty():
    assert input_schema({"a": {}})["required"] == []


def test_input_schema_keeps_declared_required():
    assert input_schema({"a": {}, "b": {}}, required=["a"])["required"] == ["a"]


# ══════════════ 4. 参数类型强制 ══════════════

def _handlers():
    def tool_int(days: int = 5): return days
    def tool_float(x: float = 1.0): return x
    def tool_bool(flag: bool = False): return flag
    def tool_str(symbol: str = "x"): return symbol
    def tool_list(codes: list = None): return codes
    def tool_dict(opts: dict = None): return opts
    def tool_req(symbol: str): return symbol
    def tool_var(**kw): return kw
    return {"t_int": tool_int, "t_float": tool_float, "t_bool": tool_bool,
            "t_str": tool_str, "t_list": tool_list, "t_dict": tool_dict,
            "t_req": tool_req, "t_var": tool_var}


H = _handlers()


def test_coerce_int_from_string():
    assert coerce_args(H, "t_int", {"days": "60"}) == ({"days": 60}, None)


def test_coerce_int_from_float_string():
    assert coerce_args(H, "t_int", {"days": "60.0"}) == ({"days": 60}, None)


def test_coerce_int_rejects_fractional():
    out, err = coerce_args(H, "t_int", {"days": "6.5"})
    assert out is None and json.loads(err)["code"] == "invalid_argument"


def test_coerce_float_from_string():
    assert coerce_args(H, "t_float", {"x": "2.5"}) == ({"x": 2.5}, None)


def test_coerce_bool_from_strings():
    assert coerce_args(H, "t_bool", {"flag": "true"}) == ({"flag": True}, None)
    assert coerce_args(H, "t_bool", {"flag": "no"}) == ({"flag": False}, None)


def test_coerce_str_from_float_avoids_dot_zero():
    """JSON 里 600519.0 → "600519"，不是 "600519.0"。"""
    assert coerce_args(H, "t_str", {"symbol": 600519.0}) == ({"symbol": "600519"}, None)


def test_coerce_list_from_json_and_csv():
    assert coerce_args(H, "t_list", {"codes": '["600519"]'}) == ({"codes": ["600519"]}, None)
    assert coerce_args(H, "t_list", {"codes": "600519,000001"}) == ({"codes": ["600519", "000001"]}, None)


def test_coerce_dict_from_json():
    assert coerce_args(H, "t_dict", {"opts": '{"a":1}'}) == ({"opts": {"a": 1}}, None)


def test_coerce_null_with_default_drops_arg():
    """显式传 null 且有默认值 → 当没传（旧实现会崩在 min(max(days,1),60)）。"""
    assert coerce_args(H, "t_int", {"days": None}) == ({}, None)


def test_coerce_null_without_default_errors():
    out, err = coerce_args(H, "t_req", {"symbol": None})
    assert out is None and json.loads(err)["code"] == "invalid_argument"


def test_coerce_unknown_arg_errors_with_supported_list():
    out, err = coerce_args(H, "t_int", {"nope": 1})
    body = json.loads(err)
    assert out is None and body["code"] == "unknown_argument" and "days" in body["hint"]


def test_coerce_allows_kwargs_handler():
    assert coerce_args(H, "t_var", {"anything": 1})[1] is None


def test_coerce_unknown_tool_passes_through():
    """未注册的工具交给上层报"工具不存在"，这里不越权。"""
    assert coerce_args(H, "nope", {"a": 1}) == ({"a": 1}, None)


def test_coerce_non_dict_passes_through():
    assert coerce_args(H, "t_int", None) == (None, None)


def test_make_coercer_binds_registry():
    f = make_coercer(H)
    assert f("t_int", {"days": "3"}) == ({"days": 3}, None)


def test_arg_fail_names_parameter_and_type():
    body = json.loads(arg_fail("days", "integer", "abc"))
    assert "days" in body["error"] and "integer" in body["error"]
    assert "days=5" in body["hint"]


# ══════════════ 5. A 股代码归一化 ══════════════

@pytest.mark.parametrize("raw,expected", [
    ("600519", ("sh", "600519")),
    ("sh600519", ("sh", "600519")),
    ("SH600519", ("sh", "600519")),
    ("600519.SH", ("sh", "600519")),
    ("600519.SS", ("sh", "600519")),
    ("600519-sh", ("sh", "600519")),
    ("000001", ("sz", "000001")),
    ("sz000001", ("sz", "000001")),
    ("300750", ("sz", "300750")),
    ("688017", ("sh", "688017")),
    ("510300", ("sh", "510300")),
    ("159915", ("sz", "159915")),
    ("430047", ("bj", "430047")),
    ("830799", ("bj", "830799")),
    ("920819", ("bj", "920819")),
    (" 600519 ", ("sh", "600519")),
])
def test_normalize_cn_symbol_ok(raw, expected):
    assert normalize_cn_symbol(raw) == expected


def test_normalize_cn_symbol_explicit_tag_beats_prefix():
    """显式标记优先：sh000001 是上证指数，sz000001 是平安银行。"""
    assert normalize_cn_symbol("sh000001") == ("sh", "000001")
    assert normalize_cn_symbol("sz000001") == ("sz", "000001")


def test_normalize_cn_symbol_north_8xx_is_bj_not_sz():
    """回归：旧实现有 3 套市场规则，北交所 8xx 被判成深市。"""
    assert normalize_cn_symbol("830799") == ("bj", "830799")


@pytest.mark.parametrize("raw", [None, "", "ZZZZ999", "60051", "6005199",
                                 "12345678", "600519.XX", 12345])
def test_normalize_cn_symbol_invalid(raw):
    assert normalize_cn_symbol(raw) is None


def test_symbol_fail_is_actionable():
    body = json.loads(symbol_fail("ZZZZ999"))
    assert body["code"] == "invalid_symbol" and body["hint"]


def test_symbol_derivations():
    assert bare_code("600519.SH") == "600519"
    assert tencent_symbol("600519") == "sh600519"
    assert eastmoney_secid("600519") == "1.600519"
    assert eastmoney_secid("000001") == "0.000001"
    assert eastmoney_secid("830799") == "0.830799"
    assert bare_code("nope") is None


# ══════════════ 6. 日期归一化 ══════════════

@pytest.mark.parametrize("raw,expected", [
    ("2026-06-26", "2026-06-26"),
    ("20260626", "2026-06-26"),
    ("2026/06/26", "2026-06-26"),
    ("2026.06.26", "2026-06-26"),
])
def test_normalize_date_ok(raw, expected):
    assert normalize_date(raw) == expected


def test_normalize_date_compact_format():
    assert normalize_date("2026-06-26", fmt="%Y%m%d") == "20260626"


def test_normalize_date_none_and_empty():
    assert normalize_date(None) is None
    assert normalize_date("  ") is None


def test_normalize_date_invalid_raises():
    with pytest.raises(ValueError):
        normalize_date("2026-13-45")


def test_date_arg_default_today():
    out, err = date_arg("", default_today=True)
    assert err is None and len(out) == 10


def test_date_arg_invalid_returns_fail_json():
    out, err = date_arg("bad-date")
    assert out is None and json.loads(err)["code"] == "invalid_date"


def test_date_arg_no_default_returns_none():
    assert date_arg(None) == (None, None)


# ══════════════ 7. 日志 ══════════════

def test_setup_logging_is_idempotent():
    a = setup_logging("svc-a")
    n_handlers = len(logging.getLogger().handlers)
    b = setup_logging("svc-b")
    assert a.name == "svc-a" and b.name == "svc-b"
    assert len(logging.getLogger().handlers) == n_handlers
    assert n_handlers >= 1
