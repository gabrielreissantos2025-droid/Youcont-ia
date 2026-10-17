import hashlib
import html
import json
import re
import secrets
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd
import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError, field_validator

from db import (
    POOL,
    agora,
    formatar_brl,
    hash_senha,
    inicializar_banco,
    normalizar_decimal,
    registrar_log,
    transacao,
    valor_para_centavos,
    verificar_cadeia,
)

MODELO_GEMINI = st.secrets.get("MODELO_GEMINI", "gemini-2.5-flash")

MIME_POR_EXTENSAO = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ofx": "text/plain",
}

MAGIC_BYTES = {
    ".pdf": b"%PDF-",
    ".png": b"PNG
\u001A
",
    ".jpg": b"ÿØÿ",
    ".jpeg": b"ÿØÿ",
}

st.set_page_config(
    page_title="Agente Contábil Multiempresa",
    page_icon="💼",
    layout="wide",
)

st.title("💼 Agente Contábil Multiempresa & Auditoria")
st.markdown(
    "Pipeline multiempresa com PostgreSQL, isolamento por tenant, "
    "auditoria encadeada e revisão humana."
)


# =============================================================
# AUTENTICAÇÃO
# =============================================================

def autenticar(email: str, senha: str):
    with POOL.connection() as conn:
        usuario = conn.execute(
            """
            SELECT u.usuario_id, u.tenant_id, u.nome, u.papel,
                   u.senha_salt, u.senha_hash, t.nome
            FROM usuarios u
            JOIN tenants t ON t.tenant_id = u.tenant_id
            WHERE u.email = %s AND u.ativo = true
            """,
            (email.lower().strip(),),
        ).fetchone()

    if not usuario:
        return None

    (
        usuario_id,
        tenant_id,
        nome,
        papel,
        salt,
        senha_hash,
        tenant_nome,
    ) = usuario

    tentativa = hash_senha(senha, salt)

    if not secrets.compare_digest(tentativa, senha_hash):
        return None

    return {
        "usuario_id": usuario_id,
        "tenant_id": tenant_id,
        "nome": nome,
        "papel": papel,
        "tenant_nome": tenant_nome,
    }


# =============================================================
# VALIDAÇÃO
# =============================================================

class LancamentoSchema(BaseModel):
    data: str
    descricao: str
    valor: float
    tipo: Literal["CREDITO", "DEBITO"]
    categoria: Literal[
        "Investimentos",
        "Transferências",
        "Impostos",
        "Fornecedores",
        "Despesas Operacionais",
        "Outros",
    ]

    @field_validator("data")
    @classmethod
    def validar_data(cls, valor: str) -> str:
        datetime.strptime(valor, "%d/%m/%Y")
        return valor


class RelatorioContabilSchema(BaseModel):
    lancamentos: list[LancamentoSchema]


# =============================================================
# UTILITÁRIOS
# =============================================================

def calcular_hash_arquivo(bytes_arquivo: bytes) -> str:
    return hashlib.sha256(bytes_arquivo).hexdigest()


def validar_conteudo(extensao: str, bytes_arquivo: bytes) -> bool:
    assinatura = MAGIC_BYTES.get(extensao)
    return assinatura is None or bytes_arquivo.startswith(assinatura)


def aplicar_mascara_lgpd(texto: str) -> str:
    if not isinstance(texto, str):
        return texto

    texto = re.sub(
        r"\bd{3}.d{3}.d{3}-d{2}\b",
        "***.***.***-**",
        texto,
    )
    texto = re.sub(r"\bd{11}\b", "***********", texto)
    texto = re.sub(
        r"\bd{2}.d{3}.d{3}/d{4}-d{2}\b",
        "**.***.***/****-**",
        texto,
    )
    texto = re.sub(r"\bd{14}\b", "**************", texto)
    texto = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+.[A-Za-z]{2,}\b",
        "****@****.***",
        texto,
    )
    return texto


def decodificar_ofx(bytes_arquivo: bytes) -> str:
    try:
        return bytes_arquivo.decode("utf-8")
    except UnicodeDecodeError:
        return bytes_arquivo.decode("cp1252", errors="replace")


# =============================================================
# PARSER OFX
# =============================================================

def parse_ofx(bytes_arquivo: bytes, arquivo_hash: str):
    lancamentos = []
    erros = []
    avisos = []
    chaves_locais = set()
    duplicados = 0

    conteudo = decodificar_ofx(bytes_arquivo)
    corpo = (
        conteudo[conteudo.index("<OFX>"):]
        if "<OFX>" in conteudo
        else conteudo
    )

    blocos = [
        match.group(2)
        for match in re.finditer(
            r"<(STMTRS|CCSTMTRS)>(.*?)</\u0001>",
            corpo,
            re.DOTALL,
        )
    ] or [corpo]

    indice = 0

    for bloco in blocos:
        bank = re.search(r"<BANKID>([^<
]+)", bloco)
        acct = re.search(r"<ACCTID>([^<
]+)", bloco)

        bank_id = bank.group(1).strip() if bank else "UNKNOWN_BANK"
        acct_id = acct.group(1).strip() if acct else "UNKNOWN_ACCT"

        if "UNKNOWN_" in (bank_id, acct_id):
            avisos.append(
                "Metadados bancários incompletos; "
                "deduplicação por FITID/hash."
            )

        transacoes = re.findall(
            r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|$)",
            bloco,
            re.DOTALL,
        )

        ocorrencias = {}

        for transacao in transacoes:
            indice += 1

            try:
                data_m = re.search(r"<DTPOSTED>(d{8})", transacao)
                valor_m = re.search(r"<TRNAMT>([^<
]+)", transacao)
                memo_m = re.search(r"<MEMO>([^<
]+)", transacao)
                nome_m = re.search(r"<NAME>([^<
]+)", transacao)
                fitid_m = re.search(r"<FITID>([^<
]+)", transacao)

                if not data_m or not valor_m:
                    raise ValueError("Sem DTPOSTED ou TRNAMT")

                data_bruta = data_m.group(1)
                data_iso = (
                    f"{data_bruta[0:4]}-"
                    f"{data_bruta[4:6]}-"
                    f"{data_bruta[6:8]}"
                )
                datetime.strptime(data_iso, "%Y-%m-%d")

                centavos = valor_para_centavos(valor_m.group(1))
                tipo = "CREDITO" if centavos >= 0 else "DEBITO"

                descricao = (
                    memo_m.group(1).strip()
                    if memo_m
                    else nome_m.group(1).strip()
                    if nome_m
                    else "Transação OFX"
                )

                if fitid_m:
                    chave = (
                        f"{bank_id}|{acct_id}|"
                        f"{fitid_m.group(1).strip()}"
                    )
                else:
                    assinatura = (
                        f"{data_bruta}|{centavos}|{descricao}"
                    )
                    ocorrencias[assinatura] = (
                        ocorrencias.get(assinatura, 0) + 1
                    )

                    hash_local = hashlib.sha256(
                        f"{bank_id}|{acct_id}|{assinatura}|"
                        f"{ocorrencias[assinatura]}".encode()
                    ).hexdigest()

                    chave = f"{bank_id}|{acct_id}|HASH-{hash_local}"

                if chave in chaves_locais:
                    duplicados += 1
                    continue

                categoria = (
                    "Transferências"
                    if centavos < 0
                    else "Outros"
                )

                descricao_upper = descricao.upper()

                if "INVEST" in descricao_upper or re.search(
                    r"\bB3\b", descricao_upper
                ):
                    categoria = "Investimentos"
                elif "IMPOSTO" in descricao_upper or "IOF" in descricao_upper:
                    categoria = "Impostos"

                lancamentos.append(
                    {
                        "chave_dedup": chave,
                        "arquivo_hash": arquivo_hash,
                        "data_iso": data_iso,
                        "descricao_original": html.unescape(descricao),
                        "valor_centavos": abs(centavos),
                        "tipo": tipo,
                        "categoria": categoria,
                        "origem": "OFX local",
                    }
                )
                chaves_locais.add(chave)

            except Exception as erro:
                erros.append(f"Transação {indice}: {erro}")

    return lancamentos, erros, avisos, duplicados


# =============================================================
# GEMINI
# =============================================================

PROMPT_EXTRACAO = """
Extraia todos os lançamentos financeiros do documento.
Não invente lançamentos.
Se não houver transações claras, retorne lista vazia.
Ignore saldos, totais e subtotais.
Datas em DD/MM/AAAA.
Valores absolutos e positivos.
Saídas são DEBITO; entradas são CREDITO.
"""


def chamar_gemini(client, bytes_data: bytes, mime_tipo: str):
    ultimo_erro = None

    for tentativa in range(3):
        try:
            return client.models.generate_content(
                model=MODELO_GEMINI,
                contents=[
                    types.Part.from_bytes(
                        data=bytes_data,
                        mime_type=mime_tipo,
                    ),
                    PROMPT_EXTRACAO,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RelatorioContabilSchema,
                ),
            )
        except Exception as erro:
            ultimo_erro = erro
            if tentativa < 2:
                time.sleep(2 ** tentativa)

    raise ultimo_erro


# =============================================================
# PERSISTÊNCIA
# =============================================================

def inserir_lancamentos(
    conn,
    tenant_id: str,
    cliente_id: str,
    lancamentos: list[dict],
):
    inseridos = 0

    for lancamento in lancamentos:
        cursor = conn.execute(
            """
            INSERT INTO lancamentos (
                tenant_id,
                cliente_id,
                lancamento_id,
                chave_dedup,
                arquivo_hash,
                data_iso,
                descricao_original,
                valor_centavos,
                tipo,
                categoria,
                origem
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, chave_dedup) DO NOTHING
            """,
            (
                tenant_id,
                cliente_id,
                str(uuid.uuid4()),
                lancamento["chave_dedup"],
                lancamento["arquivo_hash"],
                lancamento["data_iso"],
                lancamento["descricao_original"],
                lancamento["valor_centavos"],
                lancamento["tipo"],
                lancamento["categoria"],
                lancamento["origem"],
            ),
        )

        inseridos += cursor.rowcount

    return inseridos, len(lancamentos) - inseridos


def carregar_livro(tenant_id: str, cliente_id: str) -> pd.DataFrame:
    with POOL.connection() as conn:
        df = pd.read_sql_query(
            """
            SELECT lancamento_id, arquivo_hash, data_iso AS data,
                   descricao_original, valor_centavos AS valor,
                   tipo, categoria, origem, requer_revisao
            FROM lancamentos
            WHERE tenant_id = %s AND cliente_id = %s
            ORDER BY data_iso DESC
            """,
            conn,
            params=(tenant_id, cliente_id),
        )

    if df.empty:
        return df

    df["requer_revisao"] = df["requer_revisao"].astype(bool)
    df["valor"] = df["valor"].astype("int64")
    df["valor_assinado"] = df["valor"].where(
        df["tipo"] == "CREDITO",
        -df["valor"],
    )
    df["possivel_duplicata"] = df.duplicated(
        subset=["data", "valor", "tipo", "descricao_original"],
        keep=False,
    )
    return df


def carregar_log(tenant_id: str, limite: int = 300) -> pd.DataFrame:
    with POOL.connection() as conn:
        return pd.read_sql_query(
            """
            SELECT log_id, criado_em AS ts, evento, arquivo, tipo,
                   status, total, destino,
                   substring(hash_registro from 1 for 12) AS hash_registro
            FROM auditoria_log
            WHERE tenant_id = %s
            ORDER BY log_id DESC
            LIMIT %s
            """,
            conn,
            params=(tenant_id, limite),
        )


def carregar_quarentena(tenant_id: str, cliente_id: str) -> pd.DataFrame:
    with POOL.connection() as conn:
        return pd.read_sql_query(
            """
            SELECT id, criado_em AS ts, arquivo, item_json, erro_json
            FROM quarentena
            WHERE tenant_id = %s AND cliente_id = %s
            ORDER BY id DESC
            """,
            conn,
            params=(tenant_id, cliente_id),
        )


# =============================================================
# PIPELINE
# =============================================================

def processar_upload(
    tenant_id: str,
    usuario_id: str,
    cliente_id: str,
    uploaded_files,
    consentimento: bool,
):
    api_key = st.secrets.get("GEMINI_API_KEY")

    if not api_key:
        st.error("Credencial Gemini não configurada.")
        return

    client = genai.Client(api_key=api_key)

    with transacao(tenant_id) as conn:
        for arquivo in uploaded_files:
            bytes_arquivo = arquivo.getvalue()
            nome_arquivo = arquivo.name
            extensao = Path(nome_arquivo).suffix.lower()
            arquivo_hash = calcular_hash_arquivo(bytes_arquivo)

            mime = MIME_POR_EXTENSAO.get(extensao)

            if not mime:
                st.warning(f"Extensão não suportada: {nome_arquivo}")
                continue

            if not validar_conteudo(extensao, bytes_arquivo):
                st.error(f"Conteúdo incompatível: {nome_arquivo}")
                continue

            documento_id = str(uuid.uuid4())

            conn.execute(
                """
                INSERT INTO documentos (
                    documento_id, tenant_id, cliente_id, origem,
                    tipo_documento, nome_arquivo, arquivo_hash,
                    mime_type, tamanho_bytes, status
                )
                VALUES (
                    %s, %s, %s, 'UPLOAD', 'OUTRO',
                    %s, %s, %s, %s, 'PROCESSANDO'
                )
                ON CONFLICT (tenant_id, arquivo_hash) DO NOTHING
                """,
                (
                    documento_id,
                    tenant_id,
                    cliente_id,
                    nome_arquivo,
                    arquivo_hash,
                    mime,
                    len(bytes_arquivo),
                ),
            )

            if extensao == ".ofx":
                lancamentos, erros, avisos, duplicados = parse_ofx(
                    bytes_arquivo,
                    arquivo_hash,
                )

                inseridos, ignorados = inserir_lancamentos(
                    conn,
                    tenant_id,
                    cliente_id,
                    lancamentos,
                )

                status = "PROCESSADO" if not erros else "QUARENTENA"

                conn.execute(
                    """
                    UPDATE documentos
                    SET status = %s, processado_em = now()
                    WHERE tenant_id = %s AND documento_id = %s
                    """,
                    (status, tenant_id, documento_id),
                )

                registrar_log(
                    conn,
                    tenant_id=tenant_id,
                    usuario_id=usuario_id,
                    evento="IMPORTACAO_OFX",
                    cliente_id=cliente_id,
                    arquivo=nome_arquivo,
                    arquivo_hash=arquivo_hash,
                    tipo="OFX local",
                    status=status,
                    total=inseridos,
                    consentimento_externo=False,
                    destino="Processamento local",
                    detalhe=(
                        f"Erros: {len(erros)}; "
                        f"duplicados: {duplicados}; "
                        f"ignorados: {ignorados}"
                    ),
                )

                if erros:
                    st.warning(f"{nome_arquivo}: {erros[0]}")

            else:
                ja_processado = conn.execute(
                    """
                    SELECT 1
                    FROM documentos
                    WHERE tenant_id = %s
                      AND cliente_id = %s
                      AND arquivo_hash = %s
                      AND status = 'PROCESSADO'
                    """,
                    (tenant_id, cliente_id, arquivo_hash),
                ).fetchone()

                if ja_processado:
                    st.info(f"Arquivo já processado: {nome_arquivo}")
                    continue

                try:
                    resposta = chamar_gemini(
                        client,
                        bytes_arquivo,
                        mime,
                    )
                    dados = json.loads(resposta.text)

                    extracao_id = str(uuid.uuid4())

                    conn.execute(
                        """
                        INSERT INTO extracoes_ia (
                            extracao_id, tenant_id, documento_id,
                            modelo, resposta_json, status
                        )
                        VALUES (%s, %s, %s, %s, %s, 'SUCESSO')
                        """,
                        (
                            extracao_id,
                            tenant_id,
                            documento_id,
                            MODELO_GEMINI,
                            resposta.text,
                        ),
                    )

                    validos = []
                    rejeitados = 0

                    for item in dados.get("lancamentos", []):
                        try:
                            lancamento = LancamentoSchema.model_validate(item)

                            chave_item = hashlib.sha256(
                                f"{arquivo_hash}|"
                                f"{lancamento.data}|"
                                f"{lancamento.valor}|"
                                f"{lancamento.tipo}|"
                                f"{lancamento.descricao}".encode()
                            ).hexdigest()

                            validos.append(
                                {
                                    "chave_dedup": f"IA|{chave_item}",
                                    "arquivo_hash": arquivo_hash,
                                    "data_iso": datetime.strptime(
                                        lancamento.data, "%d/%m/%Y"
                                    ).strftime("%Y-%m-%d"),
                                    "descricao_original": lancamento.descricao,
                                    "valor_centavos": valor_para_centavos(
                                        lancamento.valor
                                    ),
                                    "tipo": lancamento.tipo,
                                    "categoria": lancamento.categoria,
                                    "origem": "Gemini IA",
                                }
                            )

                        except ValidationError as erro:
                            rejeitados += 1

                            conn.execute(
                                """
                                INSERT INTO quarentena (
                                    tenant_id, cliente_id, arquivo,
                                    arquivo_hash, item_json, erro_json
                                )
                                VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    tenant_id,
                                    cliente_id,
                                    nome_arquivo,
                                    arquivo_hash,
                                    json.dumps(item, default=str),
                                    json.dumps(
                                        erro.errors(include_url=False),
                                        default=str,
                                    ),
                                ),
                            )

                    inseridos, ignorados = inserir_lancamentos(
                        conn,
                        tenant_id,
                        cliente_id,
                        validos,
                    )

                    status = (
                        "PROCESSADO"
                        if rejeitados == 0
                        else "QUARENTENA"
                    )

                    conn.execute(
                        """
                        UPDATE documentos
                        SET status = %s, processado_em = now()
                        WHERE tenant_id = %s AND documento_id = %s
                        """,
                        (status, tenant_id, documento_id),
                    )

                    registrar_log(
                        conn,
                        tenant_id=tenant_id,
                        usuario_id=usuario_id,
                        evento="EXTRACAO_IA",
                        cliente_id=cliente_id,
                        arquivo=nome_arquivo,
                        arquivo_hash=arquivo_hash,
                        tipo="Gemini IA",
                        status=status,
                        total=inseridos,
                        consentimento_externo=True,
                        destino="Google Gemini API",
                        detalhe=(
                            f"Rejeitados: {rejeitados}; "
                            f"ignorados: {ignorados}"
                        ),
                    )

                    if rejeitados:
                        st.warning(
                            f"{nome_arquivo}: {rejeitados} "
                            "item(ns) em quarentena."
                        )

                except Exception as erro:
                    conn.execute(
                        """
                        UPDATE documentos
                        SET status = 'ERRO', processado_em = now()
                        WHERE tenant_id = %s AND documento_id = %s
                        """,
                        (tenant_id, documento_id),
                    )

                    registrar_log(
                        conn,
                        tenant_id=tenant_id,
                        usuario_id=usuario_id,
                        evento="EXTRACAO_IA_FALHA",
                        cliente_id=cliente_id,
                        arquivo=nome_arquivo,
                        arquivo_hash=arquivo_hash,
                        tipo="Gemini IA",
                        status="ERRO",
                        total=0,
                        consentimento_externo=True,
                        destino="Google Gemini API",
                        detalhe=str(erro)[:180],
                    )

                    st.error(f"Erro ao processar {nome_arquivo}.")

    st.success("Processamento concluído e persistido.")


# =============================================================
# INTERFACE
# =============================================================

if "usuario" not in st.session_state:
    st.session_state["usuario"] = None

if st.session_state["usuario"] is None:
    st.subheader("🔐 Acesso ao escritório")

    with st.form("login"):
        email = st.text_input("E-mail")
        senha = st.text_input("Senha", type="password")
        entrar = st.form_submit_button("Entrar", type="primary")

    if entrar:
        usuario = autenticar(email, senha)

        if usuario:
            st.session_state["usuario"] = usuario
            st.rerun()
        else:
            st.error("E-mail ou senha inválidos.")

    st.stop()

usuario = st.session_state["usuario"]
tenant_id = usuario["tenant_id"]

with st.sidebar:
    st.header("🏢 Escritório")
    st.write(f"**{usuario['tenant_nome']}**")
    st.write(f"{usuario['nome']} — {usuario['papel']}")

    with POOL.connection() as conn:
        clientes = conn.execute(
            """
            SELECT cliente_id, razao_social
            FROM clientes
            WHERE tenant_id = %s AND ativo = true
            ORDER BY razao_social
            """,
            (tenant_id,),
        ).fetchall()

    if not clientes:
        st.warning("Nenhum cliente cadastrado.")
        st.stop()

    cliente = st.selectbox(
        "Cliente ativo",
        clientes,
        format_func=lambda c: c[1],
    )

    cliente_id = cliente[0]

    st.markdown("---")

    with st.form("upload_form"):
        uploaded_files = st.file_uploader(
            "📂 Documentos do cliente",
            type=["pdf", "png", "jpg", "jpeg", "ofx"],
            accept_multiple_files=True,
        )

        aviso_lgpd = st.checkbox(
            "Autorizo envio de documentos não-locais à API externa.",
            value=False,
        )

        mascarar_dados = st.checkbox(
            "Mascarar dados sensíveis na exibição",
            value=True,
        )

        processar = st.form_submit_button(
            "🚀 Processar documentos",
            type="primary",
        )

if processar:
    if not uploaded_files:
        st.warning("Selecione arquivos.")
    elif not aviso_lgpd:
        st.warning("Confirme o consentimento de envio externo.")
    else:
        processar_upload(
            tenant_id,
            usuario["usuario_id"],
            cliente_id,
            uploaded_files,
            aviso_lgpd,
        )

livro = carregar_livro(tenant_id, cliente_id)

if livro.empty:
    st.info("Nenhum lançamento para este cliente.")
    st.stop()

df_exibicao = livro.copy()

if mascarar_dados:
    df_exibicao["descricao_exibicao"] = df_exibicao[
        "descricao_original"
    ].apply(aplicar_mascara_lgpd)
else:
    df_exibicao["descricao_exibicao"] = df_exibicao[
        "descricao_original"
    ]

st.markdown("---")
st.subheader("📊 Painel Contábil")

creditos = df_exibicao.loc[
    df_exibicao["tipo"] == "CREDITO", "valor"
].sum()

debitos = df_exibicao.loc[
    df_exibicao["tipo"] == "DEBITO", "valor"
].sum()

saldo = df_exibicao["valor_assinado"].sum()

col1, col2, col3, col4 = st.columns(4)
col1.metric("📈 Créditos", formatar_brl(int(creditos)))
col2.metric("📉 Débitos", formatar_brl(int(debitos)))
col3.metric("💰 Saldo", formatar_brl(int(saldo)))
col4.metric("📋 Lançamentos", len(df_exibicao))

with st.expander("🔍 Trilha de auditoria"):
    integridade, registro_invalido, total = verificar_cadeia(tenant_id)

    if integridade:
        st.success(f"Cadeia íntegra: {total} registros.")
    else:
        st.error(f"Cadeia comprometida no registro {registro_invalido}.")

    st.dataframe(
        carregar_log(tenant_id),
        use_container_width=True,
    )

with st.expander("🚨 Quarentena"):
    quarentena = carregar_quarentena(tenant_id, cliente_id)

    if quarentena.empty:
        st.info("Nenhum item em quarentena.")
    else:
        st.dataframe(quarentena, use_container_width=True)

st.markdown("---")
st.subheader("🔍 Revisão humana")

df_tabela = df_exibicao[
    [
        "lancamento_id",
        "data",
        "descricao_exibicao",
        "valor",
        "tipo",
        "categoria",
        "requer_revisao",
        "possivel_duplicata",
    ]
].rename(columns={"descricao_exibicao": "descricao"})

st.data_editor(
    df_tabela,
    column_config={
        "lancamento_id": st.column_config.TextColumn("ID", disabled=True),
        "valor": st.column_config.NumberColumn(
            "Valor (centavos)",
            disabled=True,
        ),
        "requer_revisao": st.column_config.CheckboxColumn(
            "Requer revisão",
            default=True,
        ),
        "possivel_duplicata": st.column_config.CheckboxColumn(
            "Possível duplicata",
            disabled=True,
        ),
    },
    disabled=[
        "lancamento_id",
        "data",
        "descricao",
        "valor",
        "tipo",
        "categoria",
        "possivel_duplicata",
    ],
    use_container_width=True,
    key="editor_livro_contabil",
)