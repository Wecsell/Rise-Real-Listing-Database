CREATE TABLE IF NOT EXISTS messages (
    id BIGINT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    sender_id BIGINT,
    text TEXT,
    has_media BOOLEAN,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    processed BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS extractions (
    id SERIAL PRIMARY KEY,
    message_id BIGINT REFERENCES messages(id),
    project_recid VARCHAR(50),
    object_guess VARCHAR(255),
    confidence REAL,
    slot VARCHAR(50),
    url_status VARCHAR(20),
    why TEXT,
    needs_human BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS facts (
    id SERIAL PRIMARY KEY,
    project_recid VARCHAR(50) NOT NULL,
    unit_id VARCHAR(50),
    old_value TEXT,
    new_value TEXT,
    fact_type VARCHAR(50),
    source_message_id BIGINT,
    model_used VARCHAR(50),
    tokens_in INT,
    tokens_out INT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
