import streamlit as st
import pandas as pd
import json
import re
from datetime import date
from typing import Literal
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

# Configuração da página
st.set_page_config(
    page_title="Agente Contábil Profissional - Produção", 
    page_icon="💼", 
    layout="wide"
)

st.title("💼 Agente Contábil Profissional & Auditoria (B2B)")
st.markdown("Plataforma contábil com validação rigorosa (Pydantic), integridade matemática de saldos e conformidade de segurança.")

# Configuração segura de credenciais via st.secrets (Obrigatório em produção)
api_key = None
if "GEMINI_API_KEY" in st.secrets:
    api_key = st.secrets["GEMINI_API_KEY"]

with st.sidebar:
    st.header("⚙️ Configurações & Governança")
    if not api_key:
        api_key = st.text_input("Insira sua Google Gemini API Key", type="password")
        st.warning("⚠️ Insira a chave no st.secrets ou aqui para iniciar.")
    else:
        st.success("🔒 Credencial carregada via st.secrets com segurança.")
    
    st.markdown("---")
    aviso_lgpd = st.checkbox("Concordo com o envio de documentos para análise externa via IA", value=True)
    mascarar_dados = st.checkbox("Mascarar Dados Sensíveis (LGPD - Pós-extração)", value=True)
    st.info("ℹ️ Os documentos são processados em memória volátil e descartados após o encerramento.")

# Definição do Esquema Rigoroso com Pydantic para validação da IA
class LancamentoSchema(BaseModel):
    data: str = Field(..., description="Data no formato DD/MM/AAAA")
    descricao: str = Field(..., description="Descrição detalhada da transação financeira")
    valor: float = Field(..., description="Valor absoluto numérico da transação (positivo)")
    tipo: Literal["CREDITO", "DEBITO"] = Field(..., description="Tipo da transação: CREDITO ou DEBITO")
    categoria: Literal[
        "Investimentos", 
        "Transferências", 
        "Impostos", 
        "Fornecedores", 
        "Despesas Operacionais", 
        "Outros"
    ] = Field(..., description="Categoria contábil normalizada")

class RelatorioContabilSchema(BaseModel):
    lancamentos: list[LancamentoSchema]

# Parser OFX Robusto e com Tratamento de Erros
def parse_ofx(file_bytes):
    try:
        content = file_bytes.decode('utf-8', errors='ignore')
        ofx_data = content[content.index('<OFX>'):] if '<OFX>' in content else content
        lancamentos = []
        transacoes = re.findall(r'<STMTTRN>(.*?)</STMTTRN>', ofx_data, re.DOTALL)
        
        for t in transacoes:
            try:
                data_match = re.search(r'<DTPOSTED>(.*?)([+\[]|$)', t)
                val_match = re.search(r'<TRNAMT>(.*?)</TRNAMT>', t)
                memo_match = re.search(r'<MEMO>(.*?)</MEMO>', t)
                name_match = re.search(r'<NAME>(.*?)</NAME>', t)
                
                if val_match and data_match:
                    # Substituir vírgula por ponto para conversão decimal segura
                    val_str = val_match.group(1).strip().replace(',', '.')
                    valor = float(val_str)
                    tipo = "CREDITO" if valor >= 0 else "DEBITO"
                    
                    dt_raw = data_match.group(1).strip()
                    data_fmt = f"{dt_raw[6:8]}/{dt_raw[4:6]}/{dt_raw[0:4]}" if len(dt_raw) >= 8 else date.today().strftime("%d/%m/%Y")
                    
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
            except Exception:
                continue # Ignora transações mal formatadas isoladamente
        return lancamentos
    except Exception as e:
        st.error(Ocorreu um erro crítico ao ler o ficheiro OFX: {e})
        return []

# Função de mascaramento LGPD melhorada
def aplicar_mascara_lgpd(texto):
    if not isinstance(texto, str):
        return texto
    # Mascarar CPFs e CNPJs com ou sem pontuação básica
    texto_mascarado = re.sub(r'\d{3}\.\d{3}\.\d{3}-\d{2}', '***.***.***-**', texto)
    texto_mascarado = re.sub(r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', '**.***.***/****-**', texto_mascarado)
    texto_mascarado = re.sub(r'\b\d{11}\b', '***********', texto_mascarado) # CPF cru
    return texto_mascarado

uploaded_files = st.file_uploader(
    "📂 Selecione os extratos e comprovantes (PDF, Imagens, OFX):", 
    type=["pdf", "png", "jpg", "jpeg", "ofx"], 
    accept_multiple_files=True
)

if uploaded_files:
    if not aviso_lgpd:
        st.warning("⚠️ Deve concordar com os termos de privacidade na barra lateral para prosseguir com o processamento.")
    else:
        st.success(f"✨ {len(uploaded_files)} ficheiro(s) carregado(s) com sucesso em memória volátil.")

        if st.button("🚀 Executar Auditoria e Processamento Contábil", type="primary"):
            if not api_key:
                st.error("⚠️ Chave da API do Gemini não configurada.")
            else:
                try:
                    client = genai.Client(api_key=api_key)
                    todos_lancamentos = []
                    auditoria_logs = []

                    with st.spinner("🤖 A executar pipeline de extração e validação estruturada..."):
                        for uploaded_file in uploaded_files:
                            bytes_data = uploaded_file.getvalue()
                            file_name = uploaded_file.name
                            
                            if file_name.endswith('.ofx'):
                                lanc_ofx = parse_ofx(bytes_data)
                                if lanc_ofx:
                                    todos_lancamentos.extend(lanc_ofx)
                                    auditoria_logs.append({"arquivo": file_name, "tipo": "OFX (Local)", "status": "Sucesso", "total": len(lanc_ofx)})
                                else:
                                    auditoria_logs.append({"arquivo": file_name, "tipo": "OFX (Local)", "status": "Falha/Vazio", "total": 0})
                            else:
                                prompt = """
                                Analise este documento financeiro e extraia rigorosamente todos os lançamentos financeiros.
                                Respeite escrupulosamente os tipos de dados exigidos pelo esquema estruturado.
                                """

                                resposta = client.models.generate_content(
                                    model='gemini-3.8-flash',
                                    contents=[
                                        types.Part.from_bytes(data=bytes_data, mime_type=uploaded_file.type),
                                        prompt
                                    ],
                                    config=types.GenerateContentConfig(
                                        response_mime_type="application/json",
                                        response_schema=RelatorioContabilSchema,
                                    ),
                                )

                                dados_json = json.loads(resposta.text)
                                if "lancamentos" in dados_json:
                                    todos_lancamentos.extend(dados_json["lancamentos"])
                                    auditoria_logs.append({"arquivo": file_name, "tipo": "IA (Gemini)", "status": "Sucesso", "total": len(dados_json["lancamentos"])})
                                else:
                                    auditoria_logs.append({"arquivo": file_name, "tipo": "IA (Gemini)", "status": "Sem lançamentos", "total": 0})

                    if todos_lancamentos:
                        df = pd.DataFrame(todos_lancamentos)
                        df['valor'] = pd.to_numeric(df['valor'], errors='coerce').fillna(0.0)

                        # Correção Contábil Matemática do Sinal (Valor Assinado)
                        df['valor_assinado'] = df.apply(
                            lambda row: row['valor'] if str(row['tipo']).upper() == 'CREDITO' else -row['valor'],
                            axis=1
                        )

                        # Aplicar LGPD se ativado
                        if mascarar_dados:
                            df['descricao'] = df['descricao'].apply(aplicar_mascara_lgpd)

                        st.markdown("---")
                        st.subheader("📊 Painel de Controlo & Métricas Contábeis")

                        total_creditos = df.loc[df['tipo'].str.upper() == 'CREDITO', 'valor'].sum()
                        total_debitos = df.loc[df['tipo'].str.upper() == 'DEBITO', 'valor'].sum()
                        saldo_liquido = df['valor_assinado'].sum() # Cálculo matemático correto

                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("📈 Total de Créditos", f"R$ {total_creditos:,.2f}")
                        c2.metric("📉 Total de Débitos", f"R$ {total_debitos:,.2f}")
                        c3.metric("💰 Saldo Líquido Consolidado", f"R$ {saldo_liquido:,.2f}")
                        c4.metric("📋 Transações Auditadas", len(df))

                        # Trilha de Auditoria Visível
                        with st.expander("🔍 Ver Relatório de Trilha de Auditoria por Ficheiro"):
                            st.dataframe(pd.DataFrame(auditoria_logs), use_container_width=True)

                        st.markdown("---")
                        st.subheader("📈 Distribuição Financeira por Categoria")
                        
                        df_grafico = df.groupby('categoria')['valor'].sum()
                        if not df_grafico.empty:
                            st.bar_chart(df_grafico)

                        st.markdown("---")
                        st.subheader("🔍 Filtros e Livro de Lançamentos")

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

                        # Exibir tabela limpa sem mostrar a coluna técnica interna de cálculo
                        tabela_exibicao = df_filtrado.drop(columns=['valor_assinado'])
                        st.dataframe(tabela_exibicao, use_container_width=True)

                        st.markdown("---")
                        col_d1, col_d2 = st.columns(2)
                        
                        # Exportação correta para Excel/Sheets (encoding utf-8-sig e separador ;)
                        csv = df_filtrado.drop(columns=['valor_assinado']).to_csv(index=False, sep=";", encoding='utf-8-sig').encode('utf-8-sig')
                        col_d1.download_button(
                            label="📥 Descarregar CSV (Compatível com Excel)",
                            data=csv,
                            file_name='livro_contabil_auditado.csv',
                            mime='text/csv',
                        )

                        json_str = df_filtrado.drop(columns=['valor_assinado']).to_json(orient="records", force_ascii=False, date_format="iso")
                        col_d2.download_button(
                            label="📥 Descarregar JSON Estruturado",
                            data=json_str,
                            file_name='livro_contabil_auditado.json',
                            mime='application/json',
                        )
                    else:
                        st.warning("⚠️ Nenhum lançamento válido foi identificado nos ficheiros submetidos.")

                except Exception as e:
                    st.error(f"❌ Erro crítico no motor contábil: {e}")
