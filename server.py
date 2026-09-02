# -*- coding: utf-8 -*-
"""AI 财报工作台 - 本地数据服务

桥接前端 HTML 与同花顺 iFinD MCP：
  1. 提供静态站点（html/ 目录），可用浏览器直接访问
  2. 提供实时行情 / 个股资料接口，前端通过 fetch 调用

路由：
  GET /api/health                健康检查
  GET /api/quote?symbols=600519  实时行情快照（支持代码/六位/简称，逗号分隔，最多 10 个）
  GET /api/info?symbol=600519    个股基本资料（行业/上市日期/总股本等）

用法：
  python server.py [端口]        # 默认 8765，也可用环境变量 PORT
"""

import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ifind_mcp as mcp

PORT = int(os.environ.get("PORT", "8765"))
HTML_DIR = Path(__file__).resolve().parent.parent / "html"

# iFinD 免费用户并发有限，串行化 MCP 调用以避免触发并发限制
_mcp_lock = threading.Lock()

# 实时行情常用指标（英文 key 由 indicatorMap 动态提供）
REALTIME_INDICATORS = (
    "最新价,涨跌幅,成交量,成交额,换手率,量比,总市值,流通市值,"
    "市净率,市盈率TTM,开盘价,最高价,最低价,均价"
)


def _unwrap_text(text):
    """iFinD 内容通常嵌套两层 JSON，解析到最里层 dict。"""
    for _ in range(3):
        if isinstance(text, str):
            try:
                text = json.loads(text)
            except Exception:
                return text
        else:
            break
    return text


def parse_quote_table(res):
    """把 stock_highfreq_quotes 的返回解析成按 symbol 组织的结果列表。"""
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "行情接口错误")}
    content = res.get("data", {}).get("result", {}).get("content") or []
    if not content:
        return {"ok": True, "data": [], "raw": res}
    obj = _unwrap_text(content[0].get("text", ""))
    if isinstance(obj, dict) and isinstance(obj.get("data"), str):
        obj = _unwrap_text(obj["data"])
    if not isinstance(obj, dict):
        return {"ok": True, "data": [], "raw": obj}

    tables = obj.get("tables") or []
    if not tables:
        return {"ok": True, "data": [], "raw": obj}
    cols = tables[0]
    rows = tables[1:]
    indmap = obj.get("indicatorMap") or {}

    entries = []
    for row in rows:
        if not row:
            continue
        # row[0] 证券代码，row[1] 证券简称，其余按 indicatorMap 映射为英文 key
        entry = {"symbol": row[0], "name": row[1] if len(row) > 1 else ""}
        for i, val in enumerate(row):
            if i < 2:
                continue
            col_name = cols[i] if i < len(cols) else str(i)
            key = indmap.get(col_name, col_name)
            entry[key] = val
        entries.append(entry)
    return {"ok": True, "data": entries}


def fetch_quotes(symbols):
    """拉取实时行情。symbols 为逗号分隔字符串。"""
    with _mcp_lock:
        res = mcp.call(
            "stock", "stock_highfreq_quotes",
            {"symbols": symbols, "indicators": REALTIME_INDICATORS, "data_mode": "real_time"},
        )
    parsed = parse_quote_table(res)
    if parsed.get("ok"):
        for item in parsed.get("data", []):
            item["verify"] = cross_verify(item)
    return parsed


def _cross_symbol(symbol):
    """600519.SH → sh600519，000858.SZ → sz000858。"""
    parts = symbol.split(".")
    code = parts[0]
    suffix = parts[1] if len(parts) > 1 else ""
    return ("sh" if suffix == "SH" else "sz") + code


def _fetch_cross_quote(symbol):
    """从腾讯财经公开行情接口取关键字段作为第二数据源，失败返回 None。"""
    t = _cross_symbol(symbol)
    url = f"http://qt.gtimg.cn/q={t}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("gbk", "replace")
    except Exception:
        return None
    m = re.search(r'="(.*)"', text)
    if not m:
        return None
    parts = m.group(1).split("~")
    if len(parts) < 47:
        return None

    def f(i):
        try:
            return float(parts[i])
        except (IndexError, ValueError):
            return None

    return {
        "latest": f(3),                       # 现价
        "changeRatio": f(32),                 # 涨跌幅 %
        "volume": f(6),                       # 成交量（手）
        "amount": (f(37) * 1e4) if f(37) is not None else None,   # 成交额（万 → 元）
        "turnoverRatio": f(38),               # 换手率 %
        "pe": f(39),                          # 市盈率
        "marketCap": (f(45) * 1e8) if f(45) is not None else None,  # 总市值（亿 → 元）
        "pb": f(46),                          # 市净率
    }


def _num_same(a, b, rel=0.005, abs_tol=0.01):
    """两数值偏差在容差内视为一致；任一为空返回 None（无法比较）。"""
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return None
    if abs(a - b) <= max(abs_tol, abs(a) * rel):
        return True
    return False


def cross_verify(if_quote):
    """用腾讯财经核对 iFinD 行情，返回一致性结论。"""
    em = _fetch_cross_quote(if_quote.get("symbol", ""))
    if em is None:
        return {"source": "腾讯财经", "ok": None, "error": "交叉源暂不可用", "at": time.strftime("%H:%M:%S"), "fields": {}}

    def cmp(key, rel=0.005, abs_tol=0.01, ifind_key=None):
        ik = ifind_key or key
        return {"iFind": if_quote.get(ik), "em": em.get(key),
                "same": _num_same(if_quote.get(ik), em.get(key), rel, abs_tol)}

    fields = {
        "latest": cmp("latest"),
        "changeRatio": cmp("changeRatio", rel=1, abs_tol=0.5),
        "marketCap": cmp("marketCap", rel=0.01, abs_tol=1e6, ifind_key="totalCapital"),
        "volume": cmp("volume", rel=0.02, abs_tol=20),
        "amount": cmp("amount", rel=0.02, abs_tol=1e5),
    }
    diffs = [k for k, f in fields.items() if f["same"] is False]
    return {
        "source": "腾讯财经",
        "ok": len(diffs) == 0,
        "at": time.strftime("%H:%M:%S"),
        "fields": fields,
        "pe": {"iFind": if_quote.get("pe_ttm"), "em": em.get("pe"), "note": "iFinD 为 TTM 口径，腾讯为最新口径，供参考"},
        "pb": {"iFind": if_quote.get("pb"), "em": em.get("pb")},
        "diffs": diffs,
    }


def fetch_info(symbol):
    """拉取个股基本资料（行业、上市日期、总股本等）。"""
    query = f"{symbol} 的所属行业、上市日期、总股本"
    with _mcp_lock:
        res = mcp.call("stock", "get_stock_info", {"query": query})
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "资料接口错误")}
    content = res.get("data", {}).get("result", {}).get("content") or []
    raw = content[0].get("text", "") if content else ""
    obj = _unwrap_text(raw)
    return {"ok": True, "data": obj}


def _to_yi(value):
    """把 iFinD 返回的金额字符串统一转成亿元；无法解析返回 None。"""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if not s or s in ("--", "null", "None", ""):
        return None
    if s.endswith("亿"):
        try:
            return float(s[:-1])
        except ValueError:
            return None
    if s.endswith("万"):
        try:
            return float(s[:-1]) / 1e4
        except ValueError:
            return None
    try:
        return float(s) / 1e8  # 默认单位为元
    except ValueError:
        return None


def _to_ratio(value):
    """把同比/比率字符串转成浮点数（百分比数值）。"""
    if value is None:
        return None
    s = str(value).replace(",", "").strip()
    if not s or s in ("--", "null", "None", ""):
        return None
    try:
        return float(s.rstrip("%"))
    except ValueError:
        return None


def _extract_response_table(res):
    """从 get_stock_financials / get_stock_info 等返回中提取 markdown 二维表。"""
    content = res.get("data", {}).get("result", {}).get("content") or []
    if not content:
        return None, []
    obj = _unwrap_text(content[0].get("text", ""))
    if isinstance(obj, dict) and isinstance(obj.get("data"), str):
        obj = _unwrap_text(obj["data"])
    answer = obj.get("answer", "") if isinstance(obj, dict) else ""
    if not answer:
        return None, []
    header = None
    rows = []
    for line in answer.split("\n"):
        line = line.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not header:
            header = cells
            continue
        if all(re.fullmatch(r"-+", c) for c in cells):
            continue  # markdown 分隔行
        rows.append(cells)
    return header, rows


def _norm_key(key):
    """去掉 iFinD 列名里的单位括号，如“销售费用（单位：元）” → “销售费用”。"""
    key = re.sub(r"（单位：[^）]*）", "", key)
    key = re.sub(r"[（(]单位[:：][^)）]*[)）]", "", key)
    return key.strip()


def _normalize_financial_rows(header, row):
    """按去单位后的列名组装字典，同名冲突时优先保留非空值。"""
    result = {}
    for h, v in zip(header, row):
        k = _norm_key(h)
        if k not in result or result[k] in (None, ""):
            result[k] = v
    return result


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def fetch_stock(symbol):
    """聚合一只股票的实时行情 + 财报三表 + 估值参数，输出与前端工作台一致的对象。"""
    qres = fetch_quotes(symbol)
    quotes = qres.get("data") or []
    if not quotes:
        return {"ok": False, "error": "未找到该股票，请确认代码或简称（如 600519、贵州茅台）"}
    q = quotes[0]
    code = (q.get("symbol") or "").split(".")[0]
    name = q.get("name") or symbol

    # 财报：取最近完整年度（2024 年报），避免"最新年报"对部分公司返回残缺单季口径
    query = (
        f"{name}2024年报的营业总收入、营业成本、营业税金及附加、销售费用、管理费用、研发费用、"
        "财务费用、归属于母公司所有者的净利润、经营活动产生的现金流量净额、投资活动产生的现金流量净额、"
        "筹资活动产生的现金流量净额、资产总计、负债合计、所有者权益合计、货币资金、存货、"
        "营业总收入(同比增长率)、归属母公司股东的净利润(同比增长率)"
    )
    with _mcp_lock:
        f_res = mcp.call("stock", "get_stock_financials", {"query": query})
    if not f_res.get("ok"):
        return {"ok": False, "error": "财报数据获取失败"}
    header, rows = _extract_response_table(f_res)
    if not header or not rows:
        return {"ok": False, "error": "未解析到该股票的财报数据"}
    d = _normalize_financial_rows(header, rows[0])

    def fv(*names):
        for n in names:
            if d.get(n) not in (None, ""):
                return d.get(n)
        return None

    revenue = _to_yi(fv("营业总收入", "营业收入"))
    cost = _to_yi(fv("营业成本"))
    tax = _to_yi(fv("税金及附加"))
    selling = _to_yi(fv("销售费用"))
    admin = _to_yi(fv("管理费用"))
    rd = _to_yi(fv("研发费用"))
    finance_val = _to_yi(fv("财务费用"))
    net_profit = _to_yi(fv("归属于母公司所有者的净利润", "净利润"))
    np_growth = _to_ratio(fv("归属母公司股东的净利润(同比增长率)", "净利润(同比增长率)"))
    rev_growth = _to_ratio(fv("营业总收入(同比增长率)", "营业收入(同比增长率)"))
    op_cf = _to_yi(fv("经营活动产生的现金流量净额")) or 0
    inv_cf = _to_yi(fv("投资活动产生的现金流量净额")) or 0
    fin_cf = _to_yi(fv("筹资活动产生的现金流量净额")) or 0
    assets = _to_yi(fv("资产总计"))
    liab = _to_yi(fv("负债合计"))
    equity = _to_yi(fv("所有者权益合计"))
    cash = _to_yi(fv("货币资金"))
    inventory = _to_yi(fv("存货"))
    period = str(fv("日期") or "")
    year = period[:4] if period[:4] else "最新"

    gross_margin = ((revenue - cost) / revenue * 100) if (revenue and cost is not None) else None
    net_cash_ratio = (op_cf / net_profit) if net_profit else None
    debt_ratio = (liab / assets * 100) if assets else None
    core_profit = ((revenue or 0) - (cost or 0) - (tax or 0)
                   - ((selling or 0) + (admin or 0) + (rd or 0) + (finance_val or 0)))
    core_margin = (core_profit / revenue * 100) if revenue else None

    # 简化财务评分（0-100）
    profit_score = _clamp(gross_margin / 60 * 100, 0, 100) if gross_margin is not None else 0
    growth_score = _clamp(50 + (rev_growth or 0) * 2, 0, 100)
    cash_score = _clamp((net_cash_ratio or 0) * 80, 0, 100)
    debt_score = _clamp(100 - (debt_ratio or 0), 0, 100)
    roe = (net_profit / equity * 100) if (net_profit and equity) else None
    roe_score = _clamp((roe or 0) / 25 * 100, 0, 100)
    score = round(0.25 * profit_score + 0.2 * growth_score + 0.25 * cash_score + 0.2 * debt_score + 0.1 * roe_score)
    score = int(_clamp(score, 0, 100))

    # 估值参数（简化：增速用净利同比，PE中位数用实时PE，净现比用最新）
    A = np_growth if (np_growth is not None and np_growth > 0) else 5
    C = _to_ratio(q.get("pe_ttm")) or 20
    R = net_cash_ratio if net_cash_ratio else 1.0

    balance = [
        ["货币资金", cash, None, f"占总资产 {(cash / assets * 100) if (cash and assets) else None:.1f}%" if (cash and assets) else "单位：亿元"],
        ["交易性金融资产", None, None, "—"],
        ["应收账款", None, None, "—"],
        ["存货", inventory, None, "存货"],
        ["流动资产合计", None, None, "—"],
        ["固定资产", None, None, "—"],
        ["在建工程", None, None, "—"],
        ["无形资产", None, None, "—"],
        ["非流动资产合计", None, None, "—"],
        ["总资产", assets, None, "资产规模"],
        ["短期借款", None, None, "—"],
        ["应付账款", None, None, "—"],
        ["合同负债", None, None, "—"],
        ["流动负债合计", None, None, "—"],
        ["长期借款", None, None, "—"],
        ["负债合计", liab, None, "负债结构"],
        ["股东权益", equity, None, "净资产"],
        ["资产负债率", debt_ratio, None, "负债率（%）"],
    ]

    obj = {
        "name": name,
        "code": code,
        "symbol": q.get("symbol"),
        "industry": "——",
        "price": _to_ratio(q.get("latest")),
        "change": _to_ratio(q.get("changeRatio")),
        "marketCap": _to_yi(q.get("totalCapital")),
        "pe": _to_ratio(q.get("pe_ttm")),
        "pb": _to_ratio(q.get("pb")),
        "dividendYield": None,
        "revenue": revenue,
        "revenueGrowth": rev_growth,
        "cost": cost,
        "tax": tax,
        "selling": selling,
        "admin": admin,
        "rd": rd,
        "finance": finance_val,
        "netProfit": net_profit,
        "netProfitGrowth": np_growth,
        "eps": None,
        "grossMargin": gross_margin,
        "balance": balance,
        "cashflow": {
            "op": op_cf, "opYoy": None, "salesCash": None, "inv": inv_cf, "invYoy": None,
            "capex": None, "fin": fin_cf, "finYoy": None, "totalCF": op_cf + inv_cf + fin_cf,
            "netCashRatio": net_cash_ratio, "fcf": op_cf,
        },
        "trends": {
            "years": [year],
            "revenue": [revenue],
            "coreProfit": [core_profit],
            "coreMargin": [core_margin],
            "netProfit": [net_profit],
        },
        "valuation": {"A": A, "C": C, "R": R, "Y1": score, "Y5": score},
        "score": score,
        "scoreParts": {
            "盈利能力": int(round(profit_score)),
            "成长性": int(round(growth_score)),
            "现金流": int(round(cash_score)),
            "财务安全": int(round(debt_score)),
        },
        "rank": "——",
        "quote": q,
        "verify": q.get("verify"),
        "period": period,
    }
    return {"ok": True, "data": obj, "source": "iFinD"}


class Handler(BaseHTTPRequestHandler):
    server_version = "IWorkbench/1.0"

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send_json({}, 204)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/health":
            return self._send_json({"ok": True, "service": "iworkbench", "port": PORT})

        if parsed.path == "/api/quote":
            symbols = (qs.get("symbols") or [""])[0].strip()
            if not symbols:
                return self._send_json({"ok": False, "error": "缺少 symbols 参数"}, 400)
            try:
                return self._send_json(fetch_quotes(symbols))
            except Exception as err:
                return self._send_json({"ok": False, "error": str(err)}, 500)

        if parsed.path == "/api/info":
            symbol = (qs.get("symbol") or [""])[0].strip()
            if not symbol:
                return self._send_json({"ok": False, "error": "缺少 symbol 参数"}, 400)
            try:
                return self._send_json(fetch_info(symbol))
            except Exception as err:
                return self._send_json({"ok": False, "error": str(err)}, 500)

        if parsed.path == "/api/stock":
            symbol = (qs.get("symbol") or [""])[0].strip()
            if not symbol:
                return self._send_json({"ok": False, "error": "缺少 symbol 参数"}, 400)
            try:
                return self._send_json(fetch_stock(symbol))
            except Exception as err:
                return self._send_json({"ok": False, "error": str(err)}, 500)

        return self._serve_static(parsed.path)

    def _serve_static(self, url_path):
        if url_path in ("/", ""):
            rel = Path("index.html")
        else:
            rel = Path(url_path.lstrip("/"))
        # 防路径穿越
        target = (HTML_DIR / rel).resolve()
        try:
            target.relative_to(HTML_DIR.resolve())
        except ValueError:
            return self._send_json({"ok": False, "error": "非法路径"}, 400)

        if not target.is_file():
            return self._send_json({"ok": False, "error": "文件不存在"}, 404)

        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(target.suffix.lower(), "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        # 精简日志，避免刷屏；可在需要时打印
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AI 财报工作台服务已启动: http://127.0.0.1:{port}")
    print(f"静态目录: {HTML_DIR}")
    print("实时行情: /api/quote?symbols=600519")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
