-- prepare_snapshot.sql - one-time finishing pass on gamedb.sqlite.
--
-- Run against the snapshot BEFORE it goes into a container image, since the
-- image opens it read-only:
--
--     sqlite3 gamedb.sqlite < prepare_snapshot.sql
--
-- Precomputes the genre list. app.py notes that all_genres() "aggregates
-- millions of rows (~10s)" and caches it for six hours - but on a frozen
-- dataset the answer is fixed forever, so computing it even once is waste.
-- Cloud Run scales to zero, so without this the first visitor after an idle
-- period pays that aggregate on top of the cold start. Precomputed, it is a
-- ~90-row table read.

DROP TABLE IF EXISTS genre_counts;

CREATE TABLE genre_counts AS
SELECT a.name AS name, COUNT(DISTINCT ga.game_id) AS n_games
FROM attribute a
JOIN game_attribute ga ON ga.attribute_id = a.attribute_id
WHERE a.kind IN ('genre', 'steam_genre')
GROUP BY a.name;

CREATE INDEX idx_genre_counts ON genre_counts(n_games DESC, name);

INSERT OR REPLACE INTO snapshot_meta (key, value)
VALUES ('prepared', 'genre_counts materialized');

ANALYZE;
VACUUM;

SELECT 'genres precomputed: ' || COUNT(*) FROM genre_counts;
