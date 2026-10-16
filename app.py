import os
import json
import pandas as pd
import streamlit as st
from google import genai
from google.genai import types

# ------------------------------------------------------------------------------
# 1. Configuração da Página do Streamlit
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="Agente Contábil IA - MVP",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Agente Contábil Inteligente")
st.markdown(
    "Faça upload de extratos bancários (PDF) ou comprovantes (PNG/JPG). "
    "A IA irá extrair e estruturar todos os lançamentos financeiros automaticamente."
)

# Sidebar para inserir/configurar a Chave da API
st.sidebar.header("⚙️ Configurações")
api_key_input = st.sidebar.text_input(
    "Chave da API Gemini (GEMINI_API_KEY):", 
    type="password",
    help="Cole sua chave da API do Google AI Studio aqui."
)

# Prioriza a chave digitada na sidebar, caso contrário usa a variável de ambiente
api_key = api_key_input or os.environ.get("GEMINI_API_KEY")

# ------------------------------------------------------------------------------
# 2. Funções de IA e Processamento de Dados
# ------------------------------------------------------------------------------
def extrair_dados_financeiros(caminho_arquivo, key):
    """Envia o arquivo para a API do Gemini e obtém o JSON com lançamentos."""
    client = genai.Client(api_key=key)
    
    # Upload do arquivo para os servidores da API Gemini
    arquivo_enviado = client.files.upload(file=caminho_arquivo)

    prompt = """
    Você é um assistente contábil especialista em extração de dados de documentos financeiros brasileiros.
    Analise o documento fornecido e extraia todos os lançamentos financeiros em um formato JSON estrito.
    
    O JSON deve conter uma chave raiz chamada 'lancamentos', que é uma lista de objetos.
    Cada objeto deve ter exatamente as seguintes chaves:
    - "data": "DD/MM/AAAA"
    - "descricao": "Texto descritivo do lançamento"
    - "valor": float (número positivo)
    - "tipo": "CREDITO" ou "DEBITO"
    
    Regras estritas:
    1. Não invente ou alucine dados. Se um campo não estiver claro, deixe null.
    2. Garanta que o 'valor' seja sempre um número decimal simples (exemplo: 150.50).
    3. Retorne APENAS o JSON puro.
    """

    resposta = client.models.generate_content(
        model='gemini-1.5-flash',
        contents=[arquivo_enviado, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        ),
    )

    return json.loads(resposta.text)

def validar_e_processar(dados_json):
    """Valida a lógica matemática e calcula totais de débitos e créditos."""
    total_debitos = 0.0
    total_creditos = 0.0
    lancamentos_validados = []

    for item in dados_json.get("lancamentos", []):
        try:
            valor = float(item["valor"])
            tipo = str(item.get("tipo", "")).upper()

            if tipo == "DEBITO":
                total_debitos += valor
            elif tipo == "CREDITO":
                total_creditos += valor

            lancamentos_validados.append({
                "Data": item.get("data", ""),
                "Descrição": item.get("descricao", ""),
                "Valor (R$)": valor,
                "Tipo": tipo
            })
        except (ValueError, TypeError):
            continue

    return lancamentos_validados, total_debitos, total_creditos

# ------------------------------------------------------------------------------
# 3. Interface Principal da Aplicação
# ------------------------------------------------------------------------------
arquivo_carregado = st.file_uploader(
    "Arraste ou selecione seu arquivo aqui:", 
    type=["pdf", "png", "jpg", "jpeg"]
)

if arquivo_carregado is not None:
    if not api_key:
        st.warning("⚠️ Insira sua Chave da API do Gemini na barra lateral para prosseguir.")
    else:
        # Salva o arquivo temporariamente na pasta local
        caminho_temp = f"temp_{arquivo_carregado.name}"
        with open(caminho_temp, "wb") as f:
            f.write(arquivo_carregado.getbuffer())

        st.info(f"📁 Arquivo **{arquivo_carregado.name}** pronto para análise.")

        if st.button("🚀 Processar Documento"):
            with st.spinner("A IA está lendo o documento e extraindo os dados..."):
                try:
                    # 1. Processamento via IA
                    dados_brutos = extrair_dados_financeiros(caminho_temp, api_key)
                    
                    # 2. Validação Matemática em Python
                    lancamentos, total_deb, total_cred = validar_e_processar(dados_brutos)

                    st.success("✅ Extracção concluída com sucesso!")

                    # Exibição de Métricas
                    col1, col2, col3 = st.columns(3)
                    col1.metric("Total de Débitos", f"R$ {total_deb:,.2f}")
                    col2.metric("Total de Créditos", f"R$ {total_cred:,.2f}")
                    col3.metric("Lançamentos Encontrados", len(lancamentos))

                    st.divider()

                    # Exibição da Tabela e Downloads
                    if lancamentos:
                        st.subheader("📋 Tabela de Lançamentos Conciliados")
                        df = pd.DataFrame(lancamentos)
                        st.dataframe(df, use_container_width=True)

                        col_down1, col_down2 = st.columns(2)
                        
                        # Download em CSV (Abre direto no Excel)
                        csv_data = df.to_csv(index=False).encode('utf-8')
                        col_down1.download_button(
                            label="📥 Baixar em CSV (Excel)",
                            data=csv_data,
                            file_name="lancamentos_contabeis.csv",
                            mime="text/csv"
                        )

                        # Download em JSON (Para integração com sistemas)
                        json_data = json.dumps(lancamentos, indent=4, ensure_ascii=False)
                        col_down2.download_button(
                            label="📥 Baixar em JSON",
                            data=json_data,
                            file_name="lancamentos_contabeis.json",
                            mime="application/json"
                        )
                    else:
                        st.warning("Nenhum lançamento válido foi identificado no documento enviado.")

                except Exception as e:
                    st.error(f"❌ Ocorreu um erro ao processar o arquivo: {e}")
                
                finally:
                    # Remove o arquivo temporário
                    if os.path.exists(caminho_temp):
                        os.remove(caminho_temp)


