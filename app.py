import streamlit as st
import pandas as pd
import json
from google import genai
from google.genai import types

st.set_page_config(page_title="Agente Contábil Inteligente", page_icon="📊", layout="wide")

st.title("📊 Agente Contábil Inteligente")
st.markdown("Faça upload de múltiplos extratos (PDF) ou comprovantes (PNG/JPG). A IA irá extrair, categorizar e consolidar todos os lançamentos financeiros automaticamente.")

# Sidebar para a chave da API
with st.sidebar:
    st.header("Configurações")
    api_key = st.text_input("Insira sua Google Gemini API Key", type="password")
    st.markdown("---")
    st.info("Obtenha sua chave gratuitamente no Google AI Studio.")

# Permitir múltiplos arquivos
uploaded_files = st.file_uploader("Arraste ou selecione seus arquivos aqui:", type=["pdf", "png", "jpg", "jpeg"], accept_multiple_files=True)

if uploaded_files:
    st.success(f"{len(uploaded_files)} arquivo(s) pronto(s) para análise.")

    if st.button("🚀 Processar Documentos"):
        if not api_key:
            st.error("Por favor, insira sua chave da API do Gemini na barra lateral.")
        else:
            try:
                client = genai.Client(api_key=api_key)
                todos_lancamentos = []

                with st.spinner("A processar e a categorizar documentos com IA..."):
                    for uploaded_file in uploaded_files:
                        bytes_data = uploaded_file.getvalue()
                        
                        prompt = """
                        Você é um assistente de contabilidade especialista em conciliação bancária e PMEs.
                        Analise este documento financeiro e extraia todos os lançamentos financeiros encontrados.
                        Para cada lançamento, identifique:
                        1. 'Data' (formato DD/MM/AAAA)
                        2. 'Descricao' (descrição detalhada da transação)
                        3. 'Valor' (número decimal puro, ex: 150.50, sem símbolos de moeda)
                        4. 'Tipo' (exatamente 'CREDITO' ou 'DEBITO')
                        5. 'Categoria' (classifique em uma destas categorias: 'Investimentos', 'Transferências', 'Impostos', 'Fornecedores', 'Despesas Operacionais' ou 'Outros')

                        Retorne estritamente um objeto JSON com uma chave chamada 'lancamentos' contendo uma lista de objetos com as chaves: data, descricao, valor, tipo, categoria.
                        """

                        resposta = client.models.generate_content(
                            model='gemini-2.5-flash',
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
                    
                    # Garantir que o valor é numérico
                    df['valor'] = pd.to_numeric(df['valor'], errors='coerce')

                    st.markdown("---")
                    st.subheader("📋 Resumo Consolidado")

                    total_debitos = df[df['tipo'].str.upper() == 'DEBITO']['valor'].sum()
                    total_creditos = df[df['tipo'].str.upper() == 'CREDITO']['valor'].sum()

                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total de Débitos", f"R$ {total_debitos:,.2f}")
                    col2.metric("Total de Créditos", f"R$ {total_creditos:,.2f}")
                    col3.metric("Lançamentos Encontrados", len(df))

                    st.markdown("---")
                    st.subheader("🔍 Filtros e Visualização")

                    # Filtros interativos
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

                    # Opções de Download
                    st.markdown("---")
                    col_d1, col_d2 = st.columns(2)
                    
                    csv = df_filtrado.to_csv(index=False).encode('utf-8')
                    col_d1.download_button(
                        label="📥 Baixar Filtrado em CSV (Excel)",
                        data=csv,
                        file_name='lancamentos_consolidados.csv',
                        mime='text/csv',
                    )

                    json_str = df_filtrado.to_json(orient="records", force_ascii=False)
                    col_d2.download_button(
                        label="📥 Baixar Filtrado em JSON",
                        data=json_str,
                        file_name='lancamentos_consolidados.json',
                        mime='application/json',
                    )
                else:
                    st.warning("Nenhum lançamento foi encontrado nos documentos enviados.")

            except Exception as e:
                st.error(f"Ocorreu um erro ao processar os arquivos: {e}")
                    
