import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel, Field, ValidationError, field_validator

# =============================================================
# CONFIGURAÇÃO
# =============================================================

def ler_secret(nome: str, padrao=None):
    try:
        return st.secrets[nome]
    except Exception:
        return os.environ.get(nome, padrao)


MODELO_GEMINI = ler_secret("MODELO_GEMINI", "gemini-2.5-flash")
DB_PATH = Path(ler_secret("DB_PATH", "dados_auditoria/auditoria.db"))
MAX_UPLOAD_MB = 20
PBKDF2_ITERACOES = 390_000

st.set_page_config(
    page_title="Agente Contábil Multiempresa",
    page_icon="💼",
    layout="wide",
)

st.title("💼 Agente Contábil Multiempresa & Auditoria")
st.markdown(
    "Plataforma multiempresa para escritórios contábeis: clientes isolados, "
    "documentos rastreáveis, deduplicação por tenant e trilha auditável."
)

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

# =============================================================
# BANCO DE DADOS MULTIEMPRESA
# =============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    nome        TEXT NOT NULL,
    cnpj        TEXT UNIQUE,
    criado_em   TEXT NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS usuarios (
    usuario_id       TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    email            TEXT NOT NULL,
    nome             TEXT NOT NULL,
    papel            TEXT NOT NULL CHECK (
        papel IN ('ADMIN', 'CONTADOR', 'ASSISTENTE', 'LEITURA')
    ),
    senha_salt       TEXT NOT NULL,
    senha_hash       TEXT NOT NULL,
    criado_em        TEXT NOT NULL,
    ativo            INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS clientes (
    cliente_id  TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES tenants(tenant_id),
    razao_social TEXT NOT NULL,
    nome_fantasia TEXT,
    cnpj        TEXT,
    criado_em   TEXT NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, cnpj)
);

CREATE TABLE IF NOT EXISTS usuario_clientes (
    tenant_id   TEXT NOT NULL,
    usuario_id  TEXT NOT NULL REFERENCES usuarios(usuario_id),
    cliente_id  TEXT NOT NULL REFERENCES clientes(cliente_id),
    PRIMARY KEY (tenant_id, usuario_id, cliente_id),
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id)
);

CREATE TABLE IF NOT EXISTS contas_financeiras (
    conta_id    TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    cliente_id  TEXT NOT NULL,
    instituicao TEXT NOT NULL,
    tipo        TEXT NOT NULL CHECK (
        tipo IN ('BANCO', 'CARTAO', 'CAIXA', 'INVESTIMENTO', 'OUTRO')
    ),
    agencia     TEXT,
    numero_conta TEXT,
    moeda       TEXT NOT NULL DEFAULT 'BRL',
    criado_em   TEXT NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id),
    UNIQUE (tenant_id, cliente_id, instituicao, agencia, numero_conta)
);

CREATE TABLE IF NOT EXISTS documentos (
    documento_id   TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    cliente_id     TEXT NOT NULL,
    origem         TEXT NOT NULL CHECK (
        origem IN ('UPLOAD', 'WHATSAPP', 'EMAIL', 'API')
    ),
    tipo_documento TEXT NOT NULL CHECK (
        tipo_documento IN (
            'EXTRATO', 'NFE', 'RECIBO', 'COMPROVANTE',
            'INFORME_RENDIMENTOS', 'FATURA', 'OUTRO'
        )
    ),
    nome_arquivo   TEXT NOT NULL,
    arquivo_hash   TEXT NOT NULL,
    mime_type      TEXT NOT NULL,
    tamanho_bytes  INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'RECEBIDO' CHECK (
        status IN ('RECEBIDO', 'PROCESSANDO', 'PROCESSADO', 'ERRO', 'QUARENTENA')
    ),
    recebido_em    TEXT NOT NULL,
    processado_em  TEXT,
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id),
    UNIQUE (tenant_id, arquivo_hash)
);

CREATE TABLE IF NOT EXISTS extracoes_ia (
    extracao_id   TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    documento_id  TEXT NOT NULL,
    modelo        TEXT NOT NULL,
    resposta_json TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (
        status IN ('SUCESSO', 'PARCIAL', 'FALHA')
    ),
    criado_em     TEXT NOT NULL,
    FOREIGN KEY (tenant_id, documento_id)
        REFERENCES documentos(tenant_id, documento_id)
);

CREATE TABLE IF NOT EXISTS lancamentos (
    lancamento_id       TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    cliente_id          TEXT NOT NULL,
    documento_id        TEXT,
    extracao_id         TEXT,

    chave_dedup         TEXT NOT NULL,
    arquivo_hash        TEXT NOT NULL,
    data                TEXT NOT NULL,
    data_iso            TEXT NOT NULL,
    descricao_original  TEXT NOT NULL,

    valor_centavos      INTEGER NOT NULL CHECK (valor_centavos >= 0),
    tipo                TEXT NOT NULL CHECK (tipo IN ('CREDITO', 'DEBITO')),
    categoria           TEXT NOT NULL,
    origem              TEXT NOT NULL,

    requer_revisao      INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (
        status IN ('PENDENTE', 'REVISADO', 'APROVADO', 'REJEITADO')
    ),
    criado_em           TEXT NOT NULL,

    PRIMARY KEY (tenant_id, lancamento_id),
    UNIQUE (tenant_id, chave_dedup),
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id),
    FOREIGN KEY (tenant_id, documento_id)
        REFERENCES documentos(tenant_id, documento_id)
);

CREATE INDEX IF NOT EXISTS idx_lancamentos_tenant_cliente_data
ON lancamentos (tenant_id, cliente_id, data_iso DESC);

CREATE INDEX IF NOT EXISTS idx_lancamentos_tenant_status
ON lancamentos (tenant_id, status, requer_revisao);

CREATE INDEX IF NOT EXISTS idx_documentos_tenant_cliente
ON documentos (tenant_id, cliente_id, recebido_em DESC);

CREATE TABLE IF NOT EXISTS quarentena (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL,
    cliente_id    TEXT NOT NULL,
    ts            TEXT NOT NULL,
    arquivo       TEXT,
    arquivo_hash  TEXT,
    item_json     TEXT,
    erro_json     TEXT
);

CREATE TABLE IF NOT EXISTS auditoria_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id             TEXT NOT NULL,
    usuario_id            TEXT,
    ts                    TEXT NOT NULL,
    evento                TEXT NOT NULL,
    cliente_id            TEXT,
    arquivo               TEXT,
    hash_sha256           TEXT,
    tipo                  TEXT,
    status                TEXT,
    total                 INTEGER,
    consentimento_externo INTEGER,
    destino               TEXT,
    detalhe               TEXT,
    hash_anterior         TEXT NOT NULL,
    hash_registro         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auditoria_tenant_data
ON auditoria_log (tenant_id, id DESC);

CREATE TRIGGER IF NOT EXISTS auditoria_log_sem_update
BEFORE UPDATE ON auditoria_log
BEGIN
    SELECT RAISE(ABORT, 'auditoria_log é append-only');
END;

CREATE TRIGGER IF NOT EXISTS auditoria_log_sem_delete
BEFORE DELETE ON auditoria_log
BEGIN
    SELECT RAISE(ABORT, 'auditoria_log é append-only');
END;
"""

CAMPOS_LOG = (
    "ts",
    "evento",
    "cliente_id",
    "arquivo",
    "hash_sha256",
    "tipo",
    "status",
    "total",
    "consentimento_externo",
    "destino",
    "detalhe",
)


# =============================================================
# CONEXÃO E TRANSAÇÃO
# =============================================================

def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transacao():
    conn = conectar()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def inicializar_banco():
    conn = conectar()
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


# =============================================================
# AUTENTICAÇÃO
# =============================================================

def hash_senha(senha: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        senha.encode(),
        bytes.fromhex(salt),
        PBKDF2_ITERACOES,
    ).hex()


def criar_usuario(tenant_id, email, nome, senha, papel="ADMIN"):
    salt = secrets.token_hex(16)
    senha_hash = hash_senha(senha, salt)

    with transacao() as conn:
        conn.execute(
            """
            INSERT INTO usuarios
            (usuario_id, tenant_id, email, nome, papel, senha_salt, senha_hash, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                email.lower(),
                nome,
                papel,
                salt,
                senha_hash,
                agora(),
            ),
        )


def autenticar(email: str, senha: str):
    conn = conectar()
    try:
        usuario = conn.execute(
            """
            SELECT u.usuario_id, u.tenant_id, u.nome, u.papel,
                   u.senha_salt, u.senha_hash, t.nome AS tenant_nome
            FROM usuarios u
            JOIN tenants t ON t.tenant_id = u.tenant_id
            WHERE u.email = ? AND u.ativo = 1
            """,
            (email.lower().strip(),),
        ).fetchone()
    finally:
        conn.close()

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
# AUDITORIA POR TENANT
# =============================================================

def _hash_registro(anterior: str, registro: dict) -> str:
    corpo = json.dumps(
        registro,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(
        f"{anterior}|{corpo}".encode("utf-8")
    ).hexdigest()


def registrar_log(
    conn: sqlite3.Connection,
    tenant_id: str,
    usuario_id: str | None,
    **campos,
):
    registro = {campo: campos.get(campo) for campo in CAMPOS_LOG}
    registro["ts"] = registro["ts"] or agora()

    if registro["total"] is not None:
        registro["total"] = int(registro["total"])

    if registro["consentimento_externo"] is not None:
        registro["consentimento_externo"] = int(
            bool(registro["consentimento_externo"])
        )

    anterior = conn.execute(
        """
        SELECT hash_registro
        FROM auditoria_log
        WHERE tenant_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()

    hash_anterior = anterior[0] if anterior else "GENESIS"

    hash_registro = _hash_registro(hash_anterior, registro)

    colunas = ", ".join(CAMPOS_LOG)
    marcadores = ", ".join("?" for _ in CAMPOS_LOG)

    conn.execute(
        f"""
        INSERT INTO auditoria_log
        (tenant_id, usuario_id, {colunas}, hash_anterior, hash_registro)
        VALUES (?, ?, {marcadores}, ?, ?)
        """,
        (
            tenant_id,
            usuario_id,
            *registro.values(),
            hash_anterior,
            hash_registro,
        ),
    )


def verificar_cadeia(tenant_id: str):
    conn = conectar()
    try:
        linhas = conn.execute(
            f"""
            SELECT id, {', '.join(CAMPOS_LOG)},
                   hash_anterior, hash_registro
            FROM auditoria_log
            WHERE tenant_id = ?
            ORDER BY id
            """,
            (tenant_id,),
        ).fetchall()
    finally:
        conn.close()

    anterior = "GENESIS"

    for id_, *valores, hash_ant, hash_reg in linhas:
        registro = dict(zip(CAMPOS_LOG, valores))

        if (
            hash_ant != anterior
            or _hash_registro(anterior, registro) != hash_reg
        ):
            return False, id_, len(linhas)

        anterior = hash_reg

    return True, None, len(linhas)


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


def sanitizar_erro(erro: Exception) -> str:
    return re.sub(r"[^ws.,-]", "", str(erro))[:180]


def normalizar_decimal(texto: str) -> str:
    texto = texto.strip().replace(" ", "")

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            return texto.replace(".", "").replace(",", ".")
        return texto.replace(",", "")

    return texto.replace(",", ".")


def valor_para_centavos(valor_str: str) -> int:
    dec = Decimal(normalizar_decimal(str(valor_str)))
    return int(
        (dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def formatar_brl(centavos: int) -> str:
    centavos = int(centavos)
    sinal = "-" if centavos < 0 else ""
    valor = abs(centavos)
    reais = f"{valor // 100:,}".replace(",", ".")
    return f"{sinal}R$ {reais},{valor % 100:02d}"


def decodificar_texto_ofx(bytes_arquivo: bytes) -> str:
    try:
        return bytes_arquivo.decode("utf-8")
    except UnicodeDecodeError:
        return bytes_arquivo.decode("cp1252", errors="replace")


def criar_lancamento(
    tenant_id: str,
    cliente_id: str,
    arquivo_hash: str,
    chave_dedup: str,
    data: str,
    descricao: str,
    valor_centavos: int,
    tipo: str,
    categoria: str,
    origem: str,
) -> dict:
    return {
        "lancamento_id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "cliente_id": cliente_id,
        "chave_dedup": chave_dedup,
        "arquivo_hash": arquivo_hash,
        "data": data,
        "descricao_original": html.unescape(descricao),
        "valor_centavos": abs(int(valor_centavos)),
        "tipo": tipo,
        "categoria": categoria,
        "origem": origem,
    }


# =============================================================
# VALIDAÇÃO PYDANTIC
# =============================================================

class LancamentoSchema(BaseModel):
    data: str
    descricao: str
    valor: float = Field(..., ge=0)
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
# PARSER OFX
# =============================================================

def parse_ofx(bytes_arquivo: bytes, arquivo_hash: str):
    lancamentos = []
    erros = []
    avisos = []
    chaves_locais = set()
    duplicados = 0

    try:
        conteudo = decodificar_texto_ofx(bytes_arquivo)
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
                    "deduplicação baseada em FITID/hash."
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
                    data_fmt = (
                        f"{data_bruta[6:8]}/"
                        f"{data_bruta[4:6]}/"
                        f"{data_bruta[0:4]}"
                    )
                    datetime.strptime(data_fmt, "%d/%m/%Y")

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
                            "data": data_fmt,
                            "descricao_original": descricao,
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

    except Exception as erro:
        return [], [f"Erro crítico no OFX: {erro}"], avisos, 0


# =============================================================
# GEMINI
# =============================================================

PROMPT_EXTRACAO = """
Analise este documento financeiro e extraia todos os lançamentos.
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
# PERSISTÊNCIA MULTIEMPRESA
# =============================================================

def inserir_lancamentos(
    conn: sqlite3.Connection,
    tenant_id: str,
    cliente_id: str,
    lancamentos: list[dict],
):
    inseridos = 0

    for lancamento in lancamentos:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO lancamentos
            (
                lancamento_id, tenant_id, cliente_id,
                chave_dedup, arquivo_hash, data, data_iso,
                descricao_original, valor_centavos, tipo,
                categoria, origem, requer_revisao, status, criado_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'PENDENTE', ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                cliente_id,
                lancamento["chave_dedup"],
                lancamento["arquivo_hash"],
                lancamento["data"],
                datetime.strptime(
                    lancamento["data"], "%d/%m/%Y"
                ).strftime("%Y-%m-%d"),
                lancamento["descricao_original"],
                lancamento["valor_centavos"],
                lancamento["tipo"],
                lancamento["categoria"],
                lancamento["origem"],
                agora(),
            ),
        )
        inseridos += cursor.rowcount

    return inseridos, len(lancamentos) - inseridos


def carregar_livro(tenant_id: str, cliente_id: str) -> pd.DataFrame:
    conn = conectar()
    try:
        df = pd.read_sql_query(
            """
            SELECT lancamento_id, arquivo_hash, data,
                   descricao_original, valor_centavos AS valor,
                   tipo, categoria, origem, requer_revisao
            FROM lancamentos
            WHERE tenant_id = ? AND cliente_id = ?
            ORDER BY data_iso, rowid
            """,
            conn,
            params=(tenant_id, cliente_id),
        )
    finally:
        conn.close()

    if df.empty:
        return df

    df["requer_revisao"] = df["requer_revisao"].astype(bool)
    df["valor"] = df["valor"].astype("int64")
    df["valor_assinado"] = np.where(
        df["tipo"] == "CREDITO",
        df["valor"],
        -df["valor"],
    )
    df["possivel_duplicata"] = df.duplicated(
        subset=["data", "valor", "tipo", "descricao_original"],
        keep=False,
    )
    return df


def carregar_log(tenant_id: str, limite: int = 300) -> pd.DataFrame:
    conn = conectar()
    try:
        return pd.read_sql_query(
            """
            SELECT id, ts, evento, arquivo, tipo, status,
                   total, destino, substr(hash_registro, 1, 12) AS hash_registro
            FROM auditoria_log
            WHERE tenant_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            conn,
            params=(tenant_id, limite),
        )
    finally:
        conn.close()


def carregar_quarentena(tenant_id: str, cliente_id: str) -> pd.DataFrame:
    conn = conectar()
    try:
        return pd.read_sql_query(
            """
            SELECT id, ts, arquivo, item_json, erro_json
            FROM quarentena
            WHERE tenant_id = ? AND cliente_id = ?
            ORDER BY id DESC
            """,
            conn,
            params=(tenant_id, cliente_id),
        )
    finally:
        conn.close()


def arquivo_ia_ja_processado(
    conn: sqlite3.Connection,
    tenant_id: str,
    cliente_id: str,
    arquivo_hash: str,
) -> bool:
    resultado = conn.execute(
        """
        SELECT 1
        FROM documentos
        WHERE tenant_id = ?
          AND cliente_id = ?
          AND arquivo_hash = ?
          AND status = 'PROCESSADO'
        """,
        (tenant_id, cliente_id, arquivo_hash),
    ).fetchone()

    return resultado is not None


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
    if not api_key:
        st.error("Credencial Gemini não configurada.")
        return

    client = genai.Client(api_key=api_key)

    with transacao() as conn:
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
                INSERT OR IGNORE INTO documentos
                (
                    documento_id, tenant_id, cliente_id, origem,
                    tipo_documento, nome_arquivo, arquivo_hash,
                    mime_type, tamanho_bytes, status, recebido_em
                )
                VALUES (?, ?, ?, 'UPLOAD', 'OUTRO', ?, ?, ?, ?, 'PROCESSANDO', ?)
                """,
                (
                    documento_id,
                    tenant_id,
                    cliente_id,
                    nome_arquivo,
                    arquivo_hash,
                    mime,
                    len(bytes_arquivo),
                    agora(),
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

                status = (
                    "PROCESSADO"
                    if not erros
                    else "QUARENTENA"
                )

                conn.execute(
                    """
                    UPDATE documentos
                    SET status = ?, processado_em = ?
                    WHERE tenant_id = ? AND documento_id = ?
                    """,
                    (status, agora(), tenant_id, documento_id),
                )

                registrar_log(
                    conn,
                    tenant_id=tenant_id,
                    usuario_id=usuario_id,
                    evento="IMPORTACAO_OFX",
                    cliente_id=cliente_id,
                    arquivo=nome_arquivo,
                    hash_sha256=arquivo_hash,
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
                    st.warning(
                        f"{nome_arquivo}: {erros[0]}"
                    )

            else:
                if arquivo_ia_ja_processado(
                    conn,
                    tenant_id,
                    cliente_id,
                    arquivo_hash,
                ):
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
                        INSERT INTO extracoes_ia
                        (extracao_id, tenant_id, documento_id,
                         modelo, resposta_json, status, criado_em)
                        VALUES (?, ?, ?, ?, ?, 'SUCESSO', ?)
                        """,
                        (
                            extracao_id,
                            tenant_id,
                            documento_id,
                            MODELO_GEMINI,
                            resposta.text,
                            agora(),
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
                                    "data": lancamento.data,
                                    "descricao_original": lancamento.descricao,
                                    "valor_centavos": valor_para_centavos(
                                        str(lancamento.valor)
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
                                INSERT INTO quarentena
                                (tenant_id, cliente_id, ts, arquivo,
                                 arquivo_hash, item_json, erro_json)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    tenant_id,
                                    cliente_id,
                                    agora(),
                                    nome_arquivo,
                                    arquivo_hash,
                                    json.dumps(
                                        item,
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                    json.dumps(
                                        erro.errors(include_url=False),
                                        ensure_ascii=False,
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

                    status_documento = (
                        "PROCESSADO"
                        if rejeitados == 0
                        else "QUARENTENA"
                    )

                    conn.execute(
                        """
                        UPDATE documentos
                        SET status = ?, processado_em = ?
                        WHERE tenant_id = ? AND documento_id = ?
                        """,
                        (
                            status_documento,
                            agora(),
                            tenant_id,
                            documento_id,
                        ),
                    )

                    registrar_log(
                        conn,
                        tenant_id=tenant_id,
                        usuario_id=usuario_id,
                        evento="EXTRACAO_IA",
                        cliente_id=cliente_id,
                        arquivo=nome_arquivo,
                        hash_sha256=arquivo_hash,
                        tipo="Gemini IA",
                        status=status_documento,
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
                        SET status = 'ERRO', processado_em = ?
                        WHERE tenant_id = ? AND documento_id = ?
                        """,
                        (agora(), tenant_id, documento_id),
                    )

                    registrar_log(
                        conn,
                        tenant_id=tenant_id,
                        usuario_id=usuario_id,
                        evento="EXTRACAO_IA_FALHA",
                        cliente_id=cliente_id,
                        arquivo=nome_arquivo,
                        hash_sha256=arquivo_hash,
                        tipo="Gemini IA",
                        status="ERRO",
                        total=0,
                        consentimento_externo=True,
                        destino="Google Gemini API",
                        detalhe=sanitizar_erro(erro),
                    )

                    st.error(
                        f"Erro ao processar {nome_arquivo}: "
                        f"{sanitizar_erro(erro)}"
                    )

    st.success("Processamento concluído e persistido.")


# =============================================================
# ESTADO E BOOTSTRAP
# =============================================================

def inicializar_estado():
    if "usuario" not in st.session_state:
        st.session_state["usuario"] = None

    if "cliente_id" not in st.session_state:
        st.session_state["cliente_id"] = None


inicializar_estado()
inicializar_banco()


# =============================================================
# LOGIN
# =============================================================

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


# =============================================================
# SELEÇÃO DE CLIENTE
# =============================================================

with st.sidebar:
    st.header("🏢 Escritório")
    st.write(f"**{usuario['tenant_nome']}**")
    st.write(f"Usuário: {usuario['nome']} ({usuario['papel']})")

    conn = conectar()
    clientes = conn.execute(
        """
        SELECT cliente_id, razao_social
        FROM clientes
        WHERE tenant_id = ? AND ativo = 1
        ORDER BY razao_social
        """,
        (tenant_id,),
    ).fetchall()
    conn.close()

    if not clientes:
        st.warning("Nenhum cliente cadastrado.")
        st.stop()

    cliente_selecionado = st.selectbox(
        "Cliente ativo",
        clientes,
        format_func=lambda c: c[1],
    )

    st.session_state["cliente_id"] = cliente_selecionado[0]
    cliente_id = st.session_state["cliente_id"]

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

# =============================================================
# DASHBOARD
# =============================================================

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

st.session_state["df_editor_base"] = df_tabela

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
    on_change=lambda: None,
)