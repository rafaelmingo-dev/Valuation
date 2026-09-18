# Radar W1 — Qualidade × Valuation

Painel Streamlit para o motor de valuation Radar W1.

## Arquitetura

- `valuation_engine.py` — motor completo original, preservado sem reescrever fórmulas.
- `radar_runner.py` — executa o motor em processo isolado e grava um snapshot somente após sucesso integral.
- `radar_store.py` — serialização/leitura do snapshot.
- `radar_ui.py` — componentes visuais, tabela, detalhe do ativo e gráfico candlestick + volume.
- `app.py` — aplicativo Streamlit.
- `.streamlit/config.toml` — tema escuro.
- `requirements.txt` — dependências.

O Streamlit **não recalcula valuation ao navegar entre abas**. O cálculo só ocorre quando o botão `🔄 ATUALIZAR RADAR` é pressionado.

## Subir no GitHub

Crie um repositório e envie todos os arquivos e pastas mantendo exatamente esta estrutura:

```text
app.py
valuation_engine.py
radar_runner.py
radar_store.py
radar_ui.py
requirements.txt
README.md
.gitignore
.streamlit/
  config.toml
data/
  .gitkeep
```

## Criar o aplicativo no Streamlit Community Cloud

1. Entre no Streamlit Community Cloud.
2. Escolha **Create app / New app**.
3. Selecione o repositório GitHub e a branch `main`.
4. Main file path: `app.py`.
5. Recomenda-se Python 3.12.
6. Faça o deploy.
7. Na primeira abertura, clique em **🔄 ATUALIZAR RADAR**.

Não há segredo/API key obrigatório no projeto atual.

## Fluxo de atualização

1. `app.py` chama `radar_runner.py`.
2. O runner executa o `valuation_engine.py` integralmente.
3. Se os 20 ativos forem processados, cria `data/latest_snapshot.json.gz` de forma atômica.
4. O painel recarrega o snapshot.
5. Se houver erro de CVM/Yahoo/ANBIMA, o snapshot anterior é preservado e o erro fica em `data/latest_run.log`.

## Abas

### Radar W1
Visão rápida com qualidade, preço, alvo, upside/downside, valuation, confiança e status. A linha pode ser selecionada para abrir o gráfico do ativo.

### Candidatos
Somente ativos que o motor classificou como `⭐ CANDIDATO PRIORITÁRIO`.

### Watchlist de qualidade
Empresas Forte/Excelente no grupo, independentemente do preço atual.

### Detalhar ativo
- qualidade e evidências;
- preço atual e alvo 12m;
- conclusão do Radar;
- gráfico candlestick + volume;
- períodos 3M, 6M, 1A, 2A e 5A;
- linha do preço atual;
- linha do alvo final 12m;
- opção de mostrar os quatro métodos de valuation;
- histórico fundamental e componentes do Quality Score.

### Métodos
Auditoria do método principal contra os três secundários.

### Como funciona
Resumo da metodologia já utilizada pelo motor.

## Importante

O painel é uma camada de apresentação. Ele não altera:

- DCF/FCFF;
- Residual Income;
- Ke/WACC;
- crescimento terminal;
- payout;
- múltiplos;
- Quality Score;
- Confidence Score;
- pesos 50/20/20/10;
- Status de Carteira.

Candles e volume são apenas apoio visual e não participam da classificação dos ativos.
