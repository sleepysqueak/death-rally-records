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

        if not car_vals and not track_vals:
            # Return top-N per car+track combination.
            if allow_dups:
                if limit is None:
                    limit = 1
                inner_where_clause = 'WHERE time IS NOT NULL' + drv_clause
                sql = f"""
                SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at
                FROM (
                    SELECT car_type, track_idx, driver_name, time, upload_id,
                           ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY time ASC) as rn
                    FROM lap_records
                    {inner_where_clause}
                ) l
                JOIN uploads u ON l.upload_id = u.id
                WHERE l.rn <= ?
                ORDER BY l.car_type, l.track_idx, l.time ASC
                """
                exec_params = list(drv_params) + [limit]
                cur.execute(sql, exec_params)
                rows = [dict(r) for r in cur.fetchall()]
            else:
                if limit is None:
                    limit = 1
                inner_driver_where = 'WHERE time IS NOT NULL' + drv_clause
                sql = f"""
                WITH driver_best AS (
                  SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                  FROM lap_records
                  {inner_driver_where}
                  GROUP BY car_type, track_idx, driver_name
                ), ranked AS (
                  SELECT car_type, track_idx, driver_name, best_time,
                         ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS rn
                  FROM driver_best
                )
                SELECT r.car_type, r.track_idx, r.driver_name, r.best_time AS time, u.uploaded_at
                FROM ranked r
                JOIN lap_records l ON l.car_type = r.car_type AND l.track_idx = r.track_idx AND l.driver_name = r.driver_name AND l.time = r.best_time
                JOIN uploads u ON l.upload_id = u.id
                WHERE r.rn <= ?
                ORDER BY r.car_type, r.track_idx, r.best_time ASC
                """
                exec_params = list(drv_params) + [limit]
                cur.execute(sql, exec_params)
                rows = [dict(r) for r in cur.fetchall()]
        else:
            # When filters are present, return top-N per requested combination(s)
            if limit is None:
                limit = 10

            drv_clause_common, drv_params_common = _driver_clause('l.driver_name')

            # If both car and track lists specified: run queries for each pair
            if car_idx_vals and track_idx_vals:
                for c in car_idx_vals:
                    for t in track_idx_vals:
                        if allow_dups:
                            sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                sql += drv_clause_common
                                params.extend(drv_params_common)
                            sql += ' ORDER BY l.time ASC LIMIT ?'
                            params.append(limit)
                            cur.execute(sql, params)
                            rows.extend([dict(r) for r in cur.fetchall()])
                        else:
                            inner_driver_where = 'WHERE time IS NOT NULL AND car_type = ? AND track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                inner_driver_where += drv_clause_common.replace('l.driver_name', 'driver_name')
                                params.extend(drv_params_common)
                            sql = f"""
                            WITH driver_best AS (
                              SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                              FROM lap_records
                              {inner_driver_where}
                              GROUP BY car_type, track_idx, driver_name
                            ), ranked AS (
                              SELECT car_type, track_idx, driver_name, best_time,
                                     ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS rn
                              FROM driver_best
                            )
                            SELECT r.car_type, r.track_idx, r.driver_name, r.best_time AS time, u.uploaded_at
                            FROM ranked r
                            JOIN lap_records l ON l.car_type = r.car_type AND l.track_idx = r.track_idx AND l.driver_name = r.driver_name AND l.time = r.best_time
                            JOIN uploads u ON l.upload_id = u.id
                            WHERE r.rn <= ?
                            ORDER BY r.car_type, r.track_idx, r.best_time ASC
                            """
                            exec_params = params + [limit]
                            cur.execute(sql, exec_params)
                            rows.extend([dict(r) for r in cur.fetchall()])
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
                        if allow_dups:
                            sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                sql += drv_clause_common
                                params.extend(drv_params_common)
                            sql += ' ORDER BY l.time ASC LIMIT ?'
                            params.append(limit)
                            cur.execute(sql, params)
                            rows.extend([dict(r) for r in cur.fetchall()])
                        else:
                            inner_driver_where = 'WHERE time IS NOT NULL AND car_type = ? AND track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                inner_driver_where += drv_clause_common.replace('l.driver_name', 'driver_name')
                                params.extend(drv_params_common)
                            sql = f"""
                            WITH driver_best AS (
                              SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                              FROM lap_records
                              {inner_driver_where}
                              GROUP BY car_type, track_idx, driver_name
                            ), ranked AS (
                              SELECT car_type, track_idx, driver_name, best_time,
                                     ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS rn
                              FROM driver_best
                            )
                            SELECT r.car_type, r.track_idx, r.driver_name, r.best_time AS time, u.uploaded_at
                            FROM ranked r
                            JOIN lap_records l ON l.car_type = r.car_type AND l.track_idx = r.track_idx AND l.driver_name = r.driver_name AND l.time = r.best_time
                            JOIN uploads u ON l.upload_id = u.id
                            WHERE r.rn <= ?
                            ORDER BY r.car_type, r.track_idx, r.best_time ASC
                            """
                            exec_params = params + [limit]
                            cur.execute(sql, exec_params)
                            rows.extend([dict(r) for r in cur.fetchall()])
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
                        if allow_dups:
                            sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                sql += drv_clause_common
                                params.extend(drv_params_common)
                            sql += ' ORDER BY l.time ASC LIMIT ?'
                            params.append(limit)
                            cur.execute(sql, params)
                            rows.extend([dict(r) for r in cur.fetchall()])
                        else:
                            inner_driver_where = 'WHERE time IS NOT NULL AND car_type = ? AND track_idx = ?'
                            params = [c, t]
                            if drv_clause_common:
                                inner_driver_where += drv_clause_common.replace('l.driver_name', 'driver_name')
                                params.extend(drv_params_common)
                            sql = f"""
                            WITH driver_best AS (
                              SELECT car_type, track_idx, driver_name, MIN(time) AS best_time
                              FROM lap_records
                              {inner_driver_where}
                              GROUP BY car_type, track_idx, driver_name
                            ), ranked AS (
                              SELECT car_type, track_idx, driver_name, best_time,
                                     ROW_NUMBER() OVER (PARTITION BY car_type, track_idx ORDER BY best_time ASC) AS rn
                              FROM driver_best
                            )
                            SELECT r.car_type, r.track_idx, r.driver_name, r.best_time AS time, u.uploaded_at
                            FROM ranked r
                            JOIN lap_records l ON l.car_type = r.car_type AND l.track_idx = r.track_idx AND l.driver_name = r.driver_name AND l.time = r.best_time
                            JOIN uploads u ON l.upload_id = u.id
                            WHERE r.rn <= ?
                            ORDER BY r.car_type, r.track_idx, r.best_time ASC
                            """
                            exec_params = params + [limit]
                            cur.execute(sql, exec_params)
                            rows.extend([dict(r) for r in cur.fetchall()])
            else:
                rows = []

        # map numeric columns back to names for API output
        mapped = []
        for r in rows:
            d = dict(r)
            if 'car_type' not in d or d.get('car_type') is None:
                if d.get('car_name'):
                    d['car_type'] = car_index_from_name(d.get('car_name')) if callable(car_index_from_name) else None
            d['car_name'] = car_name_from_index(d.get('car_type')) if d.get('car_type') is not None else d.get('car_name')
            d['track_name'] = track_name_from_index(d.get('track_idx')) if d.get('track_idx') is not None else d.get('track_name')
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
