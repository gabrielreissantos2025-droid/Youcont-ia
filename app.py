Import hashlib
import html
import json
import os
import re
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

st.set_page_config(
    page_title="Agente Contábil Profissional - Auditoria",
    page_icon="💼",
    layout="wide",
)

st.title("💼 Agente Contábil Profissional & Auditoria")
st.markdown(
    "Livro contábil persistido em SQLite, deduplicação por chave única, trilha de auditoria "
    "append-only com hash encadeado, quarentena de itens rejeitados e revisão humana registrada."
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
    ".png": b"\x89PNG\r\n\x1a\n",
    ".jpg": b"\xff\xd8\xff",
    ".jpeg": b"\xff\xd8\xff",
}

# =============================================================
# BANCO DE DADOS (SQLite)
# =============================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lancamentos (
    lancamento_id      TEXT PRIMARY KEY,
    chave_dedup        TEXT NOT NULL UNIQUE,
    arquivo_hash       TEXT NOT NULL,
    data               TEXT NOT NULL,
    data_iso           TEXT NOT NULL,
    descricao_original TEXT NOT NULL,
    valor_centavos     INTEGER NOT NULL CHECK (valor_centavos >= 0),
    tipo               TEXT NOT NULL CHECK (tipo IN ('CREDITO', 'DEBITO')),
    categoria          TEXT NOT NULL,
    origem             TEXT NOT NULL,
    requer_revisao     INTEGER NOT NULL DEFAULT 1,
    criado_em          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arquivos_ia (
    arquivo_hash  TEXT PRIMARY KEY,
    nome          TEXT,
    processado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarentena (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    arquivo      TEXT,
    arquivo_hash TEXT,
    item_json    TEXT,
    erro_json    TEXT
);

CREATE TABLE IF NOT EXISTS auditoria_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT NOT NULL,
    evento                TEXT NOT NULL,
    arquivo               TEXT,
    hash_sha256           TEXT,
    tipo                  TEXT,
    status                TEXT,
    total                 INTEGER,
    consentimento_externo INTEGER,
    destino               TEXT,
    detalhe               TEXT,
    usuario               TEXT,
    hash_anterior         TEXT NOT NULL,
    hash_registro         TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS auditoria_log_sem_update
BEFORE UPDATE ON auditoria_log
BEGIN SELECT RAISE(ABORT, 'auditoria_log é append-only'); END;

CREATE TRIGGER IF NOT EXISTS auditoria_log_sem_delete
BEFORE DELETE ON auditoria_log
BEGIN SELECT RAISE(ABORT, 'auditoria_log é append-only'); END;
"""

CAMPOS_LOG = (
    "ts",
    "evento",
    "arquivo",
    "hash_sha256",
    "tipo",
    "status",
    "total",
    "consentimento_externo",
    "destino",
    "detalhe",
    "usuario",
)


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
    """Transação com BEGIN IMMEDIATE: serializa escritas e mantém a cadeia de hash íntegra."""
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


def _hash_registro(anterior: str, registro: dict) -> str:
    corpo = json.dumps(registro, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{anterior}|{corpo}".encode("utf-8")).hexdigest()


def registrar_log(conn: sqlite3.Connection, **campos):
    """Acrescenta um registro à trilha, encadeado ao hash do registro anterior."""
    registro = {c: campos.get(c) for c in CAMPOS_LOG}
    registro["ts"] = registro["ts"] or agora()
    if registro["total"] is not None:
        registro["total"] = int(registro["total"])
    if registro["consentimento_externo"] is not None:
        registro["consentimento_externo"] = int(bool(registro["consentimento_externo"]))

    linha = conn.execute(
        "SELECT hash_registro FROM auditoria_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    anterior = linha[0] if linha else "GENESIS"
    hash_reg = _hash_registro(anterior, registro)

    colunas = ", ".join(CAMPOS_LOG) + ", hash_anterior, hash_registro"
    marcadores = ", ".join("?" for _ in range(len(CAMPOS_LOG) + 2))
    conn.execute(
        f"INSERT INTO auditoria_log ({colunas}) VALUES ({marcadores})",
        [*registro.values(), anterior, hash_reg],
    )


def verificar_cadeia() -> tuple[bool, int | None, int]:
    """Recalcula a cadeia. Retorna (íntegra, id do primeiro registro adulterado, total)."""
    conn = conectar()
    try:
        linhas = conn.execute(
            f"SELECT id, {', '.join(CAMPOS_LOG)}, hash_anterior, hash_registro "
            "FROM auditoria_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    anterior = "GENESIS"
    for id_, *valores, hash_ant, hash_reg in linhas:
        registro = dict(zip(CAMPOS_LOG, valores))
        if hash_ant != anterior or _hash_registro(anterior, registro) != hash_reg:
            return False, id_, len(linhas)
        anterior = hash_reg
    return True, None, len(linhas)


def inserir_lancamentos(conn: sqlite3.Connection, lancamentos: list[dict]) -> tuple[int, int]:
    """INSERT OR IGNORE: a chave UNIQUE faz a deduplicação. Retorna (inseridos, ignorados)."""
    inseridos = 0
    for l in lancamentos:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO lancamentos
            (lancamento_id, chave_dedup, arquivo_hash, data, data_iso, descricao_original,
             valor_centavos, tipo, categoria, origem, requer_revisao, criado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                l["lancamento_id"],
                l["chave_dedup"],
                l["arquivo_hash"],
                l["data"],
                datetime.strptime(l["data"], "%d/%m/%Y").strftime("%Y-%m-%d"),
                l["descricao_original"],
                l["valor"],
                l["tipo"],
                l["categoria"],
                l["origem"],
                agora(),
            ),
        )
        inseridos += cur.rowcount
    return inseridos, len(lancamentos) - inseridos


def arquivo_ia_ja_processado(arquivo_hash: str) -> bool:
    conn = conectar()
    try:
        return conn.execute(
            "SELECT 1 FROM arquivos_ia WHERE arquivo_hash = ?", (arquivo_hash,)
        ).fetchone() is not None
    finally:
        conn.close()


def carregar_livro() -> pd.DataFrame:
    conn = conectar()
    try:
        df = pd.read_sql_query(
            """
            SELECT lancamento_id, arquivo_hash, data, descricao_original,
                   valor_centavos AS valor, tipo, categoria, origem, requer_revisao
            FROM lancamentos ORDER BY data_iso, rowid
            """,
            conn,
        )
    finally:
        conn.close()

    if df.empty:
        return df

    df["requer_revisao"] = df["requer_revisao"].astype(bool)
    df["valor"] = df["valor"].astype("int64")
    df["valor_assinado"] = np.where(df["tipo"] == "CREDITO", df["valor"], -df["valor"])
    df["possivel_duplicata"] = df.duplicated(
        subset=["data", "valor", "tipo", "descricao_original"], keep=False
    )
    return df


def versao_livro() -> str:
    conn = conectar()
    try:
        n, maximo = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM lancamentos"
        ).fetchone()
    finally:
        conn.close()
    return f"{n}_{maximo}"


def carregar_log(limite: int = 500) -> pd.DataFrame:
    conn = conectar()
    try:
        return pd.read_sql_query(
            "SELECT id, ts, evento, arquivo, tipo, status, total, destino, usuario, "
            "substr(hash_registro, 1, 12) AS hash_registro "
            "FROM auditoria_log ORDER BY id DESC LIMIT ?",
            conn,
            params=(limite,),
        )
    finally:
        conn.close()


def carregar_quarentena() -> pd.DataFrame:
    conn = conectar()
    try:
        return pd.read_sql_query(
            "SELECT id, ts, arquivo, item_json, erro_json FROM quarentena ORDER BY id DESC",
            conn,
        )
    finally:
        conn.close()


# =============================================================
# FIM DO BLOCO DE BANCO
# =============================================================


def inicializar_estado():
    for chave, padrao in {"erro_revisao": None}.items():
        if chave not in st.session_state:
            st.session_state[chave] = padrao


inicializar_estado()
inicializar_banco()


# =============================================================
# VALIDAÇÃO PYDANTIC
# =============================================================

class LancamentoSchema(BaseModel):
    data: str = Field(..., description="Data no formato DD/MM/AAAA")
    descricao: str = Field(..., description="Descrição da transação")
    valor: float = Field(..., ge=0, description="Valor absoluto, positivo ou zero")
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
# UTILITÁRIOS E SEGURANÇA (LGPD & CSV INJECTION)
# =============================================================

def calcular_hash_arquivo(bytes_arquivo: bytes) -> str:
    return hashlib.sha256(bytes_arquivo).hexdigest()


def validar_conteudo(extensao: str, bytes_arquivo: bytes) -> bool:
    assinatura = MAGIC_BYTES.get(extensao)
    return assinatura is None or bytes_arquivo.startswith(assinatura)


def aplicar_mascara_lgpd(texto: str) -> str:
    if not isinstance(texto, str):
        return texto

    texto = re.sub(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", "***.***.***-**", texto)
    texto = re.sub(r"\b\d{11}\b", "***********", texto)
    texto = re.sub(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", "**.***.***/****-**", texto)
    texto = re.sub(r"\b\d{14}\b", "**************", texto)
    texto = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "****@****.***", texto)
    return texto


def neutralizar_csv_injection(texto: str) -> str:
    if isinstance(texto, str) and texto.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + texto
    return texto


def sanitizar_erro(exc: Exception) -> str:
    return re.sub(r"[^\w\s.,-]", "", str(exc))[:100]


def normalizar_decimal(texto: str) -> str:
    """Aceita '1234.56', '1234,56', '1.234,56' e '1,234.56'."""
    texto = texto.strip().replace(" ", "")
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            return texto.replace(".", "").replace(",", ".")
        return texto.replace(",", "")
    return texto.replace(",", ".")


def valor_para_centavos(valor_str: str) -> int:
    dec = Decimal(normalizar_decimal(valor_str))
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def centavos_para_float(centavos: int) -> float:
    return float(Decimal(int(centavos)) / Decimal(100))


def formatar_brl(centavos: int) -> str:
    sinal = "-" if centavos < 0 else ""
    c = abs(int(centavos))
    reais = f"{c // 100:,}".replace(",", ".")
    return f"{sinal}R$ {reais},{c % 100:02d}"


def decodificar_texto_ofx(bytes_arquivo: bytes) -> str:
    try:
        return bytes_arquivo.decode("utf-8")
    except UnicodeDecodeError:
        return bytes_arquivo.decode("cp1252", errors="replace")


def criar_lancamento(
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
        "chave_dedup": chave_dedup,
        "arquivo_hash": arquivo_hash,
        "data": data,
        "descricao_original": html.unescape(descricao),
        "valor": valor_centavos,
        "tipo": tipo,
        "categoria": categoria,
        "origem": origem,
    }


# =============================================================
# PARSER OFX (SGML v1 + XML v2, várias contas por arquivo)
# =============================================================

def parse_ofx(bytes_arquivo: bytes, arquivo_hash: str):
    """Retorna (lancamentos, erros, avisos, duplicados_no_arquivo)."""
    lancamentos: list[dict] = []
    erros: list[str] = []
    avisos: list[str] = []
    chaves_locais: set[str] = set()
    dups_locais = 0
    aviso_metadados = False

    try:
        conteudo = decodificar_texto_ofx(bytes_arquivo)
        corpo = conteudo[conteudo.index("<OFX>"):] if "<OFX>" in conteudo else conteudo

        blocos = [
            m.group(2)
            for m in re.finditer(r"<(STMTRS|CCSTMTRS)>(.*?)</\1>", corpo, re.DOTALL)
        ] or [corpo]

        indice = 0
        for bloco in blocos:
            bank_m = re.search(r"<BANKID>([^<\r\n]+)", bloco)
            acct_m = re.search(r"<ACCTID>([^<\r\n]+)", bloco)
            bank_id = bank_m.group(1).strip() if bank_m else "UNKNOWN_BANK"
            acct_id = acct_m.group(1).strip() if acct_m else "UNKNOWN_ACCT"

            if (bank_id.startswith("UNKNOWN_") or acct_id.startswith("UNKNOWN_")) and not aviso_metadados:
                aviso_metadados = True
                avisos.append(
                    "Metadados bancários incompletos (comum em cartão); "
                    "deduplicação baseada em FITID/hash de conteúdo."
                )

            transacoes = re.findall(
                r"<STMTTRN>(.*?)(?=<STMTTRN>|</BANKTRANLIST>|</STMTTRN>|$)",
                bloco,
                re.DOTALL,
            )
            ocorrencias: dict[str, int] = {}

            for transacao in transacoes:
                indice += 1
                try:
                    data_m = re.search(r"<DTPOSTED>(\d{8})", transacao)
                    valor_m = re.search(r"<TRNAMT>([^<\r\n]+)", transacao)
                    memo_m = re.search(r"<MEMO>([^<\r\n]+)", transacao)
                    nome_m = re.search(r"<NAME>([^<\r\n]+)", transacao)
                    fitid_m = re.search(r"<FITID>([^<\r\n]+)", transacao)

                    if not data_m or not valor_m:
                        raise ValueError("Transação sem DTPOSTED ou TRNAMT válido.")

                    data_bruta = data_m.group(1)
                    data_fmt = f"{data_bruta[6:8]}/{data_bruta[4:6]}/{data_bruta[0:4]}"
                    datetime.strptime(data_fmt, "%d/%m/%Y")

                    valor_bruto = valor_m.group(1).strip()
                    centavos = valor_para_centavos(valor_bruto)
                    tipo = "CREDITO" if centavos >= 0 else "DEBITO"

                    descricao = (
                        memo_m.group(1).strip()
                        if memo_m
                        else nome_m.group(1).strip()
                        if nome_m
                        else "Transação OFX"
                    )

                    if fitid_m:
                        chave = f"{bank_id}|{acct_id}|{fitid_m.group(1).strip()}"
                    else:
                        sig = f"{data_bruta}|{centavos}|{descricao}"
                        ocorrencias[sig] = ocorrencias.get(sig, 0) + 1
                        h = hashlib.sha256(
                            f"{bank_id}|{acct_id}|{sig}|{ocorrencias[sig]}".encode()
                        ).hexdigest()
                        chave = f"{bank_id}|{acct_id}|HASH-{h}"

                    if chave in chaves_locais:
                        dups_locais += 1
                        continue

                    categoria = "Outros"
                    desc_up = descricao.upper()
                    if "INVEST" in desc_up or re.search(r"\bB3\b", desc_up):
                        categoria = "Investimentos"
                    elif "IMPOSTO" in desc_up or "IOF" in desc_up:
                        categoria = "Impostos"

                    lancamentos.append(
                        criar_lancamento(
                            arquivo_hash=arquivo_hash,
                            chave_dedup=chave,
                            data=data_fmt,
                            descricao=descricao,
                            valor_centavos=abs(centavos),
                            tipo=tipo,
                            categoria=categoria,
                            origem="OFX local",
                        )
                    )
                    chaves_locais.add(chave)

                except Exception as erro:
                    erros.append(f"Transação {indice}: {erro}")

        return lancamentos, erros, avisos, dups_locais

    except Exception as erro:
        return [], [f"Erro crítico ao interpretar OFX: {erro}"], avisos, 0


# =============================================================
# EXTRAÇÃO POR IA (Gemini)
# =============================================================

PROMPT_EXTRACAO = """
Analise este documento financeiro e extraia todos os lançamentos com precisão absoluta.
Não invente lançamentos. Se não houver transações claras, retorne lista vazia.
Ignore linhas de saldo, totais e subtotais.
Datas no formato DD/MM/AAAA. O valor é sempre absoluto (positivo).
Saídas são DEBITO e entradas são CREDITO.
Respeite estritamente o esquema JSON solicitado.
"""


def chamar_gemini(client, bytes_data: bytes, mime_tipo: str):
    ultimo_erro = None
    for tentativa in range(3):
        try:
            return client.models.generate_content(
                model=MODELO_GEMINI,
                contents=[
                    types.Part.from_bytes(data=bytes_data, mime_type=mime_tipo),
                    PROMPT_EXTRACAO,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=RelatorioContabilSchema,
                    temperature=0.0,
                ),
            )
        except Exception as exc:
            ultimo_erro = exc
            if tentativa < 2:
                time.sleep(2 ** tentativa)
    raise ultimo_erro


# =============================================================
# PIPELINE POR ARQUIVO
# =============================================================

def novo_resultado(nome: str, arquivo_hash: str) -> dict:
    return {
        "arquivo": nome,
        "hash": arq