import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool
from streamlit import secrets


POOL = ConnectionPool(
    conninfo=(
        f"host={secrets['POSTGRES_HOST']} "
        f"port={secrets['POSTGRES_PORT']} "
        f"dbname={secrets['POSTGRES_DB']} "
        f"user={secrets['POSTGRES_USER']} "
        f"password={secrets['POSTGRES_PASSWORD']}"
    ),
    min_size=1,
    max_size=10,
    open=True,
)


def agora() -> datetime:
    return datetime.now()


@contextmanager
def transacao(tenant_id: str):
    """
    Abre uma conexão do pool, inicia transação e define o tenant.
    O contexto é revertido automaticamente em caso de erro.
    """
    with POOL.connection() as conn:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.current_tenant_id', %s, true)",
                (tenant_id,),
            )
            yield conn


def inicializar_banco():
    schema = open("schema.sql", "r", encoding="utf-8").read()

    with POOL.connection() as conn:
        conn.execute(schema)


def hash_senha(senha: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        senha.encode(),
        bytes.fromhex(salt),
        390_000,
    ).hex()


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
    conn,
    tenant_id: str,
    usuario_id: str,
    **campos,
):
    campos_log = (
        "evento",
        "cliente_id",
        "arquivo",
        "arquivo_hash",
        "tipo",
        "status",
        "total",
        "consentimento_externo",
        "destino",
        "detalhe",
    )

    registro = {campo: campos.get(campo) for campo in campos_log}

    if registro["total"] is not None:
        registro["total"] = int(registro["total"])

    if registro["consentimento_externo"] is not None:
        registro["consentimento_externo"] = bool(
            registro["consentimento_externo"]
        )

    anterior = conn.execute(
        """
        SELECT hash_registro
        FROM auditoria_log
        WHERE tenant_id = %s
        ORDER BY log_id DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()

    hash_anterior = anterior[0] if anterior else "GENESIS"
    hash_registro = _hash_registro(hash_anterior, registro)

    conn.execute(
        """
        INSERT INTO auditoria_log (
            tenant_id,
            usuario_id,
            evento,
            cliente_id,
            arquivo,
            arquivo_hash,
            tipo,
            status,
            total,
            consentimento_externo,
            destino,
            detalhe,
            hash_anterior,
            hash_registro
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
        """,
        (
            tenant_id,
            usuario_id,
            registro["evento"],
            registro["cliente_id"],
            registro["arquivo"],
            registro["arquivo_hash"],
            registro["tipo"],
            registro["status"],
            registro["total"],
            registro["consentimento_externo"],
            registro["destino"],
            registro["detalhe"],
            hash_anterior,
            hash_registro,
        ),
    )


def verificar_cadeia(tenant_id: str):
    with POOL.connection() as conn:
        linhas = conn.execute(
            """
            SELECT log_id, evento, cliente_id, arquivo, arquivo_hash,
                   tipo, status, total, consentimento_externo,
                   destino, detalhe, hash_anterior, hash_registro
            FROM auditoria_log
            WHERE tenant_id = %s
            ORDER BY log_id
            """,
            (tenant_id,),
        ).fetchall()

    anterior = "GENESIS"

    for linha in linhas:
        (
            log_id,
            evento,
            cliente_id,
            arquivo,
            arquivo_hash,
            tipo,
            status,
            total,
            consentimento,
            destino,
            detalhe,
            hash_anterior,
            hash_registro,
        ) = linha

        registro = {
            "evento": evento,
            "cliente_id": cliente_id,
            "arquivo": arquivo,
            "arquivo_hash": arquivo_hash,
            "tipo": tipo,
            "status": status,
            "total": total,
            "consentimento_externo": consentimento,
            "destino": destino,
            "detalhe": detalhe,
        }

        calculado = _hash_registro(anterior, registro)

        if hash_anterior != anterior or calculado != hash_registro:
            return False, log_id, len(linhas)

        anterior = hash_registro

    return True, None, len(linhas)


def normalizar_decimal(texto: str) -> str:
    texto = str(texto).strip().replace(" ", "")

    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            return texto.replace(".", "").replace(",", ".")
        return texto.replace(",", "")

    return texto.replace(",", ".")


def valor_para_centavos(valor) -> int:
    dec = Decimal(normalizar_decimal(valor))
    return int(
        (dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )


def formatar_brl(centavos: int) -> str:
    centavos = int(centavos)
    sinal = "-" if centavos < 0 else ""
    valor = abs(centavos)
    reais = f"{valor // 100:,}".replace(",", ".")
    return f"{sinal}R$ {reais},{valor % 100:02d}"