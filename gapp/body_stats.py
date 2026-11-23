"""Сколько полных текстов сохранено и сколько места они занимают: `python -m gapp.body_stats`."""
import shutil

from . import config, db


def mb(n: float) -> str:
    return f"{n / 1024 / 1024:,.1f} МБ".replace(",", " ")


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
        days = conn.execute(
            "SELECT substr(fetched_at, 1, 10) AS day, COUNT(*) AS n, "
            "SUM(body != '') AS with_body, "
            "COALESCE(SUM(LENGTH(CAST(body AS BLOB))), 0) AS body_bytes, "
            "COALESCE(SUM(LENGTH(CAST(media AS BLOB))), 0) AS media_bytes, "
            "COALESCE(SUM(LENGTH(CAST(title || summary AS BLOB))), 0) AS other_bytes "
            "FROM articles GROUP BY day ORDER BY day DESC LIMIT 7"
        ).fetchall()
    print("За последние двое суток:")
    print(f"{'источник':<22} {'язык':<4} {'статей':>6} {'с текстом':>9} {'запрет':>6} "
          f"{'не вышло':>8} {'в очереди':>9} {'знаков':>6}")
    for r in rows:
        print(f"{r['name']:<22} {r['lang']:<4} {r['total']:>6} {r['saved'] or 0:>9} "
              f"{r['blocked'] or 0:>6} {r['failed'] or 0:>8} {r['waiting'] or 0:>9} {r['avg_len'] or 0:>6}")
    print("\nМесто в базе по дням (по дате получения статьи):")
    print(f"{'день':<11} {'статей':>7} {'с текстом':>9} {'текст':>10} {'фото/видео':>11} {'заголовки':>10}")
    for d in days:
        print(f"{d['day']:<11} {d['n']:>7} {d['with_body'] or 0:>9} {mb(d['body_bytes']):>10} "
              f"{mb(d['media_bytes']):>11} {mb(d['other_bytes']):>10}")
    path = config.DB_PATH
    size = sum(p.stat().st_size for p in path.parent.glob(path.name + "*") if p.is_file())
    disk = shutil.disk_usage(path.parent)
    print(f"\nФайл базы: {mb(size)}. Диск: занято {mb(disk.used)} из {mb(disk.total)}, свободно {mb(disk.free)}.")


if __name__ == "__main__":
    main()
