#!/usr/bin/env python3
"""
finra606_analyze.py
FINRA Rule 606 NMS 데이터 기반 리테일 브로커 마켓 센싱 툴

Usage:
    python3 finra606_analyze.py --year 2026 --qtr Q1
    python3 finra606_analyze.py --year 2026 --qtr Q1 --top 100
    python3 finra606_analyze.py --auto          # 현재 날짜 기반 분기 자동 감지

Output (00-Claude/scripts/output/):
    01_broker_overview.csv      브로커 종합 프로필 + 분류 태그
    02_internalizer_share.csv   인터널라이저 시장 점유율
    03_options_exchange.csv     옵션 거래소 order flow 분포
    04_dependency_matrix.csv    브로커-인터널라이저 의존도 매트릭스
    05_monthly_trend.csv        월별 PFOF 트렌드 (Jan/Feb/Mar)

주의:
    PFOF = 0 이 zero volume 을 의미하지 않음 (Fidelity 등 no-PFOF 브로커 존재)
    ABR 브로커 = clearing firm 대리 제출 (View4 매트릭스 제외)
"""

import io
import re
import csv
import sys
import zipfile
import argparse
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests 패키지 필요: pip install requests")

# ─── 상수 ──────────────────────────────────────────────────────────────────

BULK_URL = "https://cdn.finra.org/606reportBulk/{year}/606_NMS_{year}_{qtr}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (research/finra606-analyzer)"}

PFOF_FIELDS = [
    "netPmtPaidRecvMarketOrdersUsd",
    "netPmtPaidRecvMarketableLimitOrdersUsd",
]

# 거래소 MIC 집합 (나머지는 인터널라이저로 분류)
EXCHANGE_MICS = {
    # 주식 거래소
    "XNYS", "XNAS", "ARCX", "BATS", "IEXG", "EDGX", "EDGA", "XCIS",
    "XCHI", "XLTSE", "MEMX", "OOTC",
    # 옵션 거래소
    "XCBO", "XNDQ", "XMIO", "SPHR", "XBOX", "XISX", "XBXO", "MCRY",
    "EDGO", "ARCO", "GMNI", "BATO", "MPRL",
}

OUTPUT_DIR = Path(__file__).parent / "output"

# ─── 다운로드 ───────────────────────────────────────────────────────────────

def download_bulk_zip(year: str, qtr: str) -> zipfile.ZipFile:
    url = BULK_URL.format(year=year, qtr=qtr)
    print(f"Downloading {url} ...")
    r = requests.get(url, headers=HEADERS, timeout=120)
    if r.status_code == 404:
        sys.exit(f"[ERROR] {year} {qtr} 벌크 ZIP 미공개 (아직 제출 기간 아님)")
    if r.status_code != 200:
        sys.exit(f"[ERROR] HTTP {r.status_code}: {url}")
    print(f"  → {len(r.content) / 1024:.0f} KB 다운로드 완료")
    return zipfile.ZipFile(io.BytesIO(r.content))


# ─── 파일 선택 & ABR ────────────────────────────────────────────────────────

def _extract_crd(filename: str) -> str:
    m = re.match(r"^(\d+)_606_NMS", filename)
    return m.group(1) if m else ""


def _extract_version(filename: str) -> int:
    m = re.search(r"_V(\d+)\.xml$", filename, re.IGNORECASE)
    return int(m.group(1)) if m else 1


def _is_self_filed(filename: str) -> bool:
    base = re.sub(r"_V\d+\.xml$", "", filename, flags=re.IGNORECASE)
    parts = base.split("_")
    # 패턴: CRD_606_NMS_YEAR_QTR (5개 토큰) 이면 self-filed
    return len(parts) == 5


def select_best_files(zf: zipfile.ZipFile) -> dict:
    """CRD → 최적 XML 파일명 매핑. SELF-filed 우선, 동률 시 최고 버전."""
    xml_files = [n for n in zf.namelist() if n.lower().endswith(".xml")]
    by_crd: dict[str, str] = {}
    for fname in xml_files:
        crd = _extract_crd(fname)
        if not crd:
            continue
        prev = by_crd.get(crd)
        if prev is None:
            by_crd[crd] = fname
            continue
        self_new = _is_self_filed(fname)
        self_prev = _is_self_filed(prev)
        if self_new and not self_prev:
            by_crd[crd] = fname
        elif self_new == self_prev and _extract_version(fname) > _extract_version(prev):
            by_crd[crd] = fname
    print(f"  → XML 파일 {len(xml_files)}개, 중복 제거 후 {len(by_crd)}개 CRD")
    return by_crd


def parse_abr_set(zf: zipfile.ZipFile) -> set:
    """ABR.txt 파싱 → introducing broker CRD 집합 반환."""
    abr_files = [n for n in zf.namelist() if "ABR" in n.upper() and n.endswith(".txt")]
    abr_crds = set()
    for fname in abr_files:
        try:
            text = zf.read(fname).decode("utf-8", errors="ignore")
            for line in text.splitlines():
                m = re.search(r"\b(\d{5,})\b", line)
                if m:
                    abr_crds.add(m.group(1))
        except Exception:
            pass
    print(f"  → ABR (대리 제출) 브로커: {len(abr_crds)}개")
    return abr_crds


# ─── XML 파싱 ───────────────────────────────────────────────────────────────

def _safe_float(val) -> float:
    try:
        return float(val) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


def _parse_venues(section) -> list:
    if section is None:
        return []
    venues_el = section.find("rVenues") or section
    result = []
    for v in venues_el.findall("rVenue"):
        pfof = sum(_safe_float(v.findtext(f)) for f in PFOF_FIELDS)
        result.append({
            "name": (v.findtext("name") or "").strip(),
            "mic":  (v.findtext("mic")  or "").strip().upper(),
            "orderPct": _safe_float(v.findtext("orderPct")),
            "pfof_usd": pfof,
        })
    return result


def _has_no_pfof_disclosure(root) -> bool:
    for ma in root.iter("materialAspects"):
        text = (ma.text or "").lower()
        if "does not receive payment" in text or "does not accept" in text:
            return True
    return False


def parse_xml(xml_bytes: bytes, filename: str):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  [SKIP] XML 파싱 실패 {filename}: {e}", file=sys.stderr)
        return None

    crd = _extract_crd(filename)
    bd_name = (root.findtext("bd") or "").strip()

    monthly = []
    for m_el in root.findall(".//rMonthly"):
        mon = int(m_el.findtext("mon") or 0)
        equity_venues = (
            _parse_venues(m_el.find("rSP500")) +
            _parse_venues(m_el.find("rOtherStocks"))
        )
        options_venues = _parse_venues(m_el.find("rOptions"))

        # 주식 주문 유형 비율 (SP500 + OtherStocks 평균)
        ndo_market = ndo_ml = ndo_nml = 0.0
        count = 0
        for tag in ("rSP500", "rOtherStocks"):
            sec = m_el.find(tag)
            if sec is None:
                continue
            ndo_market += _safe_float(sec.findtext("ndoMarketPct"))
            ndo_ml     += _safe_float(sec.findtext("ndoMarketableLimitPct"))
            ndo_nml    += _safe_float(sec.findtext("ndoNonmarketableLimitPct"))
            count += 1
        if count:
            ndo_market /= count
            ndo_ml     /= count
            ndo_nml    /= count

        monthly.append({
            "month": mon,
            "equity_venues":  equity_venues,
            "options_venues": options_venues,
            "ndo_market_pct": ndo_market,
            "ndo_ml_pct":     ndo_ml,
            "ndo_nml_pct":    ndo_nml,
        })

    return {
        "broker_name": bd_name,
        "crd": crd,
        "filename": filename,
        "no_pfof_disclosure": _has_no_pfof_disclosure(root),
        "monthly": monthly,
    }


# ─── 집계 헬퍼 ─────────────────────────────────────────────────────────────

def _sum_pfof(venues: list, asset_type: str = "both") -> float:
    return sum(v["pfof_usd"] for v in venues)


def _broker_totals(rec: dict) -> dict:
    """브로커 레코드 → 분기 합산 수치."""
    eq = sum(_sum_pfof(m["equity_venues"])  for m in rec["monthly"])
    op = sum(_sum_pfof(m["options_venues"]) for m in rec["monthly"])
    total = eq + op

    # 주문 유형 평균 (월별 평균)
    months = rec["monthly"]
    n = len(months) or 1
    avg_market = sum(m["ndo_market_pct"] for m in months) / n
    avg_ml     = sum(m["ndo_ml_pct"]     for m in months) / n
    avg_nml    = sum(m["ndo_nml_pct"]    for m in months) / n

    # 전체 equity venue 집계 (3개월 합산)
    all_equity_venues: dict[str, dict] = {}
    all_options_venues: dict[str, dict] = {}
    for m in months:
        for v in m["equity_venues"]:
            key = v["mic"] or v["name"]
            if key not in all_equity_venues:
                all_equity_venues[key] = {"name": v["name"], "mic": v["mic"],
                                           "orderPct_sum": 0.0, "pfof_sum": 0.0, "cnt": 0}
            all_equity_venues[key]["orderPct_sum"] += v["orderPct"]
            all_equity_venues[key]["pfof_sum"]     += v["pfof_usd"]
            all_equity_venues[key]["cnt"]          += 1
        for v in m["options_venues"]:
            key = v["mic"] or v["name"]
            if key not in all_options_venues:
                all_options_venues[key] = {"name": v["name"], "mic": v["mic"],
                                            "orderPct_sum": 0.0, "pfof_sum": 0.0, "cnt": 0}
            all_options_venues[key]["orderPct_sum"] += v["orderPct"]
            all_options_venues[key]["pfof_sum"]     += v["pfof_usd"]
            all_options_venues[key]["cnt"]          += 1

    # 인터널라이저 비율 (비거래소 venue의 orderPct 합산 평균)
    intern_pct = 0.0
    top_venue_name, top_venue_pct = "", 0.0
    all_venues_combined = list(all_equity_venues.values()) + list(all_options_venues.values())
    for vd in all_venues_combined:
        avg_pct = vd["orderPct_sum"] / (vd["cnt"] or 1)
        if vd["mic"] not in EXCHANGE_MICS:
            intern_pct += avg_pct
        if avg_pct > top_venue_pct:
            top_venue_pct = avg_pct
            top_venue_name = vd["name"]

    return {
        "pfof_equity_usd":    round(eq,    2),
        "pfof_options_usd":   round(op,    2),
        "pfof_total_usd":     round(total, 2),
        "options_mix_pct":    round(op / total * 100, 1) if total else 0.0,
        "market_order_pct":   round(avg_market, 1),
        "limit_order_pct":    round(avg_ml + avg_nml, 1),
        "internalization_rate": round(min(intern_pct, 100.0), 1),
        "top_internalizer":   top_venue_name,
        "top_internalizer_pct": round(top_venue_pct, 1),
        "equity_venues":  all_equity_venues,
        "options_venues": all_options_venues,
    }


# ─── View 1: 브로커 종합 프로필 ─────────────────────────────────────────────

def write_broker_overview(records: list, abr_set: set, output_dir: Path):
    rows = []
    for rec in records:
        t = _broker_totals(rec)
        is_zero = t["pfof_total_usd"] == 0.0
        is_abr  = rec["crd"] in abr_set

        tags = []
        if t["options_mix_pct"] >= 40:           tags.append("options_heavy")
        if t["market_order_pct"] >= 60:          tags.append("passive_retail")
        if t["limit_order_pct"]  >= 50:          tags.append("active_trader")
        if t["top_internalizer_pct"] >= 80:      tags.append("single_venue")
        if is_zero:                              tags.append("no_pfof")
        if is_abr:                               tags.append("abr")

        note = ""
        if is_zero and rec["no_pfof_disclosure"]:
            note = "No PFOF per materialAspects — routes to exchanges"

        rows.append({
            "broker_name":          rec["broker_name"],
            "crd":                  rec["crd"],
            "pfof_equity_usd":      t["pfof_equity_usd"],
            "pfof_options_usd":     t["pfof_options_usd"],
            "pfof_total_usd":       t["pfof_total_usd"],
            "options_mix_pct":      t["options_mix_pct"],
            "market_order_pct":     t["market_order_pct"],
            "limit_order_pct":      t["limit_order_pct"],
            "internalization_rate": t["internalization_rate"],
            "top_internalizer":     t["top_internalizer"],
            "top_internalizer_pct": t["top_internalizer_pct"],
            "is_zero_pfof":         is_zero,
            "is_abr":               is_abr,
            "tags":                 "|".join(tags),
            "note":                 note,
            "xml_filename":         rec["filename"],
        })

    rows.sort(key=lambda r: r["pfof_total_usd"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    cols = ["rank", "broker_name", "crd", "pfof_equity_usd", "pfof_options_usd",
            "pfof_total_usd", "options_mix_pct", "market_order_pct", "limit_order_pct",
            "internalization_rate", "top_internalizer", "top_internalizer_pct",
            "is_zero_pfof", "is_abr", "tags", "note", "xml_filename"]
    _write_csv(output_dir / "01_broker_overview.csv", cols, rows)
    return rows


# ─── View 2: 인터널라이저 시장 점유율 ────────────────────────────────────────

def write_internalizer_share(records: list, output_dir: Path):
    venue_data: dict[str, dict] = {}

    for rec in records:
        t = _broker_totals(rec)
        single = t["top_internalizer_pct"] >= 80

        for vd in t["equity_venues"].values():
            if vd["mic"] in EXCHANGE_MICS:
                continue
            key = vd["mic"] or vd["name"]
            if key not in venue_data:
                venue_data[key] = {"name": vd["name"], "mic": vd["mic"],
                                    "pfof_equity": 0.0, "pfof_options": 0.0,
                                    "broker_set": set(), "exclusive_count": 0}
            venue_data[key]["pfof_equity"]  += vd["pfof_sum"]
            venue_data[key]["broker_set"].add(rec["crd"])
            if single and t["top_internalizer"] == vd["name"]:
                venue_data[key]["exclusive_count"] += 1

        for vd in t["options_venues"].values():
            if vd["mic"] in EXCHANGE_MICS:
                continue
            key = vd["mic"] or vd["name"]
            if key not in venue_data:
                venue_data[key] = {"name": vd["name"], "mic": vd["mic"],
                                    "pfof_equity": 0.0, "pfof_options": 0.0,
                                    "broker_set": set(), "exclusive_count": 0}
            venue_data[key]["pfof_options"] += vd["pfof_sum"]
            venue_data[key]["broker_set"].add(rec["crd"])

    total_pfof = sum(v["pfof_equity"] + v["pfof_options"] for v in venue_data.values())

    rows = []
    for vd in venue_data.values():
        total = vd["pfof_equity"] + vd["pfof_options"]
        rows.append({
            "venue_name":             vd["name"],
            "mic":                    vd["mic"],
            "equity_pfof_usd":        round(vd["pfof_equity"],  2),
            "options_pfof_usd":       round(vd["pfof_options"], 2),
            "total_pfof_received_usd": round(total, 2),
            "market_share_pct":       round(total / total_pfof * 100, 2) if total_pfof else 0.0,
            "broker_count":           len(vd["broker_set"]),
            "exclusive_broker_count": vd["exclusive_count"],
        })

    rows.sort(key=lambda r: r["total_pfof_received_usd"], reverse=True)
    cols = ["venue_name", "mic", "equity_pfof_usd", "options_pfof_usd",
            "total_pfof_received_usd", "market_share_pct",
            "broker_count", "exclusive_broker_count"]
    _write_csv(output_dir / "02_internalizer_share.csv", cols, rows)


# ─── View 3: 옵션 거래소 order flow 분포 ─────────────────────────────────────

def write_options_exchange(records: list, output_dir: Path):
    exchange_data: dict[str, dict] = {}

    for rec in records:
        t = _broker_totals(rec)
        for vd in t["options_venues"].values():
            if vd["mic"] not in EXCHANGE_MICS:
                continue
            key = vd["mic"]
            if key not in exchange_data:
                exchange_data[key] = {"name": vd["name"], "mic": vd["mic"],
                                       "orderPct_sum": 0.0, "pfof_sum": 0.0,
                                       "broker_set": set(), "weight_count": 0}
            exchange_data[key]["orderPct_sum"] += vd["orderPct_sum"] / (vd["cnt"] or 1)
            exchange_data[key]["pfof_sum"]     += vd["pfof_sum"]
            exchange_data[key]["broker_set"].add(rec["crd"])
            exchange_data[key]["weight_count"] += 1

    n_brokers = len(records) or 1
    rows = []
    for vd in exchange_data.values():
        rows.append({
            "exchange_name":         vd["name"],
            "mic":                   vd["mic"],
            "avg_order_flow_pct":    round(vd["orderPct_sum"] / n_brokers, 2),
            "pfof_paid_usd":         round(vd["pfof_sum"], 2),
            "broker_count":          len(vd["broker_set"]),
        })

    rows.sort(key=lambda r: r["avg_order_flow_pct"], reverse=True)
    cols = ["exchange_name", "mic", "avg_order_flow_pct", "pfof_paid_usd", "broker_count"]
    _write_csv(output_dir / "03_options_exchange.csv", cols, rows)


# ─── View 4: 브로커-인터널라이저 의존도 매트릭스 ──────────────────────────────

def write_dependency_matrix(records: list, abr_set: set, output_dir: Path, top_n: int = 50):
    # 상위 인터널라이저 10개 선정 (전체 PFOF 기준)
    venue_pfof: dict[str, float] = defaultdict(float)
    for rec in records:
        if rec["crd"] in abr_set:
            continue
        t = _broker_totals(rec)
        for vd in list(t["equity_venues"].values()) + list(t["options_venues"].values()):
            if vd["mic"] not in EXCHANGE_MICS:
                key = vd["mic"] or vd["name"]
                venue_pfof[key] += vd["pfof_sum"]

    top_venues = sorted(venue_pfof, key=venue_pfof.get, reverse=True)[:10]

    # 상위 브로커 N개 (PFOF 기준, ABR 제외)
    non_abr = [r for r in records if r["crd"] not in abr_set]
    non_abr.sort(key=lambda r: _broker_totals(r)["pfof_total_usd"], reverse=True)
    top_brokers = non_abr[:top_n]

    # 각 브로커의 venue별 평균 orderPct 계산
    def get_venue_pct(rec, venue_key):
        total_pct = 0.0
        count = 0
        for m in rec["monthly"]:
            for v in m["equity_venues"] + m["options_venues"]:
                k = v["mic"] or v["name"]
                if k == venue_key:
                    total_pct += v["orderPct"]
                    count += 1
        return round(total_pct / count, 1) if count else 0.0

    # venue_key → display_name 매핑
    venue_names: dict[str, str] = {}
    for rec in records:
        t = _broker_totals(rec)
        for vd in list(t["equity_venues"].values()) + list(t["options_venues"].values()):
            k = vd["mic"] or vd["name"]
            if k not in venue_names:
                venue_names[k] = vd["name"]

    rows = []
    for rec in top_brokers:
        t = _broker_totals(rec)
        row = {
            "broker_name":    rec["broker_name"],
            "crd":            rec["crd"],
            "pfof_total_usd": t["pfof_total_usd"],
        }
        for vk in top_venues:
            col_name = venue_names.get(vk, vk)
            row[col_name] = get_venue_pct(rec, vk)
        rows.append(row)

    if not rows:
        return

    cols = ["broker_name", "crd", "pfof_total_usd"] + [venue_names.get(vk, vk) for vk in top_venues]
    _write_csv(output_dir / "04_dependency_matrix.csv", cols, rows)


# ─── View 5: 월별 트렌드 ──────────────────────────────────────────────────────

def write_monthly_trend(records: list, output_dir: Path):
    rows = []
    for rec in records:
        prev_total = None
        for m in sorted(rec["monthly"], key=lambda x: x["month"]):
            eq = _sum_pfof(m["equity_venues"])
            op = _sum_pfof(m["options_venues"])
            total = eq + op
            mom = round((total - prev_total) / prev_total * 100, 1) if prev_total else None
            rows.append({
                "broker_name":     rec["broker_name"],
                "crd":             rec["crd"],
                "month":           m["month"],
                "pfof_equity_usd": round(eq, 2),
                "pfof_options_usd":round(op, 2),
                "pfof_total_usd":  round(total, 2),
                "mom_change_pct":  mom if mom is not None else "",
            })
            prev_total = total if total > 0 else prev_total

    cols = ["broker_name", "crd", "month", "pfof_equity_usd",
            "pfof_options_usd", "pfof_total_usd", "mom_change_pct"]
    _write_csv(output_dir / "05_monthly_trend.csv", cols, rows)


# ─── CSV 유틸 ───────────────────────────────────────────────────────────────

def _write_csv(path: Path, cols: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  ✓ {path.name}  ({len(rows)} rows)")


# ─── 콘솔 요약 ─────────────────────────────────────────────────────────────

def print_summary(overview_rows: list):
    print("\n" + "━" * 72)
    print(f"{'Rank':<5} {'Broker':<42} {'Equity PFOF':>12} {'Options PFOF':>13} {'Tags'}")
    print("━" * 72)
    for r in overview_rows[:20]:
        eq = f"${r['pfof_equity_usd']:>10,.0f}"
        op = f"${r['pfof_options_usd']:>11,.0f}"
        print(f"{r['rank']:<5} {r['broker_name'][:42]:<42} {eq} {op}  {r['tags']}")
    print("━" * 72)
    total_brokers = len(overview_rows)
    zero_pfof = sum(1 for r in overview_rows if r["is_zero_pfof"])
    abr_count = sum(1 for r in overview_rows if r["is_abr"])
    print(f"총 {total_brokers}개 브로커 | zero-PFOF: {zero_pfof}개 | ABR(대리제출): {abr_count}개")


# ─── 분기 자동 감지 ──────────────────────────────────────────────────────────

def auto_detect_quarter():
    """현재 날짜 기반으로 분석 대상 year/qtr 반환.

    cron 실행일 기준:
      2월 → 전년도 Q4 / 5월 → 당해 Q1 / 8월 → 당해 Q2 / 11월 → 당해 Q3
    """
    import datetime
    today = datetime.date.today()
    m, y = today.month, today.year
    month_map = {2: (y - 1, "Q4"), 5: (y, "Q1"), 8: (y, "Q2"), 11: (y, "Q3")}
    if m not in month_map:
        # 지정 월 외 수동 실행 시 가장 최근 완결 분기 추정
        if   m <= 4:  return y - 1, "Q4"
        elif m <= 7:  return y, "Q1"
        elif m <= 10: return y, "Q2"
        else:         return y, "Q3"
    year, qtr = month_map[m]
    return str(year), qtr


# ─── 메인 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FINRA 606 NMS 브로커 마켓 센싱 분석")
    parser.add_argument("--year",   default=None,  help="분석 연도 (예: 2026)")
    parser.add_argument("--qtr",    default=None,  help="분기 (예: Q1)")
    parser.add_argument("--auto",   action="store_true", help="현재 날짜 기반 분기 자동 감지")
    parser.add_argument("--top",    type=int, default=50, help="View4 매트릭스 상위 N개 브로커 (기본: 50)")
    parser.add_argument("--output", default=None,  help="출력 디렉토리 (기본: output/{year}_{qtr}/)")
    args = parser.parse_args()

    if args.auto or (args.year is None and args.qtr is None):
        year, qtr = auto_detect_quarter()
        print(f"[auto] 분석 대상: {year} {qtr}")
    else:
        year = args.year or "2026"
        qtr  = args.qtr  or "Q1"

    out_dir = Path(args.output) if args.output else (OUTPUT_DIR / f"{year}_{qtr}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. 다운로드
    zf = download_bulk_zip(year, qtr)

    # 2. 파일 선택 & ABR
    xml_map = select_best_files(zf)
    abr_set = parse_abr_set(zf)

    # 3. 전체 파싱
    print(f"\n파싱 중 ({len(xml_map)}개 브로커)...")
    records = []
    for i, (crd, fname) in enumerate(xml_map.items(), 1):
        if i % 50 == 0 or i == len(xml_map):
            print(f"  [{i}/{len(xml_map)}] ...", end="\r")
        xml_bytes = zf.read(fname)
        rec = parse_xml(xml_bytes, fname)
        if rec:
            records.append(rec)
    print(f"\n  → {len(records)}개 파싱 완료")

    # 4. 각 View 생성
    print(f"\n출력 디렉토리: {out_dir}\n")
    overview = write_broker_overview(records, abr_set, out_dir)
    write_internalizer_share(records, out_dir)
    write_options_exchange(records, out_dir)
    write_dependency_matrix(records, abr_set, out_dir, top_n=args.top)
    write_monthly_trend(records, out_dir)

    # 5. 콘솔 요약
    print_summary(overview)


if __name__ == "__main__":
    main()
