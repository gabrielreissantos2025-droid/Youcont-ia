import streamlit as st
import pandas as pd
import json
import xml.etree.ElementTree as ET
from google import genai
from google.genai import types

# Configuração da página com layout moderno
st.set_page_config(
    page_title="Agente Contábil Inteligente", 
    page_icon="💼", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# Estilização CSS personalizada para um aspeto mais moderno e limpo
st.markdown("""
    <style>
    .main {
        background-color: #0e1117;
    }
    .stMetric {
        background-color: #161b22;
        padding: 15px;
        border-radius: 10px;
        border: 1px solid #30363d;
    }
    .report-box {
        background-color: #161b22;
        padding: 20px;
        border-radius: 10px;
        border-left: 5px solid #238636;
        margin-bottom: 20px;
    }
    </style>
""", unsafe_allow_html=True)

st.title("💼 Agente Contábil Inteligente & Dashboard")
st.markdown("Plataforma avançada para conciliação bancária, extração inteligente de documentos e análise financeira para PMEs.")

# Sidebar para a chave da API e navegação
with st.sidebar:
    st.header("⚙️ Configurações")
    api_key = st.text_input("Insira sua Google Gemini API Key", type="password")
    st.markdown("---")
    st.info("💡 Dica: Suporta múltiplos extratos em PDF, Imagens (PNG/JPG) e ficheiros bancários OFX.")

# Função para processar ficheiros OFX de forma nativa
def parse_ofx(file_bytes):
    try:
        content = file_bytes.decode('utf-8', errors='ignore')
        # Limpar cabeçalhos SGML do OFX se necessário
        if '<OFX>' in content:
            ofx_data = content[content.index('<OFX>'):]
        else:
            ofx_data = content

        # Extração simples de transações via tags comuns de OFX
        lancamentos = []
        import re
        
        # Padrões comuns em OFX (STMTTRN)
        transacoes = re.findall(r'<STMTTRN>(.*?)</STMTTRN>', ofx_data, re.DOTALL)
        for t in transacoes:
            tipo_match = re.search(r'<TRNTYPE>(.*?)</TRNTYPE>', t)
            data_match = re.search(r'<DTPOSTED>(.*?)([+\[]|$)', t)
            val_match = re.search(r'<TRNAMT>(.*?)</TRNAMT>', t)
            memo_match = re.search(r'<MEMO>(.*?)</MEMO>', t)
            name_match = re.search(r'<NAME>(.*?)</NAME>', t)
            
            if val_match and data_match:
                val_str = val_match.group(1).strip()
                valor = float(val_str)
                tipo = "CREDITO" if valor >= 0 else "DEBITO"
                
                # Tratar data (AAAAMMDD...)
                dt_raw = data_match.group(1).strip()
                data_fmt = f"{dt_raw[6:8]}/{dt_raw[4:6]}/{dt_raw[0:4]}" if len(dt_raw) >= 8 else "01/01/2026"
                
                desc = memo_match.group(1).strip() if memo_match else (name_match.group(1).strip() if name_match else "Transação OFX")
                
                # Categorização simples por regras ou IA posterior
                cat = "Transferências" if valor < 0 else "Outros"
                if "INVEST" in desc.upper() or "B3" in desc.upper():
                    cat = "Investimentos"
                elif "IMPOSTO" in desc.upper() or "IOF" in desc.upper() or "DARF" in desc.upper():
                    cat = "Impostos"

                lancamentos.append({
                    "data": data_fmt,
                    "descricao": desc,
                    "valor": abs(valor),
                    "tipo": tipo,
                    "categoria": cat
                })
        return lancamentos
    except Exception as e:
        return []

# Upload unificado (PDF, Imagens e OFX)
uploaded_files = st.file_uploader(
    "📂 Arraste ou selecione seus documentos (PDF, PNG, JPG, OFX):", 
    type=["pdf", "png", "jpg", "jpeg", "ofx"], 
    accept_multiple_files=True
)

if uploaded_files:
    st.success(f"✨ {len(uploaded_files)} arquivo(s) carregado(s) com sucesso.")

    if st.button("🚀 Processar e Consolidar Dados", type="primary"):
        if not api_key:
            st.error("⚠️ Por favor, insira sua chave da API do Gemini na barra lateral.")
        else:
            try:
                client = genai.Client(api_key=api_key)
                todos_lancamentos = []

                with st.spinner("🤖 A IA e o motor contábil estão a processar os dados..."):
                    for uploaded_file in uploaded_files:
                        bytes_data = uploaded_file.getvalue()
                        
                        # Se for OFX, processamos nativamente de forma instantânea
                        if uploaded_file.name.endswith('.ofx'):
                            lanc_ofx = parse_ofx(bytes_data)
                            if lanc_ofx:
                                todos_lancamentos.extend(lanc_ofx)
                        else:
                            # Se for PDF ou Imagem, usamos o Gemini
                            prompt = """
                            Você é um assistente contábil sênior especialista em conciliação bancária e PMEs.
                            Analise este documento financeiro e extraia todos os lançamentos financeiros encontrados.
                            Para cada lançamento, identifique com precisão:
                            1. 'data' (formato DD/MM/AAAA)
                            2. 'descricao' (descrição detalhada da transação)
                            3. 'valor' (número decimal puro obrigatório, ex: 150.50, jamais deixe null ou None se houver número visível)
                            4. 'tipo' (estritamente 'CREDITO' ou 'DEBITO')
                            5. 'categoria' (classifique obrigatoriamente em uma destas: 'Investimentos', 'Transferências', 'Impostos', 'Fornecedores', 'Despesas Operacionais' ou 'Outros')

                            Retorne estritamente um objeto JSON com uma chave chamada 'lancamentos' contendo uma lista de objetos com as chaves: data, descricao, valor, tipo, categoria.
                            """

                            resposta = client.models.generate_content(
                                model='gemini-3.8-flash',
                                contents=[
                                    types.Part.from_bytes(
                                        data=bytes_data,
                                        mime_type=uploaded_file.type,
                                    ),
                                    prompt
                                ],
                                config=types.GenerateContentConfig(
                                    response_mime_type="application/json"
                                ),
                            )

                            dados_json = json.loads(resposta.text)
                            if "lancamentos" in dados_json:
                                todos_lancamentos.extend(dados_json["lancamentos"])

                if todos_lancamentos:
                    df = pd.DataFrame(todos_lancamentos)
                    
                    # Garantir tratamento numérico rigoroso do campo valor
                    df['valor'] = pd.to_numeric(df['valor'], errors='coerce').fillna(0.0)

                    st.markdown("---")
                    st.subheader("📊 Dashboard Financeiro Consolidado")

                    total_debitos = df[df['tipo'].str.upper() == 'DEBITO']['valor'].sum()
                    total_creditos = df[df['tipo'].str.upper() == 'CREDITO']['valor'].sum()
                    saldo_liquido = total_creditos - total_debitos

                    # Cartões de Métricas Modernos
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("📉 Total de Débitos", f"R$ {total_debitos:,.2f}")
                    c2.metric("📈 Total de Créditos", f"R$ {total_creditos:,.2f}")
                    c3.metric("💰 Saldo Líquido", f"R$ {saldo_liquido:,.2f}", delta=f"R$ {saldo_liquido:,.2f}")
                    c4.metric("📋 Transações", len(df))

                    st.markdown("---")

                    # Relatório Executivo Automático via IA
                    with st.spinner("📑 A gerar Relatório Executivo Inteligente..."):
                        resumo_prompt = f"""
                        Com base nestes dados contábeis consolidados (Total Débitos: {total_debitos}, Total Créditos: {total_creditos}, Total de lançamentos: {len(df)}), 
                        escreva um breve relatório executivo em português (máximo de 3 parágrafos) destacando pontos de atenção financeira, saúde do fluxo de caixa e recomendações para a PME.
                        """
                        resp_relatorio = client.models.generate_content(
                            model='gemini-3.8-flash',
                            contents=resumo_prompt
                        )
                        relatorio_texto = resp_relatorio.text

                    st.markdown("### 📝 Relatório Executivo & Insights")
                    st.markdown(f"""
                        <div class="report-box">
                            {relatorio_texto}
                        </div>
                    """, unsafe_allow_html=True)

                    st.markdown("---")
                    st.subheader("📈 Análise Visual por Categoria")
                    
                    # Gráficos Dinâmicos
                    df_cat = df.groupby(['categoria', 'tipo'])['valor'].sum().unstack(fill_value=0)
                    if not df_cat.empty:
                        st.bar_chart(df_cat)

                    st.markdown("---")
                    st.subheader("🔍 Filtros Avançados e Tabela de Lançamentos")

                    # Filtros na Interface
                    col_f1, col_f2 = st.columns(2)
                    with col_f1:
                        filtro_tipo = st.selectbox("Filtrar por Tipo", ["Todos", "CREDITO", "DEBITO"])
                    with col_f2:
                        categorias_disponiveis = ["Todas"] + list(df['categoria'].unique())
                        filtro_cat = st.selectbox("Filtrar por Categoria", categorias_disponiveis)

                    df_filtrado = df.copy()
                    if filtro_tipo != "Todos":
                        df_filtrado = df_filtrado[df_filtrado['tipo'].str.upper() == filtro_tipo]
                    if filtro_cat != "Todas":
                        df_filtrado = df_filtrado[df_filtrado['categoria'] == filtro_cat]

                    st.dataframe(df_filtrado, use_container_width=True)

                    # Opções de Download Profissionais
                    st.markdown("---")
                    col_d1, col_d2 = st.columns(2)
                    
                    csv = df_filtrado.to_csv(index=False).encode('utf-8')
                    col_d1.download_button(
                        label="📥 Descarregar Relatório em CSV",
                        data=csv,
                        file_name='relatorio_contabil_consolidado.csv',
                        mime='text/csv',
                    )

                    json_str = df_filtrado.to_json(orient="records", force_ascii=False)
                    col_d2.download_button(
                        label="📥 Descarregar Relatório em JSON",
                        data=json_str,
                        file_name='relatorio_contabil_consolidado.json',
                        mime='application/json',
                    )
                else:
                    st.warning("⚠️ Nenhum lançamento foi encontrado nos documentos enviados.")

            except Exception as e:
                st.error(f"❌ Ocorreu um erro ao processar os arquivos: {e}")
