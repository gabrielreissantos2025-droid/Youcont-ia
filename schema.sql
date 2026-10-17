CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nome TEXT NOT NULL,
    cnpj TEXT UNIQUE,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    ativo BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS usuarios (
    usuario_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    email TEXT NOT NULL,
    nome TEXT NOT NULL,
    papel TEXT NOT NULL CHECK (
        papel IN ('ADMIN', 'CONTADOR', 'ASSISTENTE', 'LEITURA')
    ),
    senha_salt TEXT NOT NULL,
    senha_hash TEXT NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    ativo BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS clientes (
    cliente_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(tenant_id),
    razao_social TEXT NOT NULL,
    nome_fantasia TEXT,
    cnpj TEXT,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    ativo BOOLEAN NOT NULL DEFAULT true,
    UNIQUE (tenant_id, cnpj)
);

CREATE TABLE IF NOT EXISTS usuario_clientes (
    tenant_id UUID NOT NULL,
    usuario_id UUID NOT NULL REFERENCES usuarios(usuario_id),
    cliente_id UUID NOT NULL,

    PRIMARY KEY (tenant_id, usuario_id, cliente_id),
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id)
);

CREATE TABLE IF NOT EXISTS documentos (
    documento_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    cliente_id UUID NOT NULL,
    origem TEXT NOT NULL CHECK (
        origem IN ('UPLOAD', 'WHATSAPP', 'EMAIL', 'API')
    ),
    tipo_documento TEXT NOT NULL CHECK (
        tipo_documento IN (
            'EXTRATO', 'NFE', 'RECIBO', 'COMPROVANTE',
            'INFORME_RENDIMENTOS', 'FATURA', 'OUTRO'
        )
    ),
    nome_arquivo TEXT NOT NULL,
    arquivo_hash TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    tamanho_bytes BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RECEBIDO' CHECK (
        status IN ('RECEBIDO', 'PROCESSANDO', 'PROCESSADO', 'ERRO', 'QUARENTENA')
    ),
    recebido_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    processado_em TIMESTAMPTZ,

    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id),
    UNIQUE (tenant_id, arquivo_hash)
);

CREATE TABLE IF NOT EXISTS extracoes_ia (
    extracao_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    documento_id UUID NOT NULL,
    modelo TEXT NOT NULL,
    resposta_json JSONB NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('SUCESSO', 'PARCIAL', 'FALHA')
    ),
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),

    FOREIGN KEY (tenant_id, documento_id)
        REFERENCES documentos(tenant_id, documento_id)
);

CREATE TABLE IF NOT EXISTS lancamentos (
    tenant_id UUID NOT NULL,
    cliente_id UUID NOT NULL,
    lancamento_id UUID NOT NULL,

    chave_dedup TEXT NOT NULL,
    arquivo_hash TEXT NOT NULL,
    data_iso DATE NOT NULL,
    descricao_original TEXT NOT NULL,

    valor_centavos BIGINT NOT NULL CHECK (valor_centavos >= 0),
    tipo TEXT NOT NULL CHECK (tipo IN ('CREDITO', 'DEBITO')),
    categoria TEXT NOT NULL,
    origem TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (
        status IN ('PENDENTE', 'REVISADO', 'APROVADO', 'REJEITADO')
    ),
    requer_revisao BOOLEAN NOT NULL DEFAULT true,

    valor_assinado_centavos BIGINT GENERATED ALWAYS AS (
        CASE
            WHEN tipo = 'CREDITO' THEN valor_centavos
            ELSE -valor_centavos
        END
    ) STORED,

    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, lancamento_id),
    UNIQUE (tenant_id, chave_dedup),
    FOREIGN KEY (tenant_id, cliente_id)
        REFERENCES clientes(tenant_id, cliente_id)
);

CREATE INDEX IF NOT EXISTS idx_lancamentos_tenant_cliente_data
ON lancamentos (tenant_id, cliente_id, data_iso DESC);

CREATE INDEX IF NOT EXISTS idx_lancamentos_tenant_status
ON lancamentos (tenant_id, status, requer_revisao);

CREATE INDEX IF NOT EXISTS idx_documentos_tenant_cliente
ON documentos (tenant_id, cliente_id, recebido_em DESC);

CREATE TABLE IF NOT EXISTS quarentena (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id UUID NOT NULL,
    cliente_id UUID NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    arquivo TEXT,
    arquivo_hash TEXT,
    item_json JSONB,
    erro_json JSONB
);

CREATE TABLE IF NOT EXISTS auditoria_log (
    tenant_id UUID NOT NULL,
    log_id BIGINT GENERATED ALWAYS AS IDENTITY,
    usuario_id UUID REFERENCES usuarios(usuario_id),
    cliente_id UUID,
    evento TEXT NOT NULL,
    arquivo TEXT,
    arquivo_hash TEXT,
    tipo TEXT,
    status TEXT,
    total INTEGER,
    consentimento_externo BOOLEAN,
    destino TEXT,
    detalhe TEXT,
    hash_anterior TEXT NOT NULL,
    hash_registro TEXT NOT NULL,
    criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, log_id)
);

CREATE INDEX IF NOT EXISTS idx_auditoria_tenant_data
ON auditoria_log (tenant_id, log_id DESC);