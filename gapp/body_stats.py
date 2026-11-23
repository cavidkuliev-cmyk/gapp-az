"""Сколько полных текстов сохранено по источникам: `python -m gapp.body_stats`."""
from . import db


def main() -> None:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT s.name, s.lang, COUNT(*) AS total, "
            "SUM(a.body != '') AS saved, SUM(a.body_status = 'blocked') AS blocked, "
            "SUM(a.body_status = 'failed') AS failed, SUM(a.body_status = '') AS waiting, "
            "CAST(AVG(CASE WHEN a.body != '' THEN LENGTH(a.body) END) AS INTEGER) AS avg_len "
            "FROM articles a JOIN sources s ON s.id = a.source_id "
            "WHERE a.published_at >= datetime('now', '-2 days') "
            "GROUP BY s.id ORDER BY s.lang DESC, s.name"
        ).fetchall()
    print("За последние двое суток:")
    print(f"{'источник':<22} {'язык':<4} {'статей':>6} {'с текстом':>9} {'запрет':>6} "
          f"{'не вышло':>8} {'в очереди':>9} {'знаков':>6}")
    for r in rows:
        print(f"{r['name']:<22} {r['lang']:<4} {r['total']:>6} {r['saved'] or 0:>9} "
              f"{r['blocked'] or 0:>6} {r['failed'] or 0:>8} {r['waiting'] or 0:>9} {r['avg_len'] or 0:>6}")


if __name__ == "__main__":
    main()
