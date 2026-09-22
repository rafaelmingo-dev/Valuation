from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pandas as pd
import streamlit as st

from radar_store import read_snapshot
from radar_ui import (
    app_css,
    main_table,
    render_asset_detail,
    render_method_audit,
    render_selectable_table,
    render_summary_cards,
)

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = BASE_DIR / "data" / "latest_snapshot.json.gz"
LOG_PATH = BASE_DIR / "data" / "latest_run.log"
RUNNER_PATH = BASE_DIR / "radar_runner.py"

st.set_page_config(
    page_title="Radar W1 — Qualidade × Valuation",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)
app_css()


# =============================================================================
# SNAPSHOT / ATUALIZAÇÃO
# =============================================================================

def _load_snapshot_uncached():
    if not SNAPSHOT_PATH.exists():
        return None
    return read_snapshot(SNAPSHOT_PATH)


@st.cache_data(show_spinner=False)
def load_snapshot_cached(mtime_ns: int):
    return read_snapshot(SNAPSHOT_PATH)


def load_snapshot():
    if not SNAPSHOT_PATH.exists():
        return None
    return load_snapshot_cached(SNAPSHOT_PATH.stat().st_mtime_ns)


def run_update():
    cmd = [
        sys.executable,
        str(RUNNER_PATH),
        "--output", str(SNAPSHOT_PATH),
        "--log", str(LOG_PATH),
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            text=True,
            capture_output=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return False, "A atualização ultrapassou 30 minutos. O snapshot anterior foi preservado."

    payload = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            break
        except Exception:
            continue

    if proc.returncode == 0 and isinstance(payload, dict) and payload.get("ok"):
        load_snapshot_cached.clear()
        return True, f"Radar atualizado: {payload.get('processed')}/{payload.get('expected')} ativos processados."

    error = payload.get("error") if isinstance(payload, dict) else None
    return False, error or (proc.stderr or proc.stdout or "Falha desconhecida na atualização.")[-1200:]


# =============================================================================
# CAMADA EXECUTIVA DE DECISÃO
#
# Esta seção NÃO recalcula valuation, Quality Score, Confidence Score ou alvo.
# Ela apenas traduz os resultados que já existem no snapshot para uma leitura
# mais clara de carteira.
# =============================================================================

def _normalize_text(value) -> str:
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def _as_dataframe(value):
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, list):
        try:
            return pd.DataFrame(value)
        except Exception:
            return pd.DataFrame()
    if isinstance(value, dict):
        try:
            return pd.DataFrame(value)
        except Exception:
            try:
                return pd.DataFrame.from_dict(value, orient="index")
            except Exception:
                return pd.DataFrame()
    return pd.DataFrame()


def _ensure_asset_column(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "Ativo" in out.columns:
        out["Ativo"] = out["Ativo"].astype(str)
        return out

    normalized = {_normalize_text(c): c for c in out.columns}
    for candidate in ("ativo", "symbol", "ticker"):
        if candidate in normalized:
            out = out.rename(columns={normalized[candidate]: "Ativo"})
            out["Ativo"] = out["Ativo"].astype(str)
            return out

    idx_name = out.index.name
    if idx_name is not None and _normalize_text(idx_name) in {"ativo", "symbol", "ticker"}:
        out = out.reset_index().rename(columns={idx_name: "Ativo"})
        out["Ativo"] = out["Ativo"].astype(str)
        return out

    if not isinstance(out.index, pd.RangeIndex):
        out = out.reset_index().rename(columns={"index": "Ativo"})
        out["Ativo"] = out["Ativo"].astype(str)

    return out


def _find_detailed_decision_table(snapshot) -> pd.DataFrame:
    """
    Procura no snapshot a tabela final individual por ativo produzida pelo motor.
    A identificação é feita pelo conteúdo das colunas, sem depender do nome da
    variável usada no valuation_engine.py.
    """
    tables = snapshot.get("tables", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(tables, dict):
        return pd.DataFrame()

    signatures = (
        "bom ativo para carteira",
        "empresa forte",
        "motivo da qualidade",
        "evidencias da qualidade",
        "esta barato hoje",
        "preco esta atrativo",
        "motivo do preco",
        "evidencias do valuation",
        "conclusao direta",
        "leitura para carteira",
        "o que fazer",
    )

    best = pd.DataFrame()
    best_score = -1

    for raw in tables.values():
        df = _ensure_asset_column(_as_dataframe(raw))
        if df.empty or "Ativo" not in df.columns:
            continue

        normalized_columns = {_normalize_text(c) for c in df.columns}
        score = sum(1 for sig in signatures if sig in normalized_columns)

        if score > best_score:
            best_score = score
            best = df

    # Exige que seja de fato uma tabela explicativa e não apenas uma tabela
    # genérica do Radar.
    if best_score >= 3:
        return best

    return pd.DataFrame()


def _decision_lookup(snapshot):
    df = _find_detailed_decision_table(snapshot)
    if df.empty:
        return {}

    lookup = {}
    for _, row in df.iterrows():
        symbol = str(row.get("Ativo", "")).strip()
        if symbol:
            lookup[symbol] = row.to_dict()
    return lookup


def _parse_number(value):
    """
    Conversão apenas para FORMATAÇÃO da interface.
    Não participa de nenhum cálculo econômico.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("R$", "").replace("%", "").replace("/100", "").strip()

    if "," in text:
        text = text.replace(".", "").replace(",", ".")

    try:
        return float(text)
    except Exception:
        return None


def _fmt_money(value):
    n = _parse_number(value)
    if n is None:
        text = "" if value is None else str(value).strip()
        return text or "n/d"

    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_score(value):
    n = _parse_number(value)
    if n is None:
        text = "" if value is None else str(value).strip()
        return text or "n/d"
    return f"{n:.1f}/100".replace(".", ",")


def _fmt_percent(value):
    """
    Formata percentuais sem alterar o valor econômico.

    O radar_df mantém Upside/Downside como razão decimal em campos numéricos
    (ex.: -0.196 = -19,6%). Quando o valor já chega como texto com "%", ele
    já está em pontos percentuais e não deve ser multiplicado novamente.
    """
    n = _parse_number(value)
    if n is None:
        text = "" if value is None else str(value).strip()
        return text or "n/d"

    if isinstance(value, str):
        if "%" not in value:
            n *= 100.0
    else:
        n *= 100.0

    return f"{n:+.1f}%".replace(".", ",")


def _detail_value(detail: dict, *keys):
    for key in keys:
        if key in detail:
            value = str(detail[key]).strip()
            if value and value.lower() != "nan":
                return value
    return None


def _quality_answer(row: pd.Series, detail: dict | None):
    """
    Não cria corte novo de Quality Score.

    Se o motor já trouxe a conclusão individual, usa essa conclusão.
    Caso contrário, usa Classe de qualidade + Quality Score já existentes.
    """
    detail = detail or {}

    explicit = _detail_value(
        detail,
        "Bom ativo para carteira?",
        "Empresa forte?",
    )
    if explicit:
        return explicit

    quality_class = str(row.get("Classe de qualidade", "")).strip()
    qn = _normalize_text(quality_class)
    score = _fmt_score(row.get("Quality Score"))

    if "excelente" in qn:
        return f"SIM — EXCELENTE NO GRUPO ({score})"

    if "forte" in qn:
        # Importante: evita transformar automaticamente "forte no grupo"
        # em "excelente absoluto". O score permanece visível para julgamento.
        return f"ATENÇÃO — FORTE NO GRUPO ({score})"

    if "intermedi" in qn:
        return f"NÃO PRIORITÁRIA — QUALIDADE INTERMEDIÁRIA ({score})"

    if quality_class:
        return f"NÃO PRIORITÁRIA — {quality_class.upper()} ({score})"

    return f"QUALIDADE NÃO CLASSIFICADA ({score})"


def _price_answer(row: pd.Series, detail: dict | None):
    """
    Traduz somente o Valuation Status já calculado pelo motor.
    Não cria margem de segurança ou limite novo de upside/downside.
    """
    detail = detail or {}

    explicit = _detail_value(
        detail,
        "Está barato hoje?",
        "Preço está atrativo?",
    )
    if explicit:
        return explicit

    valuation = str(row.get("Valuation Status", "")).strip()
    vn = _normalize_text(valuation)

    if "atrativo" in vn or "consenso de upside" in vn:
        return "SIM — PREÇO ATRATIVO PELO MODELO"

    if "caro" in vn or "consenso de downside" in vn:
        return "NÃO — CARO PELO MODELO"

    if (
        "inconclus" in vn
        or "misto" in vn
        or "diverg" in vn
        or "sem consenso" in vn
    ):
        return "INCONCLUSIVO — MÉTODOS NÃO CONFIRMAM ENTRADA"

    if "nav" in vn or "sotp" in vn or "sem alvo" in vn:
        return "N/D — NAV/SOTP PENDENTE"

    return valuation or "N/D"


def _portfolio_conclusion(row: pd.Series, detail: dict | None):
    detail = detail or {}

    explicit = _detail_value(
        detail,
        "Conclusão direta",
        "Leitura para carteira",
    )
    if explicit:
        return explicit

    status = str(row.get("Status de Carteira", "")).strip()
    sn = _normalize_text(status)

    if "candidato prioritario" in sn:
        return "⭐ BOA EMPRESA + PREÇO ATRATIVO"

    if "aguardar preco" in sn:
        return "🟡 BOA EMPRESA — AGUARDAR PREÇO"

    if "valuation inconclusivo" in sn:
        return "🟣 BOA EMPRESA — PREÇO NÃO CONFIRMADO"

    if "monitorar" in sn:
        return "⚪ MONITORAR — QUALIDADE/PREÇO AINDA NÃO CONFIRMAM PRIORIDADE"

    if "fora da prioridade" in sn:
        return "🔴 NÃO PRIORITÁRIO NO ESTADO ATUAL"

    if "nav" in sn or "sotp" in sn:
        return "⚪ PREÇO NÃO CLASSIFICÁVEL — NAV/SOTP PENDENTE"

    return status or "N/D"


def _quality_reason(row: pd.Series, detail: dict | None):
    detail = detail or {}

    explicit = _detail_value(
        detail,
        "Motivo da qualidade — números",
        "Evidências da qualidade",
    )
    if explicit:
        return explicit

    score = _fmt_score(row.get("Quality Score"))
    cls = str(row.get("Classe de qualidade", "n/d"))

    return (
        f"Quality Score {score}; classificação relativa: {cls}. "
        "O detalhamento técnico abaixo mostra ROE, lucros, caixa, dívida, crescimento e dividendos."
    )


def _valuation_reason(row: pd.Series, detail: dict | None):
    detail = detail or {}

    explicit = _detail_value(
        detail,
        "Motivo do preço — números",
        "Evidências do valuation",
    )
    if explicit:
        return explicit

    price = _fmt_money(row.get("Preço atual"))
    target = _fmt_money(row.get("Alvo validado 12m"))
    upside = _fmt_percent(row.get("Upside/Downside"))
    valuation = str(row.get("Valuation Status", "n/d"))
    confidence = str(row.get("Confiança", "n/d"))

    return (
        f"Preço {price}; alvo validado 12m {target}; diferença {upside}. "
        f"Valuation: {valuation}. Confiança: {confidence}."
    )


def _what_to_do(row: pd.Series, detail: dict | None):
    detail = detail or {}

    explicit = _detail_value(detail, "O que fazer")
    if explicit:
        return explicit

    status = str(row.get("Status de Carteira", "")).strip()
    sn = _normalize_text(status)

    if "candidato prioritario" in sn:
        return "Aprofundar a tese e os riscos específicos antes de eventual inclusão em carteira."

    if "aguardar preco" in sn:
        return "Manter na watchlist e reavaliar quando o preço cair ou os fundamentos elevarem o alvo."

    if "valuation inconclusivo" in sn:
        return "Manter na watchlist; não usar o alvo composto isoladamente enquanto os métodos divergirem."

    if "monitorar" in sn:
        return "Acompanhar, mas não priorizar enquanto qualidade e/ou valuation não melhorarem."

    if "fora da prioridade" in sn:
        return "Não priorizar no estado atual; reavaliar somente com mudança relevante de preço ou fundamentos."

    if "nav" in sn or "sotp" in sn:
        return "Concluir NAV/SOTP antes de usar preço justo como base de decisão."

    return "Acompanhar a próxima atualização do Radar."


def _short_numeric_reason(row: pd.Series):
    """
    Texto curto e objetivo para a tabela principal.
    Usa somente campos já calculados.
    """
    return (
        f"Score {_fmt_score(row.get('Quality Score'))} • "
        f"preço {_fmt_money(row.get('Preço atual'))} • "
        f"alvo {_fmt_money(row.get('Alvo validado 12m'))} "
        f"({_fmt_percent(row.get('Upside/Downside'))}) • "
        f"{row.get('Valuation Status', 'n/d')}"
    )


def build_clear_decision_table(snapshot, radar_df: pd.DataFrame) -> pd.DataFrame:
    """
    Cria SOMENTE uma visão da interface.
    Os campos econômicos vêm do radar_df / snapshot sem recalculá-los.
    """
    if radar_df is None or radar_df.empty:
        return pd.DataFrame()

    details = _decision_lookup(snapshot)
    rows = []

    for _, row in radar_df.iterrows():
        symbol = str(row.get("Ativo", "")).strip()
        detail = details.get(symbol, {})

        rows.append(
            {
                "Ativo": symbol,
                "Qualidade para carteira": _quality_answer(row, detail),
                "Quality Score": row.get("Quality Score", "n/d"),
                "Classe no grupo": row.get("Classe de qualidade", "n/d"),
                "Preço atual": _fmt_money(row.get("Preço atual")),
                "Alvo 12m": _fmt_money(row.get("Alvo validado 12m")),
                "Upside/Downside": _fmt_percent(row.get("Upside/Downside")),
                "Está barato?": _price_answer(row, detail),
                "Confiança": row.get("Confiança", "n/d"),
                "Conclusão para carteira": _portfolio_conclusion(row, detail),
                "Motivo objetivo": _short_numeric_reason(row),
            }
        )

    return pd.DataFrame(rows)


def render_decision_card(snapshot, radar_df: pd.DataFrame, symbol: str):
    """
    Resumo executivo exibido ANTES do detalhamento técnico.
    """
    if not symbol or radar_df is None or radar_df.empty:
        return

    current = radar_df[radar_df["Ativo"].astype(str) == str(symbol)]
    if current.empty:
        return

    row = current.iloc[0]
    detail = _decision_lookup(snapshot).get(str(symbol), {})

    quality_answer = _quality_answer(row, detail)
    price_answer = _price_answer(row, detail)
    conclusion = _portfolio_conclusion(row, detail)
    quality_reason = _quality_reason(row, detail)
    valuation_reason = _valuation_reason(row, detail)
    action = _what_to_do(row, detail)

    asset_data = snapshot.get("assets", {}).get(symbol, {})
    name = asset_data.get("name", symbol)

    st.markdown(f"### {symbol} — {name}")

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Quality Score", _fmt_score(row.get("Quality Score")))
    with m2:
        st.metric("Preço atual", _fmt_money(row.get("Preço atual")))
    with m3:
        st.metric("Alvo validado 12m", _fmt_money(row.get("Alvo validado 12m")))
    with m4:
        st.metric("Upside/Downside", _fmt_percent(row.get("Upside/Downside")))

    with st.container(border=True):
        st.markdown("#### 1. É uma empresa forte para carteira?")
        st.markdown(f"**{quality_answer}**")
        st.write(quality_reason)

    with st.container(border=True):
        st.markdown("#### 2. O preço atual está atrativo?")
        st.markdown(f"**{price_answer}**")
        st.write(valuation_reason)

    with st.container(border=True):
        st.markdown("#### 3. Conclusão do Radar para carteira")
        st.markdown(f"### {conclusion}")
        st.write(action)

    st.caption(
        "A leitura acima apenas traduz os resultados já calculados pelo Radar. "
        "Não cria novo preço-alvo, novo Quality Score, novo peso ou nova margem de segurança."
    )


def render_decision_legend():
    with st.expander("Como interpretar as conclusões"):
        st.markdown(
            """
            **⭐ BOA EMPRESA + PREÇO ATRATIVO**  
            Qualidade aprovada e valuation favorável. É o grupo que merece aprofundamento primeiro; não significa compra automática.

            **🟡 BOA EMPRESA — AGUARDAR PREÇO**  
            A qualidade passa, mas o valuation indica que o preço atual está acima do valor encontrado pelo modelo.

            **🟣 BOA EMPRESA — PREÇO NÃO CONFIRMADO**  
            A qualidade passa, porém os métodos de valuation não convergem. Não use o alvo composto isoladamente como preço de entrada.

            **⚪ MONITORAR**  
            Qualidade e/ou valuation ainda não fornecem evidência suficiente para prioridade.

            **🔴 NÃO PRIORITÁRIO**  
            A combinação atual de qualidade e preço não justifica prioridade no Radar.

            **NAV/SOTP PENDENTE**  
            Não concluir barato/caro enquanto o valuation adequado da holding não estiver concluído.

            **Importante:** `Excelente/Forte no grupo` continua sendo uma comparação relativa dentro do universo W1.  
            Por isso o painel mantém separados o **Quality Score numérico** e a **Classe no grupo**.
            """
        )


# =============================================================================
# CABEÇALHO
# =============================================================================

st.title("RADAR W1 — QUALIDADE × VALUATION")
st.markdown(
    '<div class="radar-subtitle">Carteira de longo prazo • Qualidade • Valuation • Confiança • Seleção de ativos</div>',
    unsafe_allow_html=True,
)

snapshot = load_snapshot()

c1, c2 = st.columns([1.35, 4.65], vertical_alignment="center")
with c1:
    if st.button("🔄 ATUALIZAR RADAR", type="primary", width="stretch"):
        with st.spinner("Executando o motor completo do Radar W1. Aguarde..."):
            ok, message = run_update()
        if ok:
            st.success(message)
            st.rerun()
        else:
            st.error(message)

with c2:
    if snapshot:
        last = snapshot.get("run_finished_at") or snapshot.get("generated_at") or "n/d"
        execution = snapshot.get("execution", {})
        st.markdown(
            f'<div class="radar-meta">Última atualização: <b>{last}</b> &nbsp;•&nbsp; '
            f'Cobertura: <b>{execution.get("processed_total", "n/d")}/{execution.get("expected_assets", "n/d")}</b></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="radar-meta">Ainda não existe snapshot. Clique em <b>ATUALIZAR RADAR</b> para executar os 20 ativos.</div>',
            unsafe_allow_html=True,
        )


# =============================================================================
# SEM SNAPSHOT
# =============================================================================

if not snapshot:
    st.info(
        "O aplicativo está instalado corretamente, mas ainda não possui dados calculados. "
        "A primeira atualização executará o mesmo valuation_engine.py do projeto e criará o snapshot para o painel."
    )
    with st.expander("Ver último log disponível"):
        if LOG_PATH.exists():
            st.code(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-12000:])
        else:
            st.write("Nenhum log ainda.")
    st.stop()


# =============================================================================
# DADOS PRINCIPAIS
# =============================================================================

radar_df = main_table(snapshot)
clear_df = build_clear_decision_table(snapshot, radar_df)

render_summary_cards(radar_df)
st.write("")


# =============================================================================
# ABAS
# =============================================================================

tab_radar, tab_candidates, tab_watch, tab_detail, tab_methods, tab_help = st.tabs(
    ["Radar W1", "Candidatos", "Watchlist de qualidade", "Detalhar ativo", "Métodos", "Como funciona"]
)


# =============================================================================
# RADAR W1 — VISÃO PRINCIPAL
# =============================================================================

with tab_radar:
    st.subheader("Radar W1 — decisão rápida")
    st.caption(
        "A tabela responde diretamente: a empresa tem qualidade, o preço está atrativo "
        "e qual é a conclusão atual para carteira. Clique em uma linha para ver os motivos."
    )

    render_decision_legend()

    selected = render_selectable_table(clear_df, key="radar_main_clear")
    if selected:
        st.session_state["selected_asset"] = selected
        st.session_state["detail_asset_pending"] = selected

    active = st.session_state.get("selected_asset")
    if active:
        st.divider()
        render_decision_card(snapshot, radar_df, active)
        st.info(
            "Para abrir gráfico, candles, volume e auditoria técnica completa deste ativo, "
            "use a aba **Detalhar ativo**. O ativo selecionado será levado automaticamente para lá."
        )


# =============================================================================
# CANDIDATOS PRIORITÁRIOS
# =============================================================================

with tab_candidates:
    st.subheader("⭐ Candidatos prioritários")
    st.caption(
        "Ativos cuja combinação atual de qualidade, valuation e confiança já foi classificada "
        "pelo próprio Radar como candidata prioritária."
    )

    if radar_df.empty:
        st.info("Sem dados.")
    else:
        mask = radar_df["Status de Carteira"].astype(str).str.contains(
            "CANDIDATO PRIORITÁRIO",
            regex=False,
        )
        candidate_symbols = set(radar_df.loc[mask, "Ativo"].astype(str))
        candidates = clear_df[
            clear_df["Ativo"].astype(str).isin(candidate_symbols)
        ].copy()

        if candidates.empty:
            st.info("Nenhum ativo está nesta categoria na atualização atual.")
        else:
            picked = render_selectable_table(
                candidates,
                key="candidate_table_clear",
            )
            if picked:
                st.session_state["selected_asset"] = picked
                st.session_state["detail_asset_pending"] = picked
                st.divider()
                render_decision_card(snapshot, radar_df, picked)
                st.info(
                    "Para abrir gráfico, candles, volume e auditoria técnica completa deste candidato, "
                    "use a aba **Detalhar ativo**. O ativo selecionado será levado automaticamente para lá."
                )


# =============================================================================
# WATCHLIST DE QUALIDADE
# =============================================================================

with tab_watch:
    st.subheader("Watchlist de qualidade")
    st.caption(
        "Empresas classificadas como Forte/Excelente no grupo pelo motor. "
        "Qualidade e momento de entrada continuam separados."
    )

    watch = snapshot.get("tables", {}).get("EXECUTIVE_STRONG_WATCHLIST")

    if isinstance(watch, pd.DataFrame) and not watch.empty:
        w = _ensure_asset_column(watch)

        if "Ativo" in w.columns and not clear_df.empty:
            enrich = clear_df[
                [
                    "Ativo",
                    "Qualidade para carteira",
                    "Está barato?",
                    "Conclusão para carteira",
                ]
            ].copy()
            w = w.merge(enrich, on="Ativo", how="left")

        st.dataframe(
            w,
            width="stretch",
            hide_index=True,
            height=650,
        )

    else:
        # Fallback sem criar nova regra: reaproveita a Classe de qualidade
        # já calculada pelo motor.
        if radar_df.empty:
            st.info("Watchlist indisponível.")
        else:
            mask = radar_df["Classe de qualidade"].astype(str).str.contains(
                "FORTE|EXCELENTE",
                case=False,
                regex=True,
            )
            symbols = set(radar_df.loc[mask, "Ativo"].astype(str))
            fallback_watch = clear_df[
                clear_df["Ativo"].astype(str).isin(symbols)
            ].copy()

            if fallback_watch.empty:
                st.info("Watchlist indisponível.")
            else:
                st.dataframe(
                    fallback_watch,
                    width="stretch",
                    hide_index=True,
                    height=650,
                )


# =============================================================================
# DETALHAR ATIVO
# =============================================================================

with tab_detail:
    st.subheader("Detalhar ativo")

    order = [
        s
        for s in snapshot.get("asset_order", [])
        if s in snapshot.get("assets", {})
    ]

    default_symbol = st.session_state.get("selected_asset")
    default_index = order.index(default_symbol) if default_symbol in order else 0

    pending_symbol = st.session_state.pop("detail_asset_pending", None)
    if pending_symbol in order:
        st.session_state["detail_asset_select"] = pending_symbol

    symbol = st.selectbox(
        "Ativo",
        order,
        index=default_index,
        format_func=lambda s: (
            f"{s} — {snapshot.get('assets', {}).get(s, {}).get('name', s)}"
        ),
        key="detail_asset_select",
    )

    st.session_state["selected_asset"] = symbol

    # A leitura executiva vem ANTES do detalhamento técnico.
    render_decision_card(snapshot, radar_df, symbol)

    st.divider()
    st.subheader("Detalhamento técnico")
    render_asset_detail(snapshot, symbol, compact=False)


# =============================================================================
# MÉTODOS
# =============================================================================

with tab_methods:
    st.subheader("Métodos de valuation — comparação 4/4")
    st.caption(
        "Auditoria diagnóstica do método principal de 50% contra os três métodos secundários; "
        "não recalcula o valuation."
    )
    render_method_audit(snapshot)


# =============================================================================
# COMO FUNCIONA
# =============================================================================

with tab_help:
    st.subheader("Como funciona")
    st.markdown(
        """
        **1. O motor continua separado da interface.**  
        O botão **Atualizar Radar** executa `valuation_engine.py` integralmente em outro processo. A interface lê apenas o snapshot final.

        **2. A tela principal separa qualidade e preço.**  
        Para cada ativo, o painel responde:
        - se a empresa possui qualidade para carteira segundo os resultados já calculados;
        - se o preço está atrativo, caro ou inconclusivo;
        - qual é a conclusão atual para carteira;
        - quais números sustentam essa leitura.

        **3. `Forte/Excelente no grupo` não significa automaticamente qualidade absoluta excepcional.**  
        A classificação continua sendo relativa ao grupo econômico do universo W1. Por isso a interface mostra separadamente o **Quality Score numérico** e a **Classe no grupo**.

        **4. Qualidade e preço são perguntas diferentes.**  
        Uma empresa pode ter ótima qualidade e estar cara. Também pode haver boa qualidade com valuation inconclusivo. O painel não mistura essas duas perguntas em um novo score.

        **5. Nenhuma nova fórmula foi criada no `app.py`.**  
        A interface não recalcula DCF, RI, P/L, P/VP, EV/EBITDA, Dividend Yield, WACC, Ke, Quality Score, Confidence Score ou preço-alvo.

        **6. Métodos de valuation preservados.**  
        Para companhias não financeiras, o slot principal é DCF/FCFF ou RI setorial conforme a governança do próprio motor; os demais são P/L, EV/EBITDA e Dividend Yield. Para instituições financeiras, o motor usa RI, P/L, P/VP e Dividend Yield. Os pesos continuam **50% / 20% / 20% / 10%**.

        **7. ITSA4 permanece especial.**  
        O painel não transforma a proxy contábil da holding em preço-alvo validado; NAV/SOTP continua pendente.

        **8. Gráfico.**  
        Candles e volume são carregados separadamente apenas para visualização. Eles não alteram Quality Score, valuation, confiança ou Status de Carteira.
        """
    )


# =============================================================================
# LOG
# =============================================================================

with st.expander("Log da última atualização"):
    if LOG_PATH.exists():
        st.code(
            LOG_PATH.read_text(
                encoding="utf-8",
                errors="replace",
            )[-16000:]
        )
    else:
        st.write("Nenhum log disponível.")
