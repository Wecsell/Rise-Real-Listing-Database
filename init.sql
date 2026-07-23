CREATE TABLE IF NOT EXISTS messages (
    id BIGINT,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT DEFAULT 0,
    text TEXT,
    has_media BOOLEAN,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);

CREATE TABLE IF NOT EXISTS extractions (
    id SERIAL PRIMARY KEY,
    message_id BIGINT DEFAULT 0,
    chat_id BIGINT DEFAULT 0,
    project_recid VARCHAR(255),
    object_guess VARCHAR(255),
    confidence REAL,
    slot VARCHAR(50),
    url_status VARCHAR(50),
    why TEXT,
    needs_human BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_extractions_project ON extractions(project_recid);

CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    project_recid VARCHAR(255) NOT NULL,
    unit_id VARCHAR(100),
    old_value TEXT,
    new_value TEXT,
    fact_type VARCHAR(50),
    source_message_id BIGINT,
    model_used VARCHAR(50),
    tokens_in INT,
    tokens_out INT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
