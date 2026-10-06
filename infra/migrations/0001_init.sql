create extension if not exists "vector";

CREATE TABLE IF NOT EXISTS asset_sources (
  code TEXT PRIMARY KEY,
  label TEXT NOT NULL
);

INSERT INTO asset_sources(code, label)
VALUES ('eur-lex','EUR-Lex')
ON CONFLICT (code) DO UPDATE
SET label = EXCLUDED.label;

CREATE TABLE IF NOT EXISTS asset_types (
  code TEXT PRIMARY KEY,
  label TEXT NOT NULL
);

INSERT INTO asset_types(code, label)
VALUES ('legislation','Legislation')
ON CONFLICT (code) DO UPDATE
SET label = EXCLUDED.label;

CREATE TABLE IF NOT EXISTS assets (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  source              TEXT NOT NULL REFERENCES asset_sources(code),
  source_id           TEXT NOT NULL,
  variant             TEXT NOT NULL DEFAULT 'default', -- in case we need to store multiple versions of an asset
  language_code       TEXT NOT NULL DEFAULT 'en',
  source_uri          TEXT,
  source_published_at timestamptz,

  asset_type          TEXT NOT NULL REFERENCES asset_types(code),
  title               TEXT,
  asset_text          TEXT,
  storage_uri         TEXT,
  content_hash        TEXT,

  CONSTRAINT uq_asset UNIQUE (source, source_id, variant, language_code)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_assets_id_asset_type ON assets (id, asset_type);

CREATE TABLE IF NOT EXISTS defined_terms (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_id        BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  term            TEXT NOT NULL,
  definition      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_id        BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  article_number  INTEGER NOT NULL,
  article_text    TEXT NOT NULL,

  CONSTRAINT uq_article UNIQUE (asset_id, article_number)
);

CREATE TABLE IF NOT EXISTS article_relations (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_article_id     BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  to_article_id       BIGINT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  relation_type       TEXT NOT NULL  CHECK (relation_type IN ('references'))
);

CREATE INDEX IF NOT EXISTS ix_article_relations_from ON article_relations (from_article_id);
CREATE INDEX IF NOT EXISTS ix_article_relations_to ON article_relations (to_article_id);

CREATE TABLE IF NOT EXISTS obligations (
  id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id                  UUID NOT NULL,
  source                  TEXT NOT NULL,
  article_id              BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  polarity                TEXT,
  obligation              TEXT NOT NULL,
  obligation_references   JSONB NOT NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),

  CHECK (polarity IS NULL OR polarity IN ('positive', 'negative')),
  CHECK ((source = 'asset'   AND article_id IS NOT NULL) OR
         (source = 'derived' AND article_id IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_obligations_run_id ON obligations (run_id);

CREATE TABLE IF NOT EXISTS obligation_relations (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  from_obligation_id  BIGINT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
  to_obligation_id    BIGINT NOT NULL REFERENCES obligations(id) ON DELETE CASCADE,
  relation_type       TEXT NOT NULL CHECK (relation_type IN ('overlaps', 'derives')),
  reasoning           TEXT,
  scores              JSONB
);

CREATE INDEX IF NOT EXISTS ix_obligation_relations_from ON obligation_relations (from_obligation_id);
CREATE INDEX IF NOT EXISTS ix_obligation_relations_to ON obligation_relations (to_obligation_id);

CREATE TABLE IF NOT EXISTS assets_legislation (
  asset_id          BIGINT PRIMARY KEY,
  asset_type        TEXT GENERATED ALWAYS AS ('legislation') STORED REFERENCES asset_types(code),
  legislation_type  TEXT NOT NULL,

  CONSTRAINT ck_assets_legislation_asset_type CHECK (asset_type = 'legislation'),
  CONSTRAINT fk_legislation_asset FOREIGN KEY (asset_id, asset_type)
    REFERENCES assets (id, asset_type) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_assets_id_source ON assets (id, source);

CREATE TABLE IF NOT EXISTS assets_eur_lex (
  asset_id          BIGINT PRIMARY KEY,
  source            TEXT GENERATED ALWAYS AS ('eur-lex') STORED REFERENCES asset_sources(code),
  celex_id          TEXT NOT NULL UNIQUE,
  celex_base_id     TEXT NOT NULL,
  celex_version     TEXT,
  celex_sector      TEXT NOT NULL,
  celex_type        TEXT NOT NULL,

  CONSTRAINT fk_eur_lex_asset FOREIGN KEY (asset_id, source)
    REFERENCES assets (id, source) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS asset_parts (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  asset_id           BIGINT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,

  part_index         INTEGER NOT NULL,
  article_number     INTEGER,
  part_number        INTEGER,
  part_tag           TEXT NOT NULL,

  part_text_raw      TEXT NOT NULL,
  part_text          TEXT NOT NULL,
  language_code      TEXT NOT NULL DEFAULT 'en',

  text_length        INTEGER GENERATED ALWAYS AS (char_length(part_text)) STORED,
  created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ux_asset_parts_asset_id ON asset_parts (asset_id);
