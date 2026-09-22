from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
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


def _quality_is_approved(row: pd.Series) -> bool:
    """Usa somente a classe de qualidade já calculada pelo motor."""
    quality_class = _normalize_text(row.get("Classe de qualidade", ""))
    return "excelente" in quality_class or "forte" in quality_class


def _valuation_numbers(row: pd.Series):
    """
    Retorna preço, alvo e diferença percentual a partir dos campos já calculados.
    A diferença é derivada apenas quando o campo Upside/Downside não estiver disponível.
    """
    price = _parse_number(row.get("Preço atual"))
    target = _parse_number(row.get("Alvo validado 12m"))

    raw_upside = row.get("Upside/Downside")
    upside = _parse_number(raw_upside)

    if upside is not None:
        if isinstance(raw_upside, str) and "%" in raw_upside:
            upside_ratio = upside / 100.0
        else:
            upside_ratio = upside
    elif price is not None and target is not None and price != 0:
        upside_ratio = target / price - 1.0
    else:
        upside_ratio = None

    return price, target, upside_ratio


def _conclusive_valuation(row: pd.Series) -> str:
    """
    Valuation FINAL conclusivo pelo alvo oficial validado de 12 meses.

    Não cria margem de segurança nem novo preço-alvo:
    - alvo final > preço atual  -> ATRATIVO pelo alvo final;
    - alvo final < preço atual  -> CARO pelo alvo final;
    - alvo final = preço atual  -> NO PREÇO JUSTO;
    - sem alvo validado         -> N/D (ex.: holding que exige NAV/SOTP).

    A divergência entre os quatro métodos passa a ser tratada como CONFIANÇA/AUDITORIA,
    e não como ausência de conclusão do valuation final.
    """
    price, target, _ = _valuation_numbers(row)

    if price is None or target is None or price <= 0:
        return "N/D — SEM ALVO VALIDADO / NAV-SOTP PENDENTE"

    if target > price:
        return "ATRATIVO — ALVO FINAL ACIMA DO PREÇO"
    if target < price:
        return "CARO — ALVO FINAL ABAIXO DO PREÇO"
    return "NO PREÇO JUSTO — ALVO FINAL = PREÇO"


def _valuation_direction(row: pd.Series) -> str:
    verdict = _normalize_text(_conclusive_valuation(row))
    if "atrativo" in verdict:
        return "atrativo"
    if "caro" in verdict:
        return "caro"
    if "preco justo" in verdict:
        return "justo"
    return "nd"


def _method_audit_text(row: pd.Series) -> str:
    status = str(row.get("Valuation Status", "n/d")).strip() or "n/d"
    confidence = str(row.get("Confiança", "n/d")).strip() or "n/d"
    return f"{status} | confiança {confidence}"


def _price_answer(row: pd.Series, detail: dict | None):
    """
    Responde de forma conclusiva usando o ALVO FINAL VALIDADO do próprio motor.

    O antigo Valuation Status (consenso/divergência dos quatro métodos) continua
    preservado como auditoria de confiança, mas não impede o veredito final.
    """
    return _conclusive_valuation(row)



def _portfolio_conclusion(row: pd.Series, detail: dict | None):
    """
    Combina a classe de qualidade já calculada com o valuation final conclusivo.
    Não cria score, alvo ou margem de segurança novos.
    """
    quality_ok = _quality_is_approved(row)
    direction = _valuation_direction(row)

    if direction == "nd":
        return "⚪ PREÇO NÃO CLASSIFICÁVEL — SEM ALVO VALIDADO / NAV-SOTP PENDENTE"

    if quality_ok and direction == "atrativo":
        return "⭐ BOA EMPRESA + VALUATION ATRATIVO"

    if quality_ok and direction == "caro":
        return "🟡 BOA EMPRESA, MAS VALUATION CARO"

    if quality_ok and direction == "justo":
        return "🟢 BOA EMPRESA — NO PREÇO JUSTO DO MODELO"

    if (not quality_ok) and direction == "atrativo":
        return "🔎 PREÇO ATRATIVO, MAS QUALIDADE NÃO PRIORITÁRIA"

    if (not quality_ok) and direction == "caro":
        return "🔴 NÃO PRIORITÁRIO — QUALIDADE NÃO PRIORITÁRIA + VALUATION CARO"

    return "⚪ MONITORAR — QUALIDADE NÃO PRIORITÁRIA"



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
    """
    Explica DUAS coisas separadas:
    1) o veredito final, sempre baseado no alvo oficial validado;
    2) por que os métodos podem divergir e qual é a confiança desse veredito.
    """
    detail = detail or {}

    price, target, upside_ratio = _valuation_numbers(row)
    verdict = _conclusive_valuation(row)
    method_status = str(row.get("Valuation Status", "n/d")).strip() or "n/d"
    confidence = str(row.get("Confiança", "n/d")).strip() or "n/d"

    price_txt = _fmt_money(price)
    target_txt = _fmt_money(target)
    upside_txt = _fmt_percent(upside_ratio) if upside_ratio is not None else "n/d"

    explicit = _detail_value(
        detail,
        "Motivo do preço — números",
        "Evidências do valuation",
    )

    base = (
        f"VEREDITO FINAL: {verdict}. Preço atual {price_txt}; alvo oficial validado 12m "
        f"{target_txt}; diferença {upside_txt}. Auditoria dos métodos: {method_status}. "
        f"Confiança: {confidence}."
    )

    if explicit:
        return base + " " + explicit

    return base



def _what_to_do(row: pd.Series, detail: dict | None):
    quality_ok = _quality_is_approved(row)
    direction = _valuation_direction(row)
    confidence = str(row.get("Confiança", "n/d")).strip() or "n/d"

    if direction == "nd":
        return "Não usar preço justo para decisão até existir alvo validado; para ITSA4, concluir NAV/SOTP."

    if quality_ok and direction == "atrativo":
        return (
            "Aprofundar a tese e os riscos específicos para eventual inclusão em carteira. "
            f"A confiança dos métodos é {confidence}; divergência reduz confiança, mas não muda o sinal do alvo final."
        )

    if quality_ok and direction == "caro":
        return (
            "Manter na watchlist e aguardar preço melhor ou aumento do alvo pelos fundamentos. "
            f"A confiança dos métodos é {confidence}."
        )

    if quality_ok and direction == "justo":
        return "Manter na watchlist e avaliar a tese; o preço está praticamente no alvo final do modelo."

    if (not quality_ok) and direction == "atrativo":
        return (
            "O preço está abaixo do alvo final, mas a qualidade não é prioritária no Radar. "
            "Investigar a qualidade antes de considerar entrada."
        )

    return "Não priorizar no estado atual; qualidade não prioritária e/ou valuation caro pelo alvo final."



def _short_numeric_reason(row: pd.Series):
    """Resumo curto do veredito final + auditoria dos métodos."""
    price, target, upside_ratio = _valuation_numbers(row)
    return (
        f"Preço {_fmt_money(price)} • alvo {_fmt_money(target)} "
        f"({_fmt_percent(upside_ratio) if upside_ratio is not None else 'n/d'}) • "
        f"{_conclusive_valuation(row)} • métodos: {_method_audit_text(row)}"
    )



def build_clear_decision_table(snapshot, radar_df: pd.DataFrame) -> pd.DataFrame:
    """
    Visão executiva sem recalcular o valuation.

    IMPORTANTE PARA O STREAMLIT:
    Preço atual, alvo e Upside/Downside permanecem em formato NUMÉRICO bruto.
    O render_selectable_table/radar_ui é quem formata esses campos. Isso evita
    converter os números em strings e depois vê-los aparecer como n/d.
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
                # Mantém os nomes que o radar_ui já conhece para formatação.
                "Preço atual": row.get("Preço atual"),
                "Alvo validado 12m": row.get("Alvo validado 12m"),
                "Upside/Downside": row.get("Upside/Downside"),
                "Valuation final": _conclusive_valuation(row),
                "Confiança": row.get("Confiança", "n/d"),
                "Auditoria dos métodos": row.get("Valuation Status", "n/d"),
                "Conclusão para carteira": _portfolio_conclusion(row, detail),
                "Motivo objetivo": _short_numeric_reason(row),
            }
        )

    return pd.DataFrame(rows)



def _row_border_for_attractive(row: pd.Series):
    """
    Destaca visualmente, sem alterar nenhum dado, as linhas em que o preço atual
    está abaixo do alvo final validado de 12 meses.

    A borda é apenas um sinal visual. O critério econômico continua sendo o mesmo
    já usado pelo painel: alvo validado 12m > preço atual.
    """
    price = _parse_number(row.get("Preço atual"))
    target = _parse_number(row.get("Alvo validado 12m"))

    if price is None or target is None or not (target > price):
        return [""] * len(row)

    base = "border-top: 2px solid #22c55e; border-bottom: 2px solid #22c55e;"
    styles = [base] * len(row)

    if styles:
        styles[0] += " border-left: 2px solid #22c55e;"
        styles[-1] += " border-right: 2px solid #22c55e;"

    return styles



def render_highlighted_selectable_table(
    df: pd.DataFrame,
    *,
    key: str,
    height: int = 650,
):
    """
    Tabela selecionável local do app.py.

    Mantém seleção por linha e acrescenta uma borda verde somente quando
    preço atual < alvo validado 12m. Nenhum valor econômico é recalculado.
    """
    if df is None or df.empty:
        st.info("Sem dados.")
        return None

    display = df.copy().reset_index(drop=True)

    styler = display.style.apply(_row_border_for_attractive, axis=1)

    formatters = {}
    if "Preço atual" in display.columns:
        formatters["Preço atual"] = lambda x: _fmt_money(x)
    if "Alvo validado 12m" in display.columns:
        formatters["Alvo validado 12m"] = lambda x: _fmt_money(x)
    if "Upside/Downside" in display.columns:
        formatters["Upside/Downside"] = lambda x: _fmt_percent(x)

    if formatters:
        styler = styler.format(formatters)

    event = st.dataframe(
        styler,
        width="stretch",
        hide_index=True,
        height=height,
        on_select="rerun",
        selection_mode="single-row",
        key=key,
    )

    rows = getattr(getattr(event, "selection", None), "rows", None)
    if not rows:
        return None

    try:
        selected_row = int(rows[0])
        return str(display.iloc[selected_row]["Ativo"])
    except Exception:
        return None



def render_target_projection_chart(radar_df: pd.DataFrame, symbol: str):
    """
    Exibe uma projeção VISUAL do preço atual até o alvo oficial de 12 meses.

    A linha intermediária é uma interpolação linear entre os dois pontos já
    calculados pelo Radar. Ela NÃO é uma previsão mensal, não altera o valuation
    e não cria novos preços-alvo.
    """
    if not symbol or radar_df is None or radar_df.empty:
        return

    current = radar_df[radar_df["Ativo"].astype(str) == str(symbol)]
    if current.empty:
        return

    row = current.iloc[0]
    price, target, upside_ratio = _valuation_numbers(row)

    st.subheader("Projeção visual até o preço-alvo de 12 meses")

    if price is None or target is None or price <= 0:
        st.info(
            "Este ativo não possui preço atual e alvo final validado suficientes "
            "para montar a projeção visual. Para ITSA4, NAV/SOTP continua pendente."
        )
        return

    months = list(range(13))
    projected = [
        price + (target - price) * (month / 12.0)
        for month in months
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=months,
            y=projected,
            mode="lines+markers",
            name="Trajetória linear de referência",
            hovertemplate="Mês %{x}<br>R$ %{y:.2f}<extra></extra>",
        )
    )

    fig.add_hline(
        y=price,
        line_dash="dot",
        annotation_text=f"Preço atual: {_fmt_money(price)}",
        annotation_position="bottom right",
    )
    fig.add_hline(
        y=target,
        line_dash="dash",
        annotation_text=f"Alvo 12m: {_fmt_money(target)}",
        annotation_position="top right",
    )

    fig.update_layout(
        title=f"{symbol} — preço atual → alvo final validado em 12 meses",
        xaxis_title="Meses a partir de hoje",
        yaxis_title="R$ por ação",
        height=440,
        margin=dict(l=20, r=20, t=70, b=30),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.10, x=0),
    )
    fig.update_xaxes(dtick=1, range=[0, 12])

    st.plotly_chart(
        fig,
        width="stretch",
        key=f"target_projection_{symbol}",
    )

    change_txt = _fmt_percent(upside_ratio) if upside_ratio is not None else "n/d"
    st.caption(
        f"Preço atual {_fmt_money(price)} → alvo final 12m {_fmt_money(target)} "
        f"({change_txt}). A trajetória entre hoje e o mês 12 é apenas uma "
        "interpolação linear para visualização; não representa previsão mensal "
        "de cotação nem altera o valuation."
    )



def render_decision_card(snapshot, radar_df: pd.DataFrame, symbol: str):
    """Resumo executivo: qualidade, valuation conclusivo e confiança dos métodos."""
    if not symbol or radar_df is None or radar_df.empty:
        return

    current = radar_df[radar_df["Ativo"].astype(str) == str(symbol)]
    if current.empty:
        return

    row = current.iloc[0]
    detail = _decision_lookup(snapshot).get(str(symbol), {})

    quality_answer = _quality_answer(row, detail)
    price_answer = _conclusive_valuation(row)
    conclusion = _portfolio_conclusion(row, detail)
    quality_reason = _quality_reason(row, detail)
    valuation_reason = _valuation_reason(row, detail)
    action = _what_to_do(row, detail)

    price, target, upside_ratio = _valuation_numbers(row)

    asset_data = snapshot.get("assets", {}).get(symbol, {})
    name = asset_data.get("name", symbol)

    st.markdown(f"### {symbol} — {name}")

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Quality Score", _fmt_score(row.get("Quality Score")))
    with m2:
        st.metric("Preço atual", _fmt_money(price))
    with m3:
        st.metric("Alvo validado 12m", _fmt_money(target))
    with m4:
        st.metric(
            "Upside/Downside",
            _fmt_percent(upside_ratio) if upside_ratio is not None else "n/d",
        )

    with st.container(border=True):
        st.markdown("#### 1. É uma empresa forte para carteira?")
        st.markdown(f"**{quality_answer}**")
        st.write(quality_reason)

    with st.container(border=True):
        st.markdown("#### 2. Valuation final: atrativo ou caro?")
        st.markdown(f"### {price_answer}")
        st.write(valuation_reason)
        st.caption(
            "O alvo final oficial é conclusivo quando existe preço e alvo válidos. "
            "Divergência entre DCF/RI e métodos secundários afeta a confiança, não transforma o alvo final em 'inconclusivo'."
        )

    with st.container(border=True):
        st.markdown("#### 3. Conclusão do Radar para carteira")
        st.markdown(f"### {conclusion}")
        st.write(action)

    st.caption(
        "A interface não cria novo preço-alvo, Quality Score, peso ou margem de segurança. "
        "Ela usa o alvo final validado do motor e mantém a divergência dos métodos como auditoria de confiança."
    )



def render_decision_legend():
    with st.expander("Como interpretar o valuation conclusivo"):
        st.markdown(
            """
            **O valuation final agora é sempre conclusivo quando há preço atual e alvo final validado.**

            - **ATRATIVO** = alvo final validado de 12 meses acima do preço atual.
            - **CARO** = alvo final validado de 12 meses abaixo do preço atual.
            - **NO PREÇO JUSTO** = alvo final igual ao preço atual.
            - **N/D** = não existe alvo validado adequado; ITSA4 continua exigindo NAV/SOTP.

            **Por que antes aparecia "INCONCLUSIVO"?**  
            Porque o painel estava usando a divergência entre os quatro métodos como se fosse a conclusão do valuation. Agora essa divergência permanece apenas como **Auditoria dos métodos / Confiança**. O alvo final oficial continua sendo o veredito econômico do motor.

            Exemplo: se o alvo final está acima da cotação, o painel mostra **ATRATIVO**, mesmo que DCF/RI e múltiplos discordem. Nesse caso a confiança pode ser 1/4, 2/4 ou 3/4 — mas o valuation final deixa de ser chamado de inconclusivo.

            **Qualidade continua separada do preço.** `Excelente/Forte no grupo` permanece a classificação relativa já calculada pelo Quality Score.
            """
        )


def render_conclusive_summary_cards(radar_df: pd.DataFrame):
    """Resumo superior coerente com o valuation conclusivo do alvo final."""
    if radar_df is None or radar_df.empty:
        return

    strong_attractive = 0
    strong_expensive = 0
    other_attractive = 0
    other_expensive = 0
    no_target = 0

    for _, row in radar_df.iterrows():
        quality_ok = _quality_is_approved(row)
        direction = _valuation_direction(row)

        if direction == "nd":
            no_target += 1
        elif quality_ok and direction in {"atrativo", "justo"}:
            strong_attractive += 1
        elif quality_ok and direction == "caro":
            strong_expensive += 1
        elif (not quality_ok) and direction in {"atrativo", "justo"}:
            other_attractive += 1
        elif (not quality_ok) and direction == "caro":
            other_expensive += 1

    cols = st.columns(5)
    cards = [
        ("⭐ Qualidade aprovada + valuation atrativo", strong_attractive),
        ("🟡 Qualidade aprovada + valuation caro", strong_expensive),
        ("🔎 Qualidade não prioritária + valuation atrativo", other_attractive),
        ("🔴 Qualidade não prioritária + valuation caro", other_expensive),
        ("⚪ Sem alvo validado", no_target),
    ]

    for col, (label, value) in zip(cols, cards):
        with col:
            st.metric(label, value)



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

render_conclusive_summary_cards(radar_df)
with st.expander("Ver categorias originais do motor"):
    st.caption("Categorias originais preservadas para auditoria. Elas podem marcar divergência de métodos como inconclusiva; a visão principal usa o alvo final validado como conclusão do valuation.")
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
        "A tabela usa o alvo final validado para dar um valuation conclusivo: ATRATIVO ou CARO. "
        "A divergência dos quatro métodos aparece separadamente como confiança/auditoria. Clique em uma linha para ver os motivos."
    )

    render_decision_legend()
    st.caption("🟢 Borda verde = preço atual abaixo do alvo final validado de 12 meses.")

    selected = render_highlighted_selectable_table(clear_df, key="radar_main_clear")
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
            picked = render_highlighted_selectable_table(
                candidates,
                key="candidate_table_clear",
                height=320,
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
            # Enriquece a watchlist apenas com colunas que REALMENTE existem
            # na visão executiva atual. Isso evita KeyError caso a interface
            # evolua e algum nome de coluna seja alterado.
            desired_columns = [
                "Ativo",
                "Qualidade para carteira",
                "Valuation final",
                "Confiança",
                "Auditoria dos métodos",
                "Conclusão para carteira",
            ]
            available_columns = [
                col for col in desired_columns
                if col in clear_df.columns
            ]

            # "Ativo" é obrigatório para o merge; as demais são opcionais.
            if "Ativo" in available_columns:
                enrich = clear_df[available_columns].copy()
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
    render_target_projection_chart(radar_df, symbol)

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
        - se o preço está ATRATIVO ou CARO pelo alvo final validado;
        - por que os quatro métodos podem divergir e qual a confiança dessa conclusão;
        - qual é a conclusão atual para carteira;
        - quais números sustentam essa leitura.

        **3. `Forte/Excelente no grupo` não significa automaticamente qualidade absoluta excepcional.**  
        A classificação continua sendo relativa ao grupo econômico do universo W1. Por isso a interface mostra separadamente o **Quality Score numérico** e a **Classe no grupo**.

        **4. Qualidade e preço são perguntas diferentes.**  
        Uma empresa pode ter ótima qualidade e estar cara. A divergência entre os quatro métodos não torna mais o valuation final inconclusivo: ela reduz a confiança. O veredito final vem do alvo validado de 12 meses comparado ao preço atual.

        **5. Nenhuma nova fórmula foi criada no `app.py`.**  
        A interface não recalcula DCF, RI, P/L, P/VP, EV/EBITDA, Dividend Yield, WACC, Ke, Quality Score, Confidence Score ou preço-alvo.

        **6. Métodos de valuation preservados.**  
        Para companhias não financeiras, o slot principal é DCF/FCFF ou RI setorial conforme a governança do próprio motor; os demais são P/L, EV/EBITDA e Dividend Yield. Para instituições financeiras, o motor usa RI, P/L, P/VP e Dividend Yield. Os pesos continuam **50% / 20% / 20% / 10%**.

        **7. ITSA4 permanece especial.**  
        O painel não transforma a proxy contábil da holding em preço-alvo validado; NAV/SOTP continua pendente.

        **8. Gráfico histórico.**  
        Candles e volume são carregados separadamente apenas para visualização. Eles não alteram Quality Score, valuation, confiança ou Status de Carteira.

        **9. Projeção visual do alvo de 12 meses.**  
        A aba **Detalhar ativo** mostra também uma linha entre o preço atual e o alvo final validado de 12 meses. Os pontos intermediários são apenas interpolação linear para visualização; não são previsão mensal e não alteram o valuation.

        **10. Borda verde na tabela.**  
        Uma linha recebe borda verde quando o **preço atual está abaixo do alvo final validado de 12 meses**. É apenas destaque visual do mesmo critério usado pelo valuation conclusivo; não cria nova regra de entrada.
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
