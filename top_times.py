from flask import request, jsonify
import sqlite3
from typing import Callable, List


def register_routes(app, db_filename: str,
                    car_index_from_name: Callable[[str], int],
                    track_index_from_name: Callable[[str], int],
                    car_name_from_index: Callable[[int], str],
                    track_name_from_index: Callable[[int], str],
                    difficulty_name_from_index: Callable[[int], str]):
    """Register /api/top_times on the provided Flask app.

    The helper conversion functions are injected to avoid importing server internals.
    """

    @app.route('/api/top_times', methods=['GET'])
    def api_top_times():
        # accept multiple values for car/track/driver
        car_vals = request.args.getlist('car')
        track_vals = request.args.getlist('track')
        driver_vals = request.args.getlist('driver')

        # allow multiple times per driver flag (1/0)
        allow_dups_raw = request.args.get('allow_dups')
        allow_dups = True
        if allow_dups_raw is not None:
            if str(allow_dups_raw) in ('0', 'false', 'False'):
                allow_dups = False

        # convert car/track names to numeric indexes if provided
        car_idx_vals = [car_index_from_name(c) for c in car_vals if c]
        car_idx_vals = [c for c in car_idx_vals if c is not None]
        track_idx_vals = [track_index_from_name(t) for t in track_vals if t]
        track_idx_vals = [t for t in track_idx_vals if t is not None]

        # treat limit param
        limit_str = request.args.get('limit')
        limit = None
        if limit_str is not None:
            try:
                limit = int(limit_str)
            except Exception:
                limit = 10

        conn = sqlite3.connect(db_filename)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        rows = []

        # helper to build driver filter clause and params
        def _driver_clause(field_name='driver_name'):
            if not driver_vals:
                return ('', [])
            if len(driver_vals) == 1:
                return (f' AND {field_name} = ?', [driver_vals[0]])
            placeholders = ','.join(['?'] * len(driver_vals))
            return (f' AND {field_name} IN ({placeholders})', list(driver_vals))

        drv_clause, drv_params = _driver_clause('driver_name')

        # Helper: fetch rows for allow_dups=True for either global (car_val/track_val None)
        # or for a specific car/track pair (pass numeric car_val and track_val).
        def _fetch_allow_dups_for(car_val, track_val, outer_drv_clause, outer_drv_params, limit_val):
            # Build WHERE fragments for the two CTEs; include pair filters if provided
            driver_best_where = 'WHERE time IS NOT NULL'
            l_where = 'WHERE time IS NOT NULL'
            params = []
            if car_val is not None and track_val is not None:
                driver_best_where += ' AND car_type = ? AND track_idx = ?'
                l_where += ' AND car_type = ? AND track_idx = ?'
                # driver_best placeholders come first, then l placeholders
                params.extend([car_val, track_val, car_val, track_val])

            sql = f"""
                WITH driver_best AS (
                  SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                  FROM lap_records
                  {driver_best_where}
                  GROUP BY car_type, track_idx, driver_name
                ), driver_rank AS (
                  SELECT car_type, track_idx, driver_name,
                         ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS racer_rank
                  FROM driver_best
                ), l AS (
                  SELECT car_type, track_idx, driver_name, time, upload_id,
                         ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY time ASC) AS rn
                  FROM lap_records
                  {l_where}
                )
                SELECT l.car_type, l.track_idx, l.driver_name, l.time, l.rn AS rank, dr.racer_rank, u.uploaded_at
                FROM l
                LEFT JOIN driver_rank dr ON dr.car_type = l.car_type AND dr.track_idx = l.track_idx AND dr.driver_name = l.driver_name
                JOIN uploads u ON l.upload_id = u.id
            """
            exec_params = list(params)
            if not outer_drv_clause:
                # restrict to top-N using rn
                sql += 'WHERE l.rn <= ?'
                exec_params.append(limit_val)
            else:
                # apply outer driver filter but do not restrict by rn
                sql += outer_drv_clause
                exec_params.extend(outer_drv_params)
            sql += ' ORDER BY l.time ASC'
            cur.execute(sql, exec_params)
            return [dict(r) for r in cur.fetchall()]

        # Helper: fetch rows for allow_dups=False for either global (None) or pair
        def _fetch_no_dups_for(car_val, track_val, outer_drv_clause, outer_drv_params, limit_val):
            all_ranks_where = 'WHERE time IS NOT NULL'
            driver_best_where = 'WHERE time IS NOT NULL'
            params = []
            if car_val is not None and track_val is not None:
                all_ranks_where += ' AND car_type = ? AND track_idx = ?'
                driver_best_where += ' AND car_type = ? AND track_idx = ?'
                params.extend([car_val, track_val, car_val, track_val])

            sql = f"""
                    WITH all_ranks AS (
                      SELECT car_type, track_idx, driver_name, time, upload_id,
                             ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY time ASC) AS rank_global
                      FROM lap_records
                      {all_ranks_where}
                    ), driver_best AS (
                      SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                      FROM lap_records
                      {driver_best_where}
                      GROUP BY car_type, track_idx, driver_name
                    ), driver_rank AS (
                      SELECT car_type, track_idx, driver_name,
                             ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS racer_rank
                      FROM driver_best
                    ), best_with_rank AS (
                      SELECT db.car_type, db.track_idx, db.driver_name, db.best_time AS time, ar.rank_global AS rank, ar.upload_id, dr.racer_rank
                      FROM driver_best db
                      JOIN all_ranks ar ON ar.car_type = db.car_type AND ar.track_idx = db.track_idx AND ar.driver_name = db.driver_name AND ar.time = db.best_time
                      LEFT JOIN driver_rank dr ON dr.car_type = db.car_type AND dr.track_idx = db.track_idx AND dr.driver_name = db.driver_name
                    )
                    SELECT b.car_type, b.track_idx, b.driver_name, b.time, b.rank, b.racer_rank, u.uploaded_at
                    FROM best_with_rank b
                    JOIN uploads u ON b.upload_id = u.id
            """
            exec_params = list(params)
            if not outer_drv_clause:
                sql += ' WHERE b.rank <= ?'
                exec_params.append(limit_val)
            else:
                sql += ' ' + outer_drv_clause
                exec_params.extend(outer_drv_params)
            sql += ' ORDER BY b.rank ASC'
            cur.execute(sql, exec_params)
            return [dict(r) for r in cur.fetchall()]

        if not car_vals and not track_vals:
            # Return top-N per car+track combination.
            if limit is None:
                limit = 1
            # compute ROW_NUMBER / driver_best without applying driver filters so rank is global
            if allow_dups:
                # outer driver clause should reference l.driver_name
                outer_drv_clause = drv_clause.replace('driver_name', 'l.driver_name') if drv_clause else ''
                outer_drv_params = list(drv_params)
                rows = _fetch_allow_dups_for(None, None, outer_drv_clause, outer_drv_params, limit)
            else:
                # outer driver clause should reference b.driver_name
                outer_drv_clause = drv_clause.replace('driver_name', 'b.driver_name') if drv_clause else ''
                outer_drv_params = list(drv_params)
                rows = _fetch_no_dups_for(None, None, outer_drv_clause, outer_drv_params, limit)
        else:
            # When filters are present, return top-N per requested combination(s)
            if limit is None:
                limit = 10

            drv_clause_common, drv_params_common = _driver_clause('l.driver_name')

            # helper: return top-N rows for a given car/track pair
            def _top_n_for_pair(car_val, track_val, allow_dups_flag, limit_val):
                if allow_dups_flag:
                    # use the shared helper but pass the per-pair outer driver clause
                    outer_drv_clause = drv_clause_common.replace('l.driver_name', 'l.driver_name') if drv_clause_common else ''
                    outer_drv_params = list(drv_params_common)
                    return _fetch_allow_dups_for(car_val, track_val, outer_drv_clause, outer_drv_params, limit_val)
                else:
                    outer_drv_clause = drv_clause_common.replace('l.driver_name', 'b.driver_name') if drv_clause_common else ''
                    outer_drv_params = list(drv_params_common)
                    return _fetch_no_dups_for(car_val, track_val, outer_drv_clause, outer_drv_params, limit_val)

            # If both car and track lists specified: run queries for each pair
            if car_idx_vals and track_idx_vals:
                for c in car_idx_vals:
                    for t in track_idx_vals:
                        rows.extend(_top_n_for_pair(c, t, allow_dups, limit))
            elif car_idx_vals and not track_idx_vals:
                # for each specified car, return top-N per track
                for c in car_idx_vals:
                    q = 'SELECT DISTINCT track_idx FROM lap_records WHERE car_type = ?'
                    qparams = [c]
                    if drv_clause_common:
                        q += drv_clause_common.replace('l.driver_name', 'driver_name')
                        qparams.extend(drv_params_common)
                    cur.execute(q, qparams)
                    tracks = [r[0] for r in cur.fetchall()]
                    for t in tracks:
                        rows.extend(_top_n_for_pair(c, t, allow_dups, limit))
            elif track_idx_vals and not car_idx_vals:
                # for each specified track, return top-N per car
                for t in track_idx_vals:
                    q = 'SELECT DISTINCT car_type FROM lap_records WHERE track_idx = ?'
                    qparams = [t]
                    if drv_clause_common:
                        q += drv_clause_common.replace('l.driver_name', 'driver_name')
                        qparams.extend(drv_params_common)
                    cur.execute(q, qparams)
                    cars = [r[0] for r in cur.fetchall()]
                    for c in cars:
                        rows.extend(_top_n_for_pair(c, t, allow_dups, limit))
            else:
                rows = []

        # At this point `rows` may contain more than `limit` per (car_type, track_idx)
        # (e.g. due to joins or driver filtering). Enforce the per-car/track limit here
        # while preserving the global `rank` value computed by the SQL.
        if limit is not None:
            try:
                def _row_sort_key(r):
                    car_idx = r.get('car_type') if r.get('car_type') is not None else 9999
                    track_idx = r.get('track_idx') if r.get('track_idx') is not None else 9999
                    # prefer explicit rank if available, otherwise fall back to time
                    rankv = r.get('rank') if r.get('rank') is not None else (r.get('time') if r.get('time') is not None else 99999999)
                    return (car_idx, track_idx, rankv)
                rows.sort(key=_row_sort_key)

                trimmed = []
                counts = {}
                for r in rows:
                    key = (r.get('car_type'), r.get('track_idx'))
                    cnt = counts.get(key, 0)
                    if cnt < limit:
                        trimmed.append(r)
                        counts[key] = cnt + 1
                rows = trimmed
            except Exception:
                # if anything goes wrong, fall back to original rows
                pass

        # map numeric columns back to names for API output
        mapped = []
        for r in rows:
            d = dict(r)
            if 'car_type' not in d or d.get('car_type') is None:
                if d.get('car_name'):
                    d['car_type'] = car_index_from_name(d.get('car_name')) if callable(car_index_from_name) else None
            d['car_name'] = car_name_from_index(d.get('car_type')) if d.get('car_type') is not None else d.get('car_name')
            d['track_name'] = track_name_from_index(d.get('track_idx')) if d.get('track_idx') is not None else d.get('track_name')
            # ensure rank is an int when present
            if d.get('rank') is not None:
                try:
                    d['rank'] = int(d.get('rank'))
                except Exception:
                    pass
            # ensure racer_rank is an int when present
            if d.get('racer_rank') is not None:
                try:
                    d['racer_rank'] = int(d.get('racer_rank'))
                except Exception:
                    pass
            mapped.append(d)

        # ensure results are ordered by car index (numeric) and then track name
        try:
            if isinstance(mapped, list) and len(mapped) > 0:
                def sort_key(item):
                    car_idx = item.get('car_type')
                    if car_idx is None:
                        derived = car_index_from_name(item.get('car_name')) if callable(car_index_from_name) else None
                        car_idx = derived if derived is not None else 9999
                    track_name = item.get('track_name') or ''
                    return (car_idx, track_name)
                mapped.sort(key=sort_key)
        except Exception:
            pass

        conn.close()
        return jsonify({'results': mapped})
