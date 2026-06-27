"""
rank.py - local CLI for querying the game database.

Also the reference implementation for web/app.py: shows exactly how to call
db.ranked_games() and the helper functions (db.genres_for, db.attributes_for),
what the result rows look like, and how to drive the filters.

Blended score:
    critic = average of the usable critic sources:
               - IGDB critic rating, when it clears the min-review-count floor
               - Metacritic score, when present (no review count, always averaged in)
             With both: straight 50/50. With one: that one alone.
    user   = count-weighted average of Steam %-positive and IGDB user rating
    final  = (critic + user) / 2

A game needs both a critic score and a user score to appear in results.

Usage:
    python rank.py                          # top 50, critic needs >= 5 reviews
    python rank.py 100                      # top 100
    python rank.py 100 0                    # no minimum critic-review count (noisier)
    python rank.py 100 10                   # critic score must have >= 10 reviews
    python rank.py 100 5 50                 # also require >= 50 combined user ratings
    python rank.py 100 5 50 --steam         # ...and only games with a Steam store page

    python rank.py --search "hades"         # find a game by name and show its score
    python rank.py --search "civ" 50 0      # ...with no critic-review floor, if it's hiding

    python rank.py 1000 5 --steam           # top 1000 Steam-page games
    python rank.py 1000 5 --out top.txt     # write to a UTF-8 file instead of the terminal

    python rank.py 100 --popular --min-score 80     # 100 most-rated games scoring >= 80
    python rank.py 50  --popular --min-score 85 --steam

    python rank.py --genres                         # list every genre with game counts, then exit
    python rank.py 100 --exclude "Sports, Racing"   # exclude games in those genres
    python rank.py 100 --min-critic-score 80        # only games whose critic score is >= 80
    python rank.py 100 --min-user-score 85          # only games whose user score is >= 85

Args:
    [limit]              how many games to show (default: 50)
    [min_critic_reviews] IGDB critic score is dropped from the average if its review
                         count is below this floor (default: 5). db.ranked_games()
                         defaults this to 0 - the 5 here is a CLI convenience, since
                         the IGDB critic signal is noisy at low counts. Don't "fix"
                         this apparent mismatch.
    [min_user_ratings]   minimum combined user rating count, Steam + IGDB (default: 0)

    --search "name"      show only games whose title contains "name" (case-insensitive).
                         Only finds games that already have both scores; if a game
                         you expect is missing, try a lower critic-review floor, e.g.
                         --search "name" 50 0
    --steam              restrict to games with a Steam store page
    --popular            sort by combined user rating count instead of blended score
    --min-score N        exclude games whose final blended score is below N
    --min-critic-score N exclude games whose critic score is below N
    --min-user-score N   exclude games whose user score is below N
    --exclude "A,B,C"    exclude games in any of these genres (comma-separated,
                         case-insensitive; check spellings with --genres first)
    --out FILE           write output to FILE (UTF-8) instead of the terminal
    --genres             list every genre with game counts, then exit;
                         all other flags are ignored when this is passed
"""
import sys
import db


def main():
    args = sys.argv[1:]
    steam_only = "--steam" in args
    sort_by = "popularity" if "--popular" in args else "score"

    def take_value(flag):
        """Return the value after a valued flag (e.g. --out FILE), or None."""
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                return args[i + 1]
            sys.exit(f"error: {flag} needs a value")
        return None

    # --genres: just list every genre across all sources, then exit.
    if "--genres" in args:
        conn = db.get_connection()
        rows = db.all_genres(conn)
        print(f"\n{len(rows)} genres across all sources (name - games):\n")
        for r in rows:
            print(f"  {r['n_games']:>6,}  {r['name']}")
        conn.close()
        sys.exit(0)

    out_path      = take_value("--out")
    search_query  = take_value("--search")
    min_score_str = take_value("--min-score")
    min_score     = float(min_score_str) if min_score_str is not None else 0
    min_cscore_str = take_value("--min-critic-score")
    min_critic_score = float(min_cscore_str) if min_cscore_str is not None else 0
    min_uscore_str = take_value("--min-user-score")
    min_user_score = float(min_uscore_str) if min_uscore_str is not None else 0
    exclude_str   = take_value("--exclude")
    exclude_genres = [g.strip() for g in exclude_str.split(",") if g.strip()] if exclude_str else None

    # First collect every value already claimed by a flag.
    # The remaining numbers are assumed to be positionals.
    skip = {v for v in (out_path, search_query, min_score_str, min_cscore_str,
                        min_uscore_str, exclude_str) if v is not None}
    nums = [a for a in args if a not in skip and a.lstrip("-").isdigit()]

    limit       = int(nums[0]) if len(nums) > 0 else 50
    min_critics = int(nums[1]) if len(nums) > 1 else 5
    min_users   = int(nums[2]) if len(nums) > 2 else 0

    conn = db.get_connection()
    rows = db.ranked_games(conn, min_critic_count=min_critics,
                        min_user_count=min_users, limit=limit,
                        steam_only=steam_only, sort_by=sort_by, min_score=min_score,
                        exclude_genres=exclude_genres, min_critic_score=min_critic_score,
                        min_user_score=min_user_score, title_search=search_query)

    out = open(out_path, "w", encoding="utf-8") if out_path else sys.stdout

    sort_label = "popularity (most-rated)" if sort_by == "popularity" else "blended score"
    notes = [f"critic >= {min_critics} reviews", f"user >= {min_users} ratings"]
    if search_query:
        notes.append(f'matching "{search_query}"')
    if min_score:
        notes.append(f"score >= {min_score:g}")
    if min_critic_score:
        notes.append(f"critic score >= {min_critic_score:g}")
    if min_user_score:
        notes.append(f"user score >= {min_user_score:g}")
    if steam_only:
        notes.append("Steam store pages only")
    if exclude_genres:
        notes.append("excluding " + ", ".join(exclude_genres))
    print(f"\nTop {len(rows)} by {sort_label}  ({', '.join(notes)})\n", file=out)

    for i, r in enumerate(rows, 1):
        year = f" ({r['release_year']})" if r["release_year"] else ""
        genres = ", ".join(db.genres_for(conn, r["game_id"]))
        platforms = ", ".join(db.attributes_for(conn, r["game_id"], "platform"))
        franchises = ", ".join(db.attributes_for(conn, r["game_id"], "franchise"))

        # critic-side breakdown: igdb counts only if it cleared the threshold; metacritic always
        cparts = []
        if r["igdb_critic"] is not None and (r["igdb_critic_n"] or 0) >= min_critics:
            cparts.append(f"igdb {r['igdb_critic']:.0f} ({r['igdb_critic_n'] or 0})")
        if r["metacritic"] is not None:
            cparts.append(f"mc {r['metacritic']:.0f}")
        critic_breakdown = " + ".join(cparts)

        # user-side breakdown from whichever sources are present
        uparts = []
        if r["steam_user"] is not None:
            uparts.append(f"steam {r['steam_user']:.0f} ({r['steam_user_n']:,})")
        if r["igdb_user"] is not None:
            uparts.append(f"igdb {r['igdb_user']:.0f} ({r['igdb_user_n'] or 0:,})")
        user_breakdown = " + ".join(uparts)

        print(f"{i:>3}. {r['final_score']:5.1f}  {r['canonical_title']}{year}", file=out)
        print(f"     critic {r['critic_score']:.0f}  [{critic_breakdown}]"
            f"   user {r['user_score']:.0f}  [{user_breakdown}]", file=out)
        print(f"     {genres or '-'}", file=out)
        if franchises:
            print(f"     series: {franchises}", file=out)
        if platforms:
            print(f"     platforms: {platforms}", file=out)

    if out_path:
        out.close()
        print(f"Wrote {len(rows)} games to {out_path}")

    conn.close()


if __name__ == "__main__":
    main()