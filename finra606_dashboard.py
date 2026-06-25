#!/usr/bin/env python3
"""
finra606_dashboard.py
FINRA 606 NMS 브로커 마켓 센싱 대시보드

실행:
    cd "...NextSecurities-BD"
    /Users/seunghui.lee/Library/Python/3.9/bin/streamlit run 00-Claude/scripts/finra606_dashboard.py
"""

import re
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ─── 경로 ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent / "output"

# ─── 유틸 ──────────────────────────────────────────────────────────────────

def fmt_usd(val):
    """숫자 → $91.2M / $14.1K / $320 형태"""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return str(val)
    neg = v < 0
    av = abs(v)
    if av >= 1_000_000:
        s = f"${av / 1_000_000:.1f}M"
    elif av >= 1_000:
        s = f"${av / 1_000:.1f}K"
    else:
        s = f"${av:.0f}"
    return f"-{s}" if neg else s


def load_quarter(qtr_dir: Path):
    def read(name):
        p = qtr_dir / name
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    return {
        "overview":     read("01_broker_overview.csv"),
        "internalizer": read("02_internalizer_share.csv"),
        "options_exch": read("03_options_exchange.csv"),
        "dependency":   read("04_dependency_matrix.csv"),
        "monthly":      read("05_monthly_trend.csv"),
    }


def available_quarters():
    if not BASE_DIR.exists():
        return []
    return sorted(
        [d.name for d in BASE_DIR.iterdir() if d.is_dir() and re.match(r"\d{4}_Q\d", d.name)],
        reverse=True,
    )


def selected_total(df, eq_col, opt_col, show_eq, show_opt):
    """사이드바 Equity/Options 토글에 따라 합산 컬럼 계산"""
    eq = df[eq_col].fillna(0) if eq_col in df.columns else 0
    opt = df[opt_col].fillna(0) if opt_col in df.columns else 0
    if show_eq and show_opt:
        return eq + opt
    if show_eq:
        return eq
    return opt

# ─── 페이지 설정 ────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="FINRA 606 마켓 센싱",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 FINRA 606 NMS — 리테일 브로커 마켓 센싱")

quarters = available_quarters()
if not quarters:
    st.error(f"`{BASE_DIR}` 에 분석 결과가 없음. 먼저 `finra606_analyze.py --auto` 를 실행하세요.")
    st.stop()

# ─── 사이드바 ───────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("필터")
    selected_qtr = st.selectbox("분기", quarters, index=0)
    data = load_quarter(BASE_DIR / selected_qtr)
    ov = data["overview"]

    all_tags = set()
    for t in ov["tags"].dropna():
        all_tags.update(t.split("|"))
    all_tags.discard("")

    tag_filter = st.multiselect(
        "브로커 태그 필터",
        sorted(all_tags),
        default=[],
        help="선택 시 해당 태그가 포함된 브로커만 표시",
    )
    top_n = st.slider("브로커 랭킹 상위 N개", 10, 50, 20)
    st.caption(f"데이터: {selected_qtr.replace('_', ' ')}")

    st.divider()
    st.subheader("자산유형 필터")
    show_equity = st.checkbox("📈 Equity (주식)", value=True)
    show_options = st.checkbox("📊 Options (옵션)", value=True)
    if not show_equity and not show_options:
        st.warning("최소 1개는 켜야 함 — Equity로 복원")
        show_equity = True
    st.caption(
        "켜진 항목만 랭킹·차트·합계에 반영됨 (탭1·2·5에 적용). "
        "탭3(옵션 거래소)은 Options를 끄면 비활성화, "
        "탭4(의존도 매트릭스)는 데이터 구조상 항상 Equity+Options 합산."
    )

    st.divider()
    st.caption(
        "**데이터 업데이트 주기**\n\n"
        "FINRA 606 보고서는 분기 종료 후 ~30일 내 제출됩니다.\n\n"
        "| 업데이트일 | 대상 분기 |\n"
        "|---|---|\n"
        "| 매년 2/5 | 전년도 Q4 |\n"
        "| 매년 5/5 | 당해 Q1 |\n"
        "| 매년 8/5 | 당해 Q2 |\n"
        "| 매년 11/5 | 당해 Q3 |\n\n"
        "자동 갱신: `finra606_quarterly.sh` (cron)\n\n"
        "---\n\n"
        "**커버리지 한계**\n\n"
        "Rule 606(a)는 **held order** (시장가·지정가 등 고객 지정 주문) 만 적용.\n"
        "DriveWealth 등 not-held order 처리 B2B 브로커는 공시 의무 없음 → 이 데이터에 미포함."
    )

# 태그 필터 적용
def apply_tag_filter(df):
    if not tag_filter:
        return df
    mask = df["tags"].fillna("").apply(
        lambda t: any(tag in t.split("|") for tag in tag_filter)
    )
    return df[mask]

ov_filtered = apply_tag_filter(ov)

# ─── 탭 ─────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs(
    ["🏆 브로커 랭킹", "🏦 인터널라이저", "📈 옵션 거래소", "🔗 의존도 매트릭스", "📅 월별 트렌드"]
)

# ════════════════════════════════════════════════════════════════
# 탭 1 — 브로커 랭킹
# ════════════════════════════════════════════════════════════════
with tab1:
    with st.expander("📖 이 탭 설명", expanded=False):
        st.markdown("""
**PFOF(Payment for Order Flow) 수취액 기준으로 리테일 브로커를 랭킹합니다.**

PFOF = 브로커가 Citadel·Virtu 등 인터널라이저에 주문을 보낼 때 받는 수수료.
거래량이 많을수록 PFOF가 높아 리테일 브로커 규모의 proxy로 활용됩니다.

> ⚠️ **주의 — 이 데이터에 "없는" 브로커가 있음:**
>
> **① no-PFOF 브로커** (Fidelity·Vanguard 등): 거래소 직접 라우팅으로 PFOF=$0이지만 실거래량은 상위권. 데이터는 있으나 랭킹 하단. `no_pfof` 태그로 구분.
>
> **② Not-held order 브로커** (DriveWealth 등 B2B API): Rule 606(a)는 **held order** (시장가·지정가 — 고객이 가격·시간 조건을 직접 지정한 주문) 에만 적용됨. Not-held order (VWAP·알고리즘·B2B API 위임 주문) 처리 브로커는 분기 공시 의무 자체가 없음 → 데이터에 아예 없음.

**🏷 태그 의미**

| 태그 | 조건 | 의미 |
|------|------|------|
| `options_heavy` | 옵션 PFOF 비중 ≥ 40% | 옵션 트레이더 고객 비중 높음 |
| `active_trader` | limit order 비중 ≥ 50% | 능동적 트레이더 고객층 |
| `passive_retail` | market order 비중 ≥ 60% | 수동적 리테일 고객층 |
| `single_venue` | 특정 인터널라이저 집중도 ≥ 80% | 독점 계약 의심 → BD 접근 어려울 수 있음 |
| `no_pfof` | PFOF 총액 = 0 | 거래소 직접 라우팅 (실거래량 있음, 추적 불가) |
        """)
    ov = ov.copy()
    ov["__sel_total__"] = selected_total(ov, "pfof_equity_usd", "pfof_options_usd", show_equity, show_options)
    active = ov[ov["__sel_total__"] != 0]

    sel_label = "Equity+Options" if (show_equity and show_options) else ("Equity" if show_equity else "Options")

    col1, col2, col3 = st.columns(3)
    col1.metric(f"총 PFOF · {sel_label} (활성 브로커)", fmt_usd(active["__sel_total__"].sum()))
    col2.metric("활성 브로커 수", f"{len(active)}개")
    if show_options:
        top1_options = ov.nlargest(1, "pfof_options_usd")
        col3.metric(
            "Options PFOF 1위",
            top1_options["broker_name"].values[0] if len(top1_options) else "-",
            fmt_usd(top1_options["pfof_options_usd"].values[0]) if len(top1_options) else "",
        )
    else:
        top1_equity = ov.nlargest(1, "pfof_equity_usd")
        col3.metric(
            "Equity PFOF 1위",
            top1_equity["broker_name"].values[0] if len(top1_equity) else "-",
            fmt_usd(top1_equity["pfof_equity_usd"].values[0]) if len(top1_equity) else "",
        )

    st.divider()

    # 상위 N개 막대 차트 — 사이드바 Equity/Options 토글 반영, 선택된 합계 기준 랭킹
    ov_filtered = ov_filtered.copy()
    ov_filtered["__sel_total__"] = selected_total(
        ov_filtered, "pfof_equity_usd", "pfof_options_usd", show_equity, show_options
    )
    plot_df = ov_filtered.nlargest(top_n, "__sel_total__").copy()
    plot_df["broker_short"] = plot_df["broker_name"].str[:35]

    fig = go.Figure()
    if show_equity:
        fig.add_trace(go.Bar(
            y=plot_df["broker_short"],
            x=plot_df["pfof_equity_usd"],
            name="Equity PFOF",
            orientation="h",
            marker_color="#4A90D9",
            text=plot_df["pfof_equity_usd"].apply(fmt_usd),
            textposition="inside",
        ))
    if show_options:
        fig.add_trace(go.Bar(
            y=plot_df["broker_short"],
            x=plot_df["pfof_options_usd"],
            name="Options PFOF",
            orientation="h",
            marker_color="#F5A623",
            text=plot_df["pfof_options_usd"].apply(fmt_usd),
            textposition="inside",
        ))
    fig.update_layout(
        barmode="stack",
        height=max(400, top_n * 28),
        xaxis_title="PFOF (USD)",
        yaxis=dict(autorange="reversed"),
        margin=dict(l=10, r=10, t=10, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    st.plotly_chart(fig, use_container_width=True)

    # 테이블
    st.subheader("브로커 상세")
    tbl = ov_filtered.sort_values("__sel_total__", ascending=False)[
        ["rank", "broker_name", "pfof_equity_usd", "pfof_options_usd",
         "pfof_total_usd", "options_mix_pct", "internalization_rate",
         "top_internalizer", "top_internalizer_pct", "tags", "note"]
    ].copy()
    for col in ["pfof_equity_usd", "pfof_options_usd", "pfof_total_usd"]:
        tbl[col] = tbl[col].apply(fmt_usd)
    tbl.columns = ["Rank", "브로커", "Equity PFOF", "Options PFOF", "Total PFOF(전체)",
                   "Options Mix%", "내부화율%", "주요 인터널라이저", "집중도%", "태그", "비고"]
    st.dataframe(tbl, use_container_width=True, hide_index=True)


# ════════════════════════════════════════════════════════════════
# 탭 2 — 인터널라이저
# ════════════════════════════════════════════════════════════════
with tab2:
    with st.expander("📖 이 탭 설명", expanded=False):
        st.markdown("""
**Citadel Securities·Virtu Americas 등 시장 조성자(Market Maker)들이 리테일 주문을 얼마나 가져가는지 보여줍니다.**

- **인터널라이저**: 거래소를 거치지 않고 브로커 주문을 자체 처리하는 대형 트레이딩 회사.
  주문을 받는 대가로 브로커에게 PFOF를 지급합니다.
- **시장 점유율**: 전체 인터널라이저 PFOF 합산 대비 각 venue 비중
- **독점 브로커 수**: 해당 venue로 80%+ 집중 라우팅하는 브로커 수 (독점 계약 지표)

> 💡 **BD 활용:** 독점 브로커 수가 많은 인터널라이저 = 브로커와 배타적 관계. 해당 브로커와 협상 시 인터널라이저 계약 제약 여부 확인 필요.
        """)
    intl = data["internalizer"]
    if intl.empty:
        st.info("데이터 없음")
    else:
        intl = intl.copy()
        intl["__sel_total__"] = selected_total(
            intl, "equity_pfof_usd", "options_pfof_usd", show_equity, show_options
        )
        # 시장 점유율은 선택된 카테고리 합산 기준으로 재계산 (전체 venue 대비)
        denom = intl["__sel_total__"].sum()
        intl["__sel_share_pct__"] = (intl["__sel_total__"] / denom * 100) if denom else 0

        # 상위 15개만 — 선택된 합계 기준 랭킹
        top_intl = intl.nlargest(15, "__sel_total__")

        col_a, col_b = st.columns([1, 1])
        with col_a:
            st.subheader("시장 점유율")
            fig_pie = px.pie(
                top_intl,
                values="__sel_share_pct__",
                names="venue_name",
                hole=0.4,
            )
            fig_pie.update_traces(textposition="inside", textinfo="percent+label")
            fig_pie.update_layout(showlegend=False, margin=dict(t=10, b=10))
            st.plotly_chart(fig_pie, use_container_width=True)

        with col_b:
            st.subheader("Equity vs Options PFOF")
            fig_bar = go.Figure()
            if show_equity:
                fig_bar.add_trace(go.Bar(
                    y=top_intl["venue_name"].str[:30],
                    x=top_intl["equity_pfof_usd"],
                    name="Equity",
                    orientation="h",
                    marker_color="#4A90D9",
                ))
            if show_options:
                fig_bar.add_trace(go.Bar(
                    y=top_intl["venue_name"].str[:30],
                    x=top_intl["options_pfof_usd"],
                    name="Options",
                    orientation="h",
                    marker_color="#F5A623",
                ))
            fig_bar.update_layout(
                barmode="stack",
                height=450,
                yaxis=dict(autorange="reversed"),
                margin=dict(l=10, r=10, t=10, b=10),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
            )
            st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("인터널라이저 상세")
        st.caption("👆 행을 클릭하면 연결 브로커 목록을 확인할 수 있습니다.")

        intl_sorted = intl.nlargest(20, "__sel_total__").reset_index(drop=True)
        tbl2_raw = intl_sorted[["venue_name", "mic", "equity_pfof_usd", "options_pfof_usd",
                                  "total_pfof_received_usd", "__sel_share_pct__",
                                  "broker_count", "exclusive_broker_count"]].copy()

        event = st.dataframe(
            tbl2_raw,
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "venue_name":             st.column_config.TextColumn("Venue"),
                "mic":                    st.column_config.TextColumn("MIC"),
                "equity_pfof_usd":        st.column_config.NumberColumn("Equity PFOF", format="$%.0f"),
                "options_pfof_usd":       st.column_config.NumberColumn("Options PFOF", format="$%.0f"),
                "total_pfof_received_usd": st.column_config.NumberColumn("Total PFOF(전체)", format="$%.0f"),
                "__sel_share_pct__":      st.column_config.NumberColumn(f"시장점유율% ({sel_label})", format="%.2f%%"),
                "broker_count":           st.column_config.NumberColumn("브로커수(전체)"),
                "exclusive_broker_count": st.column_config.NumberColumn("독점브로커수(전체)"),
            },
            use_container_width=True,
            hide_index=True,
            key="intl_table",
        )

        # 드릴다운: 행 선택 시
        if event.selection.rows:
            selected_row = intl_sorted.iloc[event.selection.rows[0]]
            selected_venue = selected_row["venue_name"]

            st.divider()
            st.markdown(f"#### 🔍 {selected_venue} — 연결 브로커")

            dep = data["dependency"]
            drill_tab1, drill_tab2 = st.tabs(
                [f"전체 라우팅 브로커 ({int(selected_row['broker_count'])}개)",
                 f"독점 계약 브로커 ({int(selected_row['exclusive_broker_count'])}개, ≥80%)"]
            )

            with drill_tab1:
                st.caption("의존도 매트릭스 기준 (PFOF 상위 50개 브로커 범위)")
                if dep.empty:
                    st.info("의존도 매트릭스 데이터 없음")
                else:
                    # venue 이름 앞 15자로 열 매칭
                    venue_key = selected_venue[:15]
                    venue_col = next(
                        (c for c in dep.columns if venue_key.lower() in c.lower()),
                        None
                    )
                    if venue_col:
                        drill_df = dep[dep[venue_col] > 0][
                            ["broker_name", "pfof_total_usd", venue_col]
                        ].copy().sort_values(venue_col, ascending=False)
                        drill_df.columns = ["브로커", "Total PFOF", f"{venue_col[:30]} 라우팅%"]
                        drill_df["Total PFOF"] = drill_df["Total PFOF"].apply(fmt_usd)
                        st.dataframe(drill_df, hide_index=True, use_container_width=True)
                    else:
                        st.info(f"의존도 매트릭스에 '{selected_venue[:20]}' 열 없음 (상위 10개 인터널라이저 외)")

            with drill_tab2:
                excl = ov[
                    ov["top_internalizer"].str.contains(selected_venue[:20], na=False, regex=False) &
                    (ov["top_internalizer_pct"] >= 80)
                ][["broker_name", "pfof_total_usd", "pfof_options_usd",
                   "top_internalizer_pct", "tags"]].copy()
                excl = excl.sort_values("pfof_total_usd", ascending=False)
                if excl.empty:
                    st.info("독점 계약 브로커 없음 (top_internalizer_pct ≥ 80 기준)")
                else:
                    excl["pfof_total_usd"]   = excl["pfof_total_usd"].apply(fmt_usd)
                    excl["pfof_options_usd"] = excl["pfof_options_usd"].apply(fmt_usd)
                    excl.columns = ["브로커", "Total PFOF", "Options PFOF", "집중도%", "태그"]
                    st.dataframe(excl, hide_index=True, use_container_width=True)


# ════════════════════════════════════════════════════════════════
# 탭 3 — 옵션 거래소
# ════════════════════════════════════════════════════════════════
with tab3:
    with st.expander("📖 이 탭 설명", expanded=False):
        st.markdown("""
**CBOE·NASDAQ Options·MIAX 등 옵션 거래소들이 리테일 옵션 주문을 얼마나 유치하는지 보여줍니다.**

| 지표 | 의미 |
|------|------|
| `avg_order_flow_pct` | 전체 브로커 옵션 주문 중 해당 거래소 평균 비중 (%) |
| `pfof_paid_usd` | 해당 거래소가 브로커에 지급한 PFOF 합산 |
| `broker_count` | 해당 거래소로 라우팅하는 브로커 수 |

> ⚠️ **음수 PFOF** = 거래소가 브로커에게 **리베이트를 지급** (maker-taker 구조).
> 음수가 클수록 거래소가 주문 유치를 위해 더 많이 지불하는 것 → 경쟁이 치열한 거래소.
        """)
    oe = data["options_exch"]
    if not show_options:
        st.info("사이드바에서 Options가 꺼져 있어 이 탭은 비활성화됨 (옵션 전용 데이터). Options를 켜면 표시됨.")
    elif oe.empty:
        st.info("데이터 없음")
    else:
        oe_active = oe[oe["broker_count"] > 0].copy()

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Order Flow 비중 (%)")
            fig_oe1 = px.bar(
                oe_active.sort_values("avg_order_flow_pct", ascending=True),
                x="avg_order_flow_pct",
                y="exchange_name",
                orientation="h",
                text="avg_order_flow_pct",
                color="avg_order_flow_pct",
                color_continuous_scale="Blues",
            )
            fig_oe1.update_traces(texttemplate="%{text:.2f}%", textposition="outside")
            fig_oe1.update_layout(
                height=450, showlegend=False,
                coloraxis_showscale=False,
                margin=dict(l=10, r=10, t=10, b=10),
            )
            st.plotly_chart(fig_oe1, use_container_width=True)

        with col_b:
            st.subheader("PFOF 지급액 (양수=수취, 음수=rebate)")
            oe_active["color"] = oe_active["pfof_paid_usd"].apply(
                lambda x: "#E74C3C" if x < 0 else "#27AE60"
            )
            fig_oe2 = px.bar(
                oe_active.sort_values("pfof_paid_usd"),
                x="pfof_paid_usd",
                y="exchange_name",
                orientation="h",
                color="pfof_paid_usd",
                color_continuous_scale=["#E74C3C", "#FFFFFF", "#27AE60"],
                color_continuous_midpoint=0,
            )
            fig_oe2.update_layout(
                height=450, showlegend=False,
                coloraxis_showscale=False,
                margin=dict(l=10, r=10, t=10, b=10),
            )
            st.plotly_chart(fig_oe2, use_container_width=True)

        st.caption("음수 PFOF = 거래소가 브로커에게 rebate 지급 (maker-taker 구조)")


# ════════════════════════════════════════════════════════════════
# 탭 4 — 의존도 매트릭스
# ════════════════════════════════════════════════════════════════
with tab4:
    with st.expander("📖 이 탭 설명", expanded=False):
        st.markdown("""
**각 브로커(행)가 어느 인터널라이저(열)에 주문을 몇 % 보내는지 히트맵으로 표시합니다.**

색이 짙을수록 해당 인터널라이저 의존도가 높습니다. PFOF 상위 30개 브로커만 표시.

**🔍 읽는 법**

| 패턴 | 의미 |
|------|------|
| 특정 행에서 한 열만 짙음 | 해당 브로커는 단일 인터널라이저와 독점 계약 중 (`single_venue` 태그) |
| 여러 열에 고르게 분산 | 다양한 인터널라이저 활용 → 파트너십 협상 여지 있음 |
| 전 열이 옅음 | no-PFOF 브로커 (Fidelity류) — 거래소 직접 라우팅 |

> 💡 **BD 활용:** `single_venue` 브로커는 신규 파트너십 진입 장벽이 높음. 분산 라우팅 브로커를 우선 타겟팅.
        """)
    dep = data["dependency"]
    if dep.empty:
        st.info("데이터 없음")
    else:
        venue_cols = [c for c in dep.columns if c not in ("broker_name", "crd", "pfof_total_usd")]
        matrix_df = dep.set_index("broker_name")[venue_cols].fillna(0)

        # 상위 30개 (pfof_total 기준)
        top30_names = dep.nlargest(30, "pfof_total_usd")["broker_name"].tolist()
        matrix_top = matrix_df.loc[matrix_df.index.isin(top30_names)]

        # venue 이름 짧게
        short_venues = [v[:25] for v in venue_cols]

        fig_hm = go.Figure(data=go.Heatmap(
            z=matrix_top.values,
            x=short_venues,
            y=[n[:35] for n in matrix_top.index],
            colorscale="Blues",
            text=matrix_top.values.round(1),
            texttemplate="%{text}%",
            hovertemplate="브로커: %{y}<br>인터널라이저: %{x}<br>비중: %{z:.1f}%<extra></extra>",
        ))
        fig_hm.update_layout(
            height=max(500, len(matrix_top) * 22),
            xaxis=dict(tickangle=-30),
            margin=dict(l=10, r=10, t=10, b=60),
        )
        st.plotly_chart(fig_hm, use_container_width=True)
        st.caption(
            "값 = 해당 브로커가 해당 인터널라이저로 라우팅하는 평균 주문 비율(%) · "
            "이 매트릭스는 원본 데이터 구조상 Equity+Options 합산 기준이라 사이드바 토글이 적용되지 않음"
        )


# ════════════════════════════════════════════════════════════════
# 탭 5 — 월별 트렌드
# ════════════════════════════════════════════════════════════════
with tab5:
    with st.expander("📖 이 탭 설명", expanded=False):
        st.markdown("""
**분기 내 월별(Jan / Feb / Mar) PFOF 변동을 추적합니다.**

MoM(Month-over-Month) 증감률로 브로커별 거래량 모멘텀을 파악할 수 있습니다.

| 신호 | 해석 |
|------|------|
| MoM 지속 플러스 📈 | 리테일 유저 유입 증가 — 성장 중인 브로커 |
| MoM 마이너스 전환 📉 | 경쟁사에 고객 이탈 가능성 |
| 변동 없음 (평탄) | 안정적이나 성장 동력 부재 |

> 💡 기본값은 PFOF 상위 10개 브로커. 아래 셀렉터에서 비교하고 싶은 브로커를 직접 선택할 수 있습니다.
        """)
    mon = data["monthly"]
    if mon.empty:
        st.info("데이터 없음")
    else:
        mon = mon.copy()
        mon["__sel_total__"] = selected_total(
            mon, "pfof_equity_usd", "pfof_options_usd", show_equity, show_options
        )

        # 기본: 선택된 카테고리(Equity/Options) 합산 상위 10개 브로커
        top10_names = (
            mon.groupby("broker_name")["__sel_total__"]
            .sum()
            .nlargest(10)
            .index.tolist()
        )
        selected_brokers = st.multiselect(
            "브로커 선택",
            options=sorted(mon["broker_name"].unique()),
            default=top10_names,
        )

        if selected_brokers:
            mon_sel = mon[mon["broker_name"].isin(selected_brokers)].copy()
            mon_sel["month_label"] = mon_sel["month"].map({1: "Jan", 2: "Feb", 3: "Mar"})
            mon_sel = mon_sel.sort_values(["broker_name", "month"])
            # MoM도 선택된 카테고리 합산 기준으로 재계산
            mon_sel["__sel_mom_pct__"] = (
                mon_sel.groupby("broker_name")["__sel_total__"].pct_change() * 100
            )

            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader(f"월별 PFOF · {sel_label}")
                fig_line = px.line(
                    mon_sel,
                    x="month_label",
                    y="__sel_total__",
                    color="broker_name",
                    markers=True,
                    category_orders={"month_label": ["Jan", "Feb", "Mar"]},
                )
                fig_line.update_layout(
                    height=420,
                    xaxis_title="",
                    yaxis_title="PFOF (USD)",
                    legend=dict(font=dict(size=10)),
                    margin=dict(l=10, r=10, t=10, b=10),
                )
                st.plotly_chart(fig_line, use_container_width=True)

            with col_b:
                st.subheader("전월 대비 증감률 (%)")
                mon_mom = mon_sel[mon_sel["__sel_mom_pct__"].notna()]
                if not mon_mom.empty:
                    fig_mom = px.bar(
                        mon_mom,
                        x="month_label",
                        y="__sel_mom_pct__",
                        color="broker_name",
                        barmode="group",
                        category_orders={"month_label": ["Feb", "Mar"]},
                    )
                    fig_mom.add_hline(y=0, line_color="gray", line_dash="dash")
                    fig_mom.update_layout(
                        height=420,
                        xaxis_title="",
                        yaxis_title="MoM 변화율 (%)",
                        legend=dict(font=dict(size=10)),
                        margin=dict(l=10, r=10, t=10, b=10),
                    )
                    st.plotly_chart(fig_mom, use_container_width=True)
                else:
                    st.info("전월 대비 데이터 없음 (단일 월 데이터)")

            # 테이블
            st.subheader("월별 상세")
            tbl5 = mon_sel[["broker_name", "month_label", "pfof_equity_usd",
                              "pfof_options_usd", "__sel_total__", "__sel_mom_pct__"]].copy()
            for col in ["pfof_equity_usd", "pfof_options_usd", "__sel_total__"]:
                tbl5[col] = tbl5[col].apply(fmt_usd)
            tbl5["__sel_mom_pct__"] = tbl5["__sel_mom_pct__"].apply(
                lambda v: f"{v:.1f}%" if pd.notna(v) else "-"
            )
            tbl5.columns = ["브로커", "월", "Equity PFOF", "Options PFOF",
                             f"선택 합계({sel_label})", "MoM(%)"]
            st.dataframe(tbl5, use_container_width=True, hide_index=True)
