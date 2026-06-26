-- ============================================================
-- Game library & recommendation database - schema v1
-- Target: SQLite 3. Will migrate later to Postgres.
--
-- Core design goal: separate the CANONICAL game (one row per
-- real-world game) from each SOURCE's record of it. Adding a new
-- source later (Metacritic, GOG, Epic, ...) becomes new ROWS in
-- existing tables - never a schema change
-- ============================================================

PRAGMA foreign_keys = ON;   -- SQLite ignores foreign keys unless set PER CONNECTION

-- ------------------------------------------------------------
-- 1. SOURCES (Steam, OpenCritic, IGDB, and whatever we add later)
-- ------------------------------------------------------------
CREATE TABLE source (
    source_id    INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,   -- machine name: 'steam', 'opencritic', 'igdb'
    display_name TEXT NOT NULL,          -- human name:   'Steam', 'OpenCritic', 'IGDB'
    base_url     TEXT,
    notes        TEXT
);

-- ------------------------------------------------------------
-- 2. STAGING / LANDING ZONE
--    Raw API responses land here first, before we reconcile them
--    into canonical games. This is what lets us re-derive every
--    field later WITHOUT re-hitting a rate-limited API.
-- ------------------------------------------------------------
CREATE TABLE raw_fetch (
    raw_fetch_id     INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES source(source_id),
    source_native_id TEXT,               -- Steam AppID / IGDB id / OpenCritic id, if known
    endpoint         TEXT,               -- which API call produced this row
    http_status      INTEGER,
    payload          TEXT,               -- raw JSON stored verbatim; query later with json_extract()
    fetched_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_raw_fetch_lookup ON raw_fetch(source_id, source_native_id);

-- ------------------------------------------------------------
-- 3. CANONICAL GAME  (the "golden record")
-- ------------------------------------------------------------
CREATE TABLE game (
    game_id          INTEGER PRIMARY KEY,
    canonical_title  TEXT NOT NULL,       -- the title we choose to trust / display
    normalized_title TEXT NOT NULL,       -- lowercased, stripped of (TM)(R), editions, punctuation.
                                          -- half of our fallback match key
    release_year     INTEGER,             --    the other half of the (title, year) match key
    release_date     TEXT,                -- full date when known (ISO 8601: 'YYYY-MM-DD')
    summary          TEXT,
    cover_image_id   TEXT,                -- IGDB cover art id; build a URL from it for the UI:
                                          --    https://images.igdb.com/igdb/image/upload/t_cover_big/<id>.jpg
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_game_match ON game(normalized_title, release_year);

-- ------------------------------------------------------------
-- 4. GAME <-> SOURCE LINK
--    One row per (canonical game, source). Stores that source's
--    native id PLUS how we arrived at the match and how sure we are.
-- ------------------------------------------------------------
CREATE TABLE game_source_ref (
    ref_id           INTEGER PRIMARY KEY,
    game_id          INTEGER NOT NULL REFERENCES game(game_id) ON DELETE CASCADE,
    source_id        INTEGER NOT NULL REFERENCES source(source_id),
    source_native_id TEXT NOT NULL,       -- Steam AppID, OpenCritic id, IGDB id, ...
    source_url       TEXT,
    match_method     TEXT,                -- 'igdb_external', 'title+year', 'manual'
    match_confidence REAL,                -- 0.0-1.0; use 1.0 for a hard external ID like IGDB->Steam
    linked_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, source_native_id), -- a given source record maps to AT MOST one canonical game
    UNIQUE (game_id, source_id)           -- a canonical game has AT MOST one record per source
);

-- ------------------------------------------------------------
-- 5. SCORES  (normalized: every source's metrics share one shape)
--    score_type names the metric, so Steam percentages and
--    OpenCritic averages sit side by side without new columns.
-- ------------------------------------------------------------
CREATE TABLE game_score (
    score_id     INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL REFERENCES game(game_id) ON DELETE CASCADE,
    source_id    INTEGER NOT NULL REFERENCES source(source_id),
    score_type   TEXT NOT NULL,           -- e.g. 'igdb_critic_rating',
                                          --      'opencritic_top_critic',
                                          --      'steam_positive_pct'
    score_value  REAL,
    sample_count INTEGER,                 -- how many critics/users back this score
                                          --   (confidence: 85 from 2 != 85 from 200)
    captured_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (game_id, source_id, score_type)   -- one current value per metric; lets us upsert
);
CREATE INDEX idx_score_game ON game_score(game_id, source_id);

-- ------------------------------------------------------------
-- 6. ATTRIBUTES  (generic tags: genre, platform, theme, game_mode,
--    player_perspective, ... - adding a new KIND needs no schema change)
-- ------------------------------------------------------------
CREATE TABLE attribute (
    attribute_id INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,           -- 'genre','platform','theme','game_mode',
                                          --    'player_perspective','franchise','keyword'
    name         TEXT NOT NULL,
    UNIQUE (kind, name)
);

CREATE TABLE game_attribute (
    game_id      INTEGER NOT NULL REFERENCES game(game_id)           ON DELETE CASCADE,
    attribute_id INTEGER NOT NULL REFERENCES attribute(attribute_id) ON DELETE CASCADE,
    PRIMARY KEY (game_id, attribute_id)
);
CREATE INDEX idx_game_attribute_game ON game_attribute(game_id);

-- ------------------------------------------------------------
-- 7. COMPANIES  (developer / publisher - a label plus a role)
-- ------------------------------------------------------------
CREATE TABLE company (
    company_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE
);

CREATE TABLE game_company (
    game_id    INTEGER NOT NULL REFERENCES game(game_id)       ON DELETE CASCADE,
    company_id INTEGER NOT NULL REFERENCES company(company_id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('developer','publisher')),
    PRIMARY KEY (game_id, company_id, role)
);
CREATE INDEX idx_game_company_game ON game_company(game_id);

-- ------------------------------------------------------------
-- 8. USER LIBRARY / BACKLOG  (the stretch tracking half of the project)
--    Single-user for now. If it ever goes multi-user, add a `user`
--    table and a user_id column here.
-- ------------------------------------------------------------
CREATE TABLE user_game (
    user_game_id INTEGER PRIMARY KEY,
    game_id      INTEGER NOT NULL UNIQUE REFERENCES game(game_id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'want-to-play'
                   CHECK (status IN ('want-to-play','playing','played')),
                   -- add more later (e.g. 'abandoned') by extending this list
    user_rating  INTEGER CHECK (user_rating BETWEEN 1 AND 10),
    hours_played REAL,
    notes        TEXT,
    added_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- Sources we're starting with
-- ------------------------------------------------------------
INSERT INTO source (name, display_name, base_url) VALUES
    ('igdb',       'IGDB',       'https://api.igdb.com/v4'),
    ('steam',      'Steam',      'https://store.steampowered.com/api'),
    ('opencritic', 'OpenCritic', 'https://opencritic-api.p.rapidapi.com'),
    ('metacritic', 'Metacritic', 'https://www.metacritic.com');