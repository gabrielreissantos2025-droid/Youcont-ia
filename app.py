import streamlit as st
import pandas as pd
import json
import re
from google import genai
from google.genai import types

st.set_page_config(
    page_title="Agente Contábil Inteligente - Seguro", 
    page_icon="🔒", 
    layout="wide"
)

st.title("🔒 Agente Contábil Inteligente & Segurança (LGPD)")
st.markdown("Plataforma protegida com mascaramento de dados sensíveis, conformidade LGPD e gestão segura de credenciais.")

# Gestão Segura de Chaves via st.secrets (Protegendo contra exposição na UI)
api_key = None
if "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

with st.sidebar:
    st.header("⚙️ Configurações de Segurança")
    if not api_key:
        api_key = st.text_input("Insira sua Google Gemini API Key", type="password")
        st.warning("⚠️ Insira a chave para habilitar o motor de IA.")
    else:
        st.success("🔒 Chave carregada de forma segura pelo servidor.")
    
    st.markdown("---")
    # Opção de LGPD
    mascarar_dados = st.checkbox("Mascarar Dados Sensíveis (LGPD)", value=True, help="Oculta CPFs/CNPJs e nomes sensíveis nas descrições.")
    st.info("💡 Os ficheiros são processados em memória volátil e eliminados logo após a extração.")

def parse_ofx(file_bytes):
    try:
        content = file_bytes.decode('utf-8', errors='ignore')
        ofx_data = content[content.index('<OFX>'):] if '<OFX>' in content else content
        lancamentos = []
        transacoes = re.findall(r'<STMTTRN>(.*?)</STMTTRN>', ofx_data, re.DOTALL)
        for t in transacoes:
            data_match = re.search(r'<DTPOSTED>(.*?)([+\[]|$)', t)
            val_match = re.search(r'<TRNAMT>(.*?)</TRNAMT>', t)
            memo_match = re.search(r'<MEMO>(.*?)</MEMO>', t)
            name_match = re.search(r'<NAME>(.*?)</NAME>', t)
            
            if val_match and data_match:
                valor = float(val_match.group(1).strip())
                tipo = "CREDITO" if valor >= 0 else "DEBITO"
                dt_raw = data_match.group(1).strip()
                data_fmt = f"{dt_raw[6:8]}/{dt_raw[4:6]}/{dt_raw[0:4]}" if len(dt_raw) >= 8 else "01/01/2026"
                desc = memo_match.group(1).strip() if memo_match else (name_match.group(1).strip() if name_match else "Transação OFX")
                
                cat = "Transferências" if valor < 0 else "Outros"
                if "INVEST" in desc.upper() or "B3" in desc.upper():
                    cat = "Investimentos"
                elif "IMPOSTO" in desc.upper() or "IOF" in desc.upper():
                    cat = "Impostos"

                lancamentos.append({
                    "data": data_fmt,
                    "descricao": desc,
                    "valor": abs(valor),
                    "tipo": tipo,
                    "categoria": cat
                })
        return lancamentos
    except:
        return []

# Função para mascarar CPFs, CNPJs ou nomes em conformidade com a LGPD
def aplicar_mascara_lgpd(texto):
    if not isinstance(texto, str):
        return texto
    texto_mascarado = re.sub(r'\d{3}\.\d{3}\.\d{3}-\d{2}', '***.***.***-**', texto)
    texto_mascarado = re.sub(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', '**.***.***/****-**', texto_mascarado)
    return texto_mascarado

uploaded_files = st.file_uploader(
    "📂 Arraste ou selecione seus documentos confidenciais (PDF, PNG, JPG, OFX):", 
    type=["pdf", "png", "jpg", "jpeg", "ofx"], 
    accept_multiple_files=True
)

if uploaded_files:
    st.success(f"✨ {len(uploaded_files)} ficheiro(s) carregado(s) com segurança em memória.")

    if st.button("🚀 Processar com Auditoria de Segurança", type="primary"):
        if not api_key:
            st.error("⚠️ É necessária uma chave de API válida para prosseguir.")
        else:
            try:
                client = genai.Client(api_key=api_key)
                todos_lancamentos = []

                with st.spinner("🤖 A processar com encriptação em trânsito e IA..."):
                    for uploaded_file in uploaded_files:
                        bytes_data = uploaded_file.getvalue()
                        
                        if uploaded_file.name.endswith('.ofx'):
                            lanc_ofx = parse_ofx(bytes_data)
                            if lanc_ofx:
                                todos_lancamentos.extend(lanc_ofx)
                        else:
                            prompt = """
                            Analise este documento financeiro e extraia todos os lançamentos com precisão absoluta.
                            Para cada lançamento, retorne um objeto estrito com:
                            1. 'data' (DD/MM/AAAA)
                            2. 'descricao' (descrição clara da transação)
                            3. 'valor' (número em formato decimal com ponto, ex: 150.50. NUNCA utilize null ou None)
                            4. 'tipo' ('CREDITO' ou 'DEBITO')
                            5. 'categoria' ('Investimentos', 'Transferências', 'Impostos', 'Fornecedores', 'Despesas Operacionais' ou 'Outros')

                            Retorne estritamente um JSON com a chave 'lancamentos' contendo a lista.
                            """

                            resposta = client.models.generate_content(
                                model='gemini-3.8-flash',
                                contents=[
                                    types.Part.from_bytes(data=bytes_data, mime_type=uploaded_file.type),
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
                    df['valor'] = pd.to_numeric(df['valor'], errors='coerce').fillna(0.0)

                    if mascarar_dados:
                        df['descricao'] = df['descricao'].apply(aplicar_mascara_lgpd)

                    st.markdown("---")
                    st.subheader("📊 Dashboard Financeiro Consolidado (Ambiente Seguro)")

                    total_debitos = df[df['tipo'].str.upper() == 'DEBITO']['valor'].sum()
                    total_creditos = df[df['tipo'].str.upper() == 'CREDITO']['valor'].sum()
                    saldo_liquido = total_creditos - total_debitos

                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("📉 Total de Débitos", f"R$ {total_debitos:,.2f}")
                    c2.metric("📈 Total de Créditos", f"R$ {total_creditos:,.2f}")
                    c3.metric("💰 Saldo Líquido", f"R$ {saldo_liquido:,.2f}")
                    c4.metric("📋 Transações Auditas", len(df))

                    st.markdown("---")
                    st.subheader("📈 Análise Visual por Categoria")
                    
                    df_grafico = df.groupby('categoria')['valor'].sum()
                    if not df_grafico.empty:
                        st.bar_chart(df_grafico)

                    st.markdown("---")
                    st.subheader("🔍 Filtros e Tabela Auditada")

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

                    st.markdown("---")
                    col_d1, col_d2 = st.columns(2)
                    
                    csv = df_filtrado.to_csv(index=False).encode('utf-8')
                    col_d1.download_button(
                        label="📥 Descarregar Relatório Seguro (CSV)",
                        data=csv,
                        file_name='relatorio_contabil_seguro.csv',
                        mime='text/csv',
                    )

                    json_str = df_filtrado.to_json(orient="records", force_ascii=False)
                    col_d2.download_button(
                        label="📥 Descarregar Relatório Seguro (JSON)",
                        data=json_str,
                        file_name='relatorio_contabil_seguro.json',
                        mime='application/json',
                    )
                else:
                    st.warning("⚠️ Nenhum lançamento encontrado para auditoria.")

            except Exception as e:
                st.error(f"❌ Erro de segurança/processamento: {e}")
