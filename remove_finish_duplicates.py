"""Cleanup script for duplicate finish_records in records.db

Usage:
  python remove_finish_duplicates.py [--db PATH] [--keep {first,latest_upload}] [--dry-run]

Behavior:
 - Finds groups of finish_records that share the same (name, races, difficulty) and have count > 1.
 - Keeps one record per group (default: the row with smallest id). With --keep latest_upload, keeps the record with the newest uploads.uploaded_at timestamp.
 - Deletes the other rows from finish_records.
 - With --dry-run, prints what would be deleted but does not modify the database.

This is intended as a one-off fix script. Make a backup of your database before running.
"""
import argparse
import sqlite3
import os
import sys

DEFAULT_DB = os.path.join(os.path.dirname(__file__), 'records.db')


def parse_args():
    p = argparse.ArgumentParser(description='Remove duplicate finish_records (same name+races+difficulty)')
    p.add_argument('--db', '-d', default=DEFAULT_DB, help='Path to the sqlite database')
    p.add_argument('--keep', choices=('first', 'latest_upload'), default='first',
                   help='Which record to keep when duplicates found (default: first by id)')
    p.add_argument('--dry-run', action='store_true', help="Don't delete anything, just show what would be removed")
    return p.parse_args()


def find_duplicate_groups(cur):
    # Groups by name, races, difficulty (NULLs are grouped together in sqlite)
    cur.execute('''
        SELECT name, races, difficulty, COUNT(*) as cnt
        FROM finish_records
        GROUP BY name, races, difficulty
        HAVING cnt > 1
    ''')
    return cur.fetchall()


def get_group_ids(cur, name, races, difficulty, order_by_uploaded=False):
    # Build WHERE clause that handles NULL values safely
    clauses = []
    params = []
    if name is None:
        clauses.append('name IS NULL')
    else:
        clauses.append('name = ?')
        params.append(name)
    if races is None:
        clauses.append('races IS NULL')
    else:
        clauses.append('races = ?')
        params.append(races)
    if difficulty is None:
        clauses.append('difficulty IS NULL')
    else:
        clauses.append('difficulty = ?')
        params.append(difficulty)

    where = ' AND '.join(clauses)

    if order_by_uploaded:
        # Join uploads to order by uploaded_at desc (newest first)
        sql = f'''
            SELECT f.id, u.uploaded_at
            FROM finish_records f
            LEFT JOIN uploads u ON f.upload_id = u.id
            WHERE {where}
            ORDER BY u.uploaded_at DESC, f.id DESC
        '''
        cur.execute(sql, params)
        rows = [r[0] for r in cur.fetchall()]
    else:
        sql = f'SELECT id FROM finish_records WHERE {where} ORDER BY id ASC'
        cur.execute(sql, params)
        rows = [r[0] for r in cur.fetchall()]
    return rows


def main():
    args = parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(2)

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    groups = find_duplicate_groups(cur)
    if not groups:
        print('No duplicate finish_records found.')
        conn.close()
        return

    total_candidates = 0
    total_deleted = 0
    to_delete_map = {}

    for name, races, difficulty, cnt in groups:
        ids = get_group_ids(cur, name, races, difficulty, order_by_uploaded=(args.keep == 'latest_upload'))
        if len(ids) <= 1:
            continue
        # decide which to keep
        if args.keep == 'first':
            keep_id = ids[0]
            delete_ids = ids[1:]
        else:  # latest_upload
            keep_id = ids[0]  # because get_group_ids with order_by_uploaded returns newest first
            delete_ids = ids[1:]

        total_candidates += len(delete_ids)
        if delete_ids:
            key = f"name={name!r}, races={races!r}, difficulty={difficulty!r}"
            to_delete_map[key] = delete_ids

    if not to_delete_map:
        print('No rows to delete after evaluation.')
        conn.close()
        return

    print(f'Found {len(to_delete_map)} duplicate groups with {total_candidates} rows marked for deletion.')
    if args.dry_run:
        print('Dry run mode - no changes will be made. The following rows would be deleted:')
        for k, ids in to_delete_map.items():
            print(f'  Group {k}: delete ids = {ids}')
        conn.close()
        return

    # Perform deletions in a transaction
    deleted = 0
    try:
        for ids in to_delete_map.values():
            # Delete in batches
            placeholders = ','.join('?' for _ in ids)
            sql = f'DELETE FROM finish_records WHERE id IN ({placeholders})'
            cur.execute(sql, ids)
            deleted += cur.rowcount
        conn.commit()
    except Exception as e:
        conn.rollback()
        print('Error during deletion:', e)
        conn.close()
        sys.exit(1)

    conn.close()
    print(f'Deletion complete. {deleted} rows removed.')


if __name__ == '__main__':
    main()
