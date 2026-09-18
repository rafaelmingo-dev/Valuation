from __future__ import annotations

import json
import subprocess
import sys
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


st.title("RADAR W1 — QUALIDADE × VALUATION")
st.markdown(
    '<div class="radar-subtitle">Carteira de longo prazo • Qualidade • Valuation • Confiança • Seleção de ativos</div>',
    unsafe_allow_html=True,
)

snapshot = load_snapshot()

c1, c2 = st.columns([1.35, 4.65], vertical_alignment="center")
with c1:
    if st.button("🔄 ATUALIZAR RADAR", type="primary", use_container_width=True):
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

radar_df = main_table(snapshot)
render_summary_cards(radar_df)
st.write("")

tab_radar, tab_candidates, tab_watch, tab_detail, tab_methods, tab_help = st.tabs(
    ["Radar W1", "Candidatos", "Watchlist de qualidade", "Detalhar ativo", "Métodos", "Como funciona"]
)

with tab_radar:
    st.subheader("Radar W1 — visão rápida")
    st.caption("Clique em uma linha para abrir imediatamente o ativo e seu gráfico abaixo da tabela.")
    selected = render_selectable_table(radar_df, key="radar_main")
    if selected:
        st.session_state["selected_asset"] = selected
    active = st.session_state.get("selected_asset")
    if active:
        st.divider()
        render_asset_detail(snapshot, active, compact=True)

with tab_candidates:
    st.subheader("⭐ Candidatos prioritários")
    if radar_df.empty:
        st.info("Sem dados.")
    else:
        mask = radar_df["Status de Carteira"].astype(str).str.contains("CANDIDATO PRIORITÁRIO", regex=False)
        candidates = radar_df[mask].copy()
        if candidates.empty:
            st.info("Nenhum ativo está nesta categoria na atualização atual.")
        else:
            picked = render_selectable_table(candidates, key="candidate_table")
            if picked:
                st.session_state["selected_asset"] = picked
                st.divider()
                render_asset_detail(snapshot, picked, compact=False)

with tab_watch:
    st.subheader("Watchlist de qualidade")
    st.caption("Esta aba acompanha empresas classificadas como Forte/Excelente no grupo, independentemente do momento de entrada.")
    watch = snapshot.get("tables", {}).get("EXECUTIVE_STRONG_WATCHLIST")
    if isinstance(watch, pd.DataFrame) and not watch.empty:
        w = watch.copy()
        if "Ativo" not in w.columns:
            idx = w.index.name or "index"
            w = w.reset_index().rename(columns={idx: "Ativo", "index": "Ativo"})
        st.dataframe(w, use_container_width=True, hide_index=True, height=650)
    else:
        st.info("Watchlist indisponível.")

with tab_detail:
    st.subheader("Detalhar ativo")
    order = [s for s in snapshot.get("asset_order", []) if s in snapshot.get("assets", {})]
    default_symbol = st.session_state.get("selected_asset")
    default_index = order.index(default_symbol) if default_symbol in order else 0
    symbol = st.selectbox(
        "Ativo",
        order,
        index=default_index,
        format_func=lambda s: f"{s} — {snapshot.get('assets', {}).get(s, {}).get('name', s)}",
        key="detail_asset_select",
    )
    st.session_state["selected_asset"] = symbol
    render_asset_detail(snapshot, symbol, compact=False)

with tab_methods:
    st.subheader("Métodos de valuation — comparação 4/4")
    st.caption("Auditoria diagnóstica do método principal de 50% contra os três métodos secundários; não recalcula o valuation.")
    render_method_audit(snapshot)

with tab_help:
    st.subheader("Como funciona")
    st.markdown(
        """
        **1. O motor continua separado da interface.** O botão **Atualizar Radar** executa `valuation_engine.py` integralmente em outro processo. A interface lê apenas o snapshot final.

        **2. Qualidade e preço são perguntas diferentes.** O Quality Score é relativo ao grupo econômico do universo W1. O valuation continua sendo calculado pelos métodos já definidos no motor.

        **3. Métodos de valuation preservados.** Para companhias não financeiras, o slot principal é DCF/FCFF ou RI setorial conforme a governança do próprio motor; os demais são P/L, EV/EBITDA e Dividend Yield. Para instituições financeiras, o motor usa RI, P/L, P/VP e Dividend Yield. Os pesos continuam 50% / 20% / 20% / 10%.

        **4. ITSA4 permanece especial.** O painel não transforma a proxy contábil da holding em preço-alvo validado; NAV/SOTP continua pendente.

        **5. Gráfico.** Candles e volume são carregados separadamente apenas para visualização. Eles não alteram Quality Score, valuation, confiança ou Status de Carteira.
        """
    )

with st.expander("Log da última atualização"):
    if LOG_PATH.exists():
        st.code(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-16000:])
    else:
        st.write("Nenhum log disponível.")
