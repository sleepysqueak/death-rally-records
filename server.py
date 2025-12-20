from flask import Flask, request, jsonify, render_template_string
import tempfile
import os
from dataclasses import asdict
from typing import Any, List, Tuple, Union
import sqlite3
from datetime import datetime

# Import the existing parser
from records import read_records, LapRecord, FinishRecord
from rebuild_db import create_schema

app = Flask(__name__, static_folder='static')

UPLOAD_FORM = (
"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Death Rally Records</title>
  <link rel="stylesheet" href="/static/styles.css">
</head>
<body>
  <div class="container">
    <h1>Death Rally Records</h1>
    <p>Use the form below to upload a <code>dr.cfg</code> file, or use the links to explore the API and UI.</p>
    <ul>
      <li><a href="/leaderboards/view">Leaderboards (HTML view)</a></li>
      <li><a href="/leaderboards">Leaderboards (JSON)</a></li>
      <li><a href="/browse">Browse Top Times (interactive UI)</a></li>
      <li><a href="/api/meta">API: /api/meta (JSON)</a></li>
      <li><a href="/api/top_times">API: /api/top_times (JSON)</a> - accepts query params: <code>car</code>, <code>track</code>, <code>driver</code>, <code>limit</code></li>
    </ul>

    <h2>Upload dr.cfg (you may select multiple files)</h2>
    <form action="/upload" method=post enctype=multipart/form-data>
      <input type=file name=file multiple>
      <input type=submit value=Upload>
    </form>

    {message_block}
  </div>
</body>
</html>
""")

@app.route('/', methods=['GET'])
def index():
    # render with empty message by default
    return render_template_string(UPLOAD_FORM.format(message_block=''))

# human-readable mappings for car types and tracks (used to convert indexes from records.LapRecord)
CAR_NAMES = ["Vagabond", "Dervish", "Sentinel", "Shrieker", "Wraith", "Deliverator"]
TRACK_NAMES = [
    "Suburbia", "Downtown", "Utopia", "Rock Zone", "Snake Alley", "Oasis",
    "Velodrome", "Holocaust", "Bogota", "West End", "Newark", "Complex",
    "Hell Mountain", "Desert Run", "Palm Side", "Eidolon", "Toxic Dump", "Borneo"
]

# Difficulty names — stored in DB as numeric indexes (0..n-1)
DIFFICULTY_NAMES = [
    'Speed makes me dizzy',
    'I live to ride',
    'Petrol in my veins'
]

def car_name_from_index(i: int) -> str:
    try:
        return CAR_NAMES[int(i)]
    except Exception:
        return f'car{i}'

def track_name_from_index(idx: int) -> str:
    try:
        return TRACK_NAMES[int(idx)]
    except Exception:
        return f'track{idx}'

def difficulty_name_from_index(idx: int) -> str:
    try:
        return DIFFICULTY_NAMES[int(idx)]
    except Exception:
        return f'difficulty{idx}'

def car_index_from_name(name: str):
    if not name:
        return None
    try:
        return CAR_NAMES.index(name)
    except ValueError:
        return None

def track_index_from_name(name: str):
    if not name:
        return None
    try:
        return TRACK_NAMES.index(name)
    except ValueError:
        return None

def difficulty_index_from_name(name: str):
    if not name:
        return None
    try:
        return DIFFICULTY_NAMES.index(name)
    except ValueError:
        return None

def dataclass_list_to_jsonable(lst: List[Union[LapRecord, FinishRecord]]) -> List[dict]:
    # convert list of dataclasses to list of dicts
    out = []
    for x in lst:
        d = asdict(x)
        # If parser uses numeric indexes, convert to human-readable fields expected by the API
        if 'car_type' in d and 'track_idx' in d:
            d['car_name'] = car_name_from_index(d.get('car_type'))
            d['track_name'] = track_name_from_index(d.get('track_idx'))
        # If finish records use numeric difficulty index, expose human-readable difficulty
        if 'difficulty_idx' in d:
            d['difficulty'] = difficulty_name_from_index(d.get('difficulty_idx'))
        out.append(d)
    return out

DB_FILENAME = os.path.join(os.path.dirname(__file__), 'records.db')

def init_db(db_path: str = DB_FILENAME) -> None:
    """Create database and tables if they don't exist using shared schema function."""
    conn = sqlite3.connect(db_path)
    try:
        create_schema(conn)
    finally:
        conn.close()

def save_records(db_path: str, filename: str, lap_records: List[LapRecord], finish_records: List[FinishRecord]) -> Tuple[int, int, int]:
    """Save parsed records into the database. Returns (upload_id, lap_inserted, finish_inserted)."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    uploaded_at = datetime.utcnow().isoformat() + 'Z'
    cur.execute('INSERT INTO uploads (filename, uploaded_at) VALUES (?, ?)', (filename, uploaded_at))
    upload_id = cur.lastrowid

    lap_inserted = 0
    finish_inserted = 0

    # Insert lap records, but avoid duplicates defined as same car_type+track_idx+driver_name+time
    for r in lap_records:
        # r may be a dataclass with numeric indexes (car_type, track_idx) or legacy fields
        if hasattr(r, 'car_type') and hasattr(r, 'track_idx'):
            car_type = r.car_type
            track_idx = r.track_idx
        else:
            # fallback: try to derive numeric indexes from names
            car_type = car_index_from_name(getattr(r, 'car_name', None))
            track_idx = getattr(r, 'idx', None) or track_index_from_name(getattr(r, 'track_name', None))

        # Use IS for driver_name/time comparison to correctly handle NULLs
        cur.execute(
            'SELECT 1 FROM lap_records WHERE car_type = ? AND track_idx = ? AND driver_name IS ? AND time IS ?',
            (car_type, track_idx, getattr(r, 'driver_name', None), getattr(r, 'time', None))
        )
        if cur.fetchone():
            # duplicate found, skip insertion
            continue
        cur.execute(
            'INSERT INTO lap_records (upload_id, car_type, track_idx, time, driver_name) VALUES (?, ?, ?, ?, ?)', 
            (upload_id, car_type, track_idx, getattr(r, 'time', None), getattr(r, 'driver_name', None))
        )
        lap_inserted += 1

    # Insert finish records, but avoid duplicates defined as same name+races+difficulty
    for fr in finish_records:
        # determine numeric difficulty index (backwards-compatible with textual difficulty)
        diff_idx = getattr(fr, 'difficulty')
        
        # consider a duplicate to be same name + races + difficulty_idx (across any upload)
        cur.execute(
            'SELECT 1 FROM finish_records WHERE name IS ? AND races IS ? AND difficulty_idx IS ?',
            (fr.name, fr.races, diff_idx)
        )
        if cur.fetchone():
            # duplicate found, skip
            continue
        cur.execute(
            'INSERT INTO finish_records (upload_id, name, races, difficulty_idx) VALUES (?, ?, ?, ?)',
            (upload_id, fr.name, fr.races, diff_idx)
        )
        finish_inserted += 1

    conn.commit()
    conn.close()
    return (upload_id, lap_inserted, finish_inserted)

@app.route('/upload', methods=['POST'])
def upload():
    # accept multiple files
    files = request.files.getlist('file')
    if not files or len(files) == 0:
        return jsonify({'error': 'no file part'}), 400

    summary_rows = []
    total_laps = 0
    total_finishes = 0

    for file in files:
        if not file or file.filename == '':
            continue
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.cfg')
        try:
            file.save(tmp.name)
            tmp.close()
            lap_records, finish_records = read_records(tmp.name)
            upload_id, laps_inserted, finishes_inserted = save_records(DB_FILENAME, file.filename, lap_records, finish_records)
            total_laps += laps_inserted
            total_finishes += finishes_inserted
            summary_rows.append((file.filename, laps_inserted, finishes_inserted, upload_id))
        finally:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass

    if len(summary_rows) == 0:
        return jsonify({'error': 'no valid files uploaded'}), 400

    # build feedback HTML and stay on the upload page
    parts = []
    parts.append(f"<p>Processed {len(summary_rows)} file(s). Inserted <strong>{total_laps}</strong> new lap record(s) and <strong>{total_finishes}</strong> new finish record(s).</p>")
    parts.append('<ul>')
    for fn, lins, fins, uid in summary_rows:
        parts.append(f"<li>{fn}: {lins} lap(s), {fins} finish(es) (upload id {uid})</li>")
    parts.append('</ul>')
    parts.append('<p><a href="/">Back to upload form</a></p>')
    message_html = '\n'.join(parts)

    return render_template_string(UPLOAD_FORM.format(message_block=message_html))

# --- Leaderboards endpoints ---

def get_leaderboards(db_path: str = DB_FILENAME):
    """Query the database and return leaderboards as dicts (include upload timestamps)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # best lap time per car and track (use track_idx to identify track order)
    cur.execute('''
    SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at
    FROM lap_records l
    JOIN uploads u ON l.upload_id = u.id
    WHERE l.time IS NOT NULL AND l.time = (
        SELECT MIN(time) FROM lap_records WHERE car_type = l.car_type AND track_idx = l.track_idx AND time IS NOT NULL
    )
    ORDER BY l.car_type
    ''')
    lap_leaders = []
    for r in cur.fetchall():
        d = dict(r)
        d['car_name'] = car_name_from_index(d.get('car_type'))
        d['track_name'] = track_name_from_index(d.get('track_idx'))
        lap_leaders.append(d)

    # Sort lap leaders by numeric car index then human-readable track name (to order by car, then track name)
    try:
        def _lb_sort_key(item):
            c = item.get('car_type')
            if c is None:
                c = 9999
            tn = item.get('track_name') or ''
            return (c, tn.lower())
        lap_leaders.sort(key=_lb_sort_key)
    except Exception:
        pass

    finish_by_difficulty = {}

    # Fetch for the known levels in the requested order
    for lvl_idx in range(3):
        cur.execute('''
            SELECT f.name, f.races, f.difficulty_idx, u.uploaded_at
            FROM finish_records f
            JOIN uploads u ON f.upload_id = u.id
            WHERE f.races IS NOT NULL AND f.difficulty_idx = ?
            ORDER BY f.races ASC, f.name ASC
            LIMIT 100
        ''', (lvl_idx,))
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            # map numeric difficulty index back to human-readable name for the returned rows
            for rr in rows:
                rr['difficulty'] = difficulty_name_from_index(rr.get('difficulty_idx'))
        finish_by_difficulty[lvl_idx] = rows

    conn.close()
    return {
        'lap_leaders': lap_leaders,
        'finish_by_difficulty': finish_by_difficulty,
    }

@app.route('/leaderboards', methods=['GET'])
def leaderboards_json():
    """Return leaderboards as JSON."""
    data = get_leaderboards()
    return jsonify(data)

@app.route('/leaderboards/view', methods=['GET'])
def leaderboards_view():
    """Simple HTML view of the leaderboards."""
    data = get_leaderboards()

    # Link to the static stylesheet
    html = [
        '<html><head><title>Death Rally Leaderboards</title>',
        '<meta charset="utf-8">',
        '<link rel="stylesheet" href="/static/styles.css">',
        '</head><body>'
    ]
    html.append('<h1>Lap Leaders</h1>')
    # link back to main page
    html.append('<p><a href="/">Home</a></p>')
    html.append('<table><tr><th>Car</th><th>Track</th><th>Driver</th><th>Time (s)</th><th>Uploaded</th></tr>')
    for r in data['lap_leaders']:
        time_display = f"{r['time']:.2f}" if r.get('time') is not None else ''
        uploaded_at = r.get('uploaded_at')
        if uploaded_at:
            # clean the ISO timestamp for hover (replace T with space, strip trailing Z)
            hover = uploaded_at.replace('T', ' ').rstrip('Z')
            display_date = hover.split(' ')[0]
            uploaded_td = f"<td title=\"{hover}\">{display_date}</td>"
        else:
            uploaded_td = '<td></td>'
        html.append(f"<tr><td>{r['car_name']}</td><td>{r['track_name']}</td><td>{r['driver_name']}</td><td>{time_display}</td>{uploaded_td}</tr>")
    html.append('</table>')

    # Render top finishers grouped by difficulty in requested order
    html.append('<h1>Finish Leaders (top 100 per difficulty)</h1>')
    for diff in range(3):
        rows = data['finish_by_difficulty'].get(diff, [])
        label = difficulty_name_from_index(diff) if difficulty_name_from_index(diff) else 'Unknown'
        html.append(f"<h2>Difficulty: {label}</h2>")
        html.append('<table><tr><th>#</th><th>Name</th><th>Races</th><th>Uploaded</th></tr>')
        for i, f in enumerate(rows, start=1):
            uploaded_at = f.get('uploaded_at')
            if uploaded_at:
                hover = uploaded_at.replace('T', ' ').rstrip('Z')
                display_date = hover.split(' ')[0]
                uploaded_td = f"<td title=\"{hover}\">{display_date}</td>"
            else:
                uploaded_td = '<td></td>'
            html.append(f"<tr><td>{i}</td><td>{f['name']}</td><td>{f['races']}</td>{uploaded_td}</tr>")
        html.append('</table>')

    html.append('</body></html>')
    return '\n'.join(html)

# API: get top times with optional filters
@app.route('/api/top_times', methods=['GET'])
def api_top_times():
    """Return top lap times filtered by car, track, driver. Accepts multiple `car`, `track`, `driver` params."""
    # accept multiple values for car/track/driver
    car_vals = request.args.getlist('car')  # list of car names
    track_vals = request.args.getlist('track')  # list of track names
    driver_vals = request.args.getlist('driver')  # list of driver names (exact match)

    # convert car/track names to numeric indexes if provided
    car_idx_vals = [car_index_from_name(c) for c in car_vals if c]
    # filter out None conversions
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

    conn = sqlite3.connect(DB_FILENAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = []

    # helper to build driver filter clause and params
    def _driver_clause(field_name='driver_name', params_list=None):
        if not driver_vals:
            return ('', [])
        if len(driver_vals) == 1:
            return (f' AND {field_name} = ?', [driver_vals[0]])
        placeholders = ','.join(['?'] * len(driver_vals))
        return (f' AND {field_name} IN ({placeholders})', list(driver_vals))

    if not car_vals and not track_vals:
        # Return best time per car+track combination. Respect optional driver filter.
        params = []
        inner_where = 'WHERE time IS NOT NULL'
        drv_clause, drv_params = _driver_clause('driver_name')
        inner_where += drv_clause
        params.extend(drv_params)

        # subquery: best time per car_type and track_idx
        subq = f"SELECT car_type, track_idx, MIN(time) as min_time FROM lap_records {inner_where} GROUP BY car_type, track_idx"

        sql = f"""
        SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at
        FROM lap_records l
        JOIN uploads u ON l.upload_id = u.id
        JOIN ({subq}) m ON l.car_type = m.car_type AND l.track_idx = m.track_idx AND l.time = m.min_time
        ORDER BY l.car_type, l.track_idx
        """

        cur.execute(sql, params)
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
                    sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                    params = [c, t]
                    if drv_clause_common:
                        sql += drv_clause_common
                        params.extend(drv_params_common)
                    sql += ' ORDER BY l.time ASC LIMIT ?'
                    params.append(limit)
                    cur.execute(sql, params)
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
                    sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                    params = [c, t]
                    if drv_clause_common:
                        sql += drv_clause_common
                        params.extend(drv_params_common)
                    sql += ' ORDER BY l.time ASC LIMIT ?'
                    params.append(limit)
                    cur.execute(sql, params)
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
            rows = []

    # map numeric columns back to names for API output
    mapped = []
    for r in rows:
        d = dict(r)
        if 'car_type' not in d or d.get('car_type') is None:
            if d.get('car_name'):
                d['car_type'] = car_index_from_name(d.get('car_name'))
        if 'track_idx' not in d or d.get('track_idx') is None:
            d['track_idx'] = d.get('track_idx')
        d['car_name'] = car_name_from_index(d.get('car_type')) if d.get('car_type') is not None else d.get('car_name')
        d['track_name'] = track_name_from_index(d.get('track_idx')) if d.get('track_idx') is not None else d.get('track_name')
        mapped.append(d)

    # ensure results are ordered by car index (numeric) and then track name
    try:
        if isinstance(mapped, list) and len(mapped) > 0:
            def sort_key(item):
                car_idx = item.get('car_type')
                if car_idx is None:
                    derived = car_index_from_name(item.get('car_name'))
                    car_idx = derived if derived is not None else 9999
                track_name = item.get('track_name') or ''
                return (car_idx, track_name)
            mapped.sort(key=sort_key)
    except Exception:
        pass

    conn.close()
    return jsonify({'results': mapped})

@app.route('/api/meta', methods=['GET'])
def api_meta():
    """Return distinct car names, track names and driver names for UI selectors."""
    conn = sqlite3.connect(DB_FILENAME)
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT car_type FROM lap_records ORDER BY car_type')
    car_idxs = [r[0] for r in cur.fetchall()]
    cars = [car_name_from_index(i) for i in car_idxs]
    cur.execute('SELECT DISTINCT track_idx FROM lap_records ORDER BY track_idx')
    track_idxs = [r[0] for r in cur.fetchall()]
    tracks = [track_name_from_index(i) for i in track_idxs]
    # difficulties
    cur.execute('SELECT DISTINCT difficulty_idx FROM finish_records ORDER BY difficulty_idx')
    diff_idxs = [r[0] for r in cur.fetchall()]
    difficulties = [difficulty_name_from_index(i) for i in diff_idxs]
    # limit drivers list to distinct non-empty names
    cur.execute("SELECT DISTINCT driver_name FROM lap_records WHERE driver_name IS NOT NULL AND driver_name <> '' ORDER BY driver_name")
    drivers = [r[0] for r in cur.fetchall()]
    conn.close()
    return jsonify({'cars': cars, 'tracks': tracks, 'drivers': drivers, 'difficulties': difficulties})

@app.route('/browse', methods=['GET'])
def browse_view():
    """HTML UI to browse top times with multi-select filters."""
    html = (
    """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Browse Top Times</title>
      <link rel="stylesheet" href="/static/styles.css">
    </head>
    <body>
      <h1>Browse Top Times</h1>
      <p><a href="/">Home</a></p>
      <div>
        <label>Racers per Car/Track: <input id="limit" type="number" min="1" value="1" style="width:60px"></label>
      </div>
      <div style="display:flex;gap:12px;align-items:flex-start;margin-top:8px;">
        <!-- Compact multi-select dropdown for Car -->
        <div class="multisel" id="car_multisel" style="position:relative;">
          <button type="button" class="ms-toggle" onclick="toggleDropdown('car_multisel')">Cars: <span class="ms-count">Any</span></button>
          <div class="ms-dropdown" style="display:none;position:absolute;z-index:50;padding:6px;max-height:240px;overflow-y:auto;overflow-x:hidden;width:220px;">
            <input class="ms-search" placeholder="Search cars..." style="width:100%;box-sizing:border-box;margin-bottom:6px;padding:4px;" />
            <div class="ms-options"></div>
          </div>
        </div>

        <!-- Compact multi-select dropdown for Track -->
        <div class="multisel" id="track_multisel" style="position:relative;">
          <button type="button" class="ms-toggle" onclick="toggleDropdown('track_multisel')">Tracks: <span class="ms-count">Any</span></button>
          <div class="ms-dropdown" style="display:none;position:absolute;z-index:50;padding:6px;max-height:240px;overflow-y:auto;overflow-x:hidden;width:220px;">
            <input class="ms-search" placeholder="Search tracks..." style="width:100%;box-sizing:border-box;margin-bottom:6px;padding:4px;" />
            <div class="ms-options"></div>
          </div>
        </div>

        <!-- Compact multi-select dropdown for Driver (searchable) -->
        <div class="multisel" id="driver_multisel" style="position:relative;">
          <button type="button" class="ms-toggle" onclick="toggleDropdown('driver_multisel')">Drivers: <span class="ms-count">Any</span></button>
          <div class="ms-dropdown" style="display:none;position:absolute;z-index:50;padding:6px;max-height:240px;overflow-y:auto;overflow-x:hidden;width:280px;">
            <input class="ms-search" placeholder="Search drivers..." style="width:100%;box-sizing:border-box;margin-bottom:6px;padding:4px;" />
            <div class="ms-options"></div>
          </div>
        </div>
      </div>
      <div style="margin-top:8px;">
        <button id="filter">Filter</button>
      </div>
      <div id="results"></div>

    <script>
    // toggle helper (explicit global function to ensure clicks always work)
    function toggleDropdown(rootId){
      try{
        const root = document.getElementById(rootId);
        if(!root) return;
        const dropdown = root.querySelector('.ms-dropdown');
        if(!dropdown) return;
        const visible = dropdown.style.display !== 'none';
        // close other dropdowns
        document.querySelectorAll('.ms-dropdown').forEach(d=>{ d.style.display = 'none'; });
        dropdown.style.display = visible ? 'none' : 'block';
        // focus search input if opening
        if(!visible){
          const s = root.querySelector('.ms-search'); if(s) s.focus();
        }
      }catch(e){ console && console.error && console.error(e); }
    }

    // Maximum number of selected option names to display on the toggle button
    const MS_DISPLAY_MAX = 2;

    // Helper to create a compact multi-select dropdown with search and '(any)'.
    function buildMultiSel(containerId, items){
      const root = document.getElementById(containerId);
      const toggle = root.querySelector('.ms-toggle');
      const dropdown = root.querySelector('.ms-dropdown');
      const search = root.querySelector('.ms-search');
      const opts = root.querySelector('.ms-options');

      function renderOptions(filter){
        opts.innerHTML = '';
        // '(any)' option first
        const anyDiv = document.createElement('div');
        anyDiv.innerHTML = `<label style="display:block;margin:2px 0"><input type="checkbox" data-value=""> (any)</label>`;
        opts.appendChild(anyDiv);
        items.forEach(v => {
          if(!v) return;
          if(filter && v.toLowerCase().indexOf(filter) === -1) return;
          const d = document.createElement('div');
          d.innerHTML = `<label style="display:block;margin:2px 0"><input type="checkbox" data-value="${v}"> ${v}</label>`;
          opts.appendChild(d);
        });
      }

      renderOptions('');

      // search filtering
      search.addEventListener('input', ()=>{ const f = search.value.trim().toLowerCase(); renderOptions(f);
        // restore selection state after re-render
        restoreSelection();
      });

      // helper to get checkboxes
      function allCheckboxes(){ return Array.from(opts.querySelectorAll('input[type=checkbox]')); }

      // keep track of selections in a Set
      const selected = new Set();

      function updateCount(){
        const cnt = selected.size;
        const span = toggle.querySelector('.ms-count');
        if(cnt === 0){
          span.textContent = 'Any';
          return;
        }
        // If we have a small number of selections, show their names (comma-separated)
        if(cnt <= MS_DISPLAY_MAX){
          const names = Array.from(selected);
          span.textContent = names.join(', ');
          return;
        }
        // Fallback to a generic count label for larger selections
        span.textContent = cnt === 1 ? '1 selected' : `${cnt} selected`;
      }

      function restoreSelection(){
        const boxes = Array.from(opts.querySelectorAll('input[type=checkbox]'));
        boxes.forEach(cb=>{ cb.checked = selected.has(cb.dataset.value); });
      }

      // click handler for options
      opts.addEventListener('change', (e)=>{
        const cb = e.target;
        if(!cb || cb.type !== 'checkbox') return;
        const val = cb.dataset.value; // empty string for Any
        if(val === ''){
          // '(any)' checkbox clicked
          if(cb.checked){
            // User checked '(any)' -> clear all other selections and ensure '(any)' stays checked
            selected.clear();
            allCheckboxes().forEach(c=>{ if(c.dataset.value) c.checked = false; });
            cb.checked = true; // ensure it remains checked
          } else {
            // User attempted to uncheck '(any)'. If no other option is selected, prevent unchecking to avoid empty state.
            const someOtherChecked = Array.from(opts.querySelectorAll('input[type=checkbox]')).some(c => c.dataset.value && c.checked);
            if(!someOtherChecked){
              // Re-check '(any)' and do nothing else
              cb.checked = true;
              return;
            }
            // If there are other selections (rare), allow unchecking; selected will be updated below
          }
        } else {
          // when any other selected, uncheck Any
          const anyCb = opts.querySelector('input[type=checkbox][data-value=""]');
          if(cb.checked){
            selected.add(val);
            if(anyCb) anyCb.checked = false;
          } else {
            selected.delete(val);
            // if none remain selected, set Any checked
            if(selected.size === 0 && anyCb) anyCb.checked = true;
          }
        }
        // sync selected set with checkboxes (ensure selected entries are maintained across searches)
        Array.from(opts.querySelectorAll('input[type=checkbox]')).forEach(c=>{ if(c.dataset.value && c.checked) selected.add(c.dataset.value); });
        // if any checkbox (empty) is checked, clear selected
        const anyChecked = !!opts.querySelector('input[type=checkbox][data-value=""]:checked');
        if(anyChecked) selected.clear();
        updateCount();
      });

      // expose helper methods on the root element
      root.getSelectedValues = function(){ return Array.from(selected); };
      root.clear = function(){ selected.clear(); updateCount(); restoreSelection(); };

      // initialize: nothing selected -> Any checked
      const anyCbInit = opts.querySelector('input[type=checkbox][data-value=""]');
      if(anyCbInit) anyCbInit.checked = true;
      updateCount();
    }

    async function loadMeta(){
      const res = await fetch('/api/meta');
      const meta = await res.json();
      buildMultiSel('car_multisel', meta.cars || []);
      // sort tracks by name (case-insensitive) for the UI
      const tracksSorted = (meta.tracks || []).slice().sort((a,b)=> (a||'').toLowerCase().localeCompare((b||'').toLowerCase()));
      buildMultiSel('track_multisel', tracksSorted);
      buildMultiSel('driver_multisel', (meta.drivers||[]).filter(d=>d && d.trim() !== ''));
    }

    function _getSelectedValuesFromMulti(id){
      const el = document.getElementById(id);
      if(!el || typeof el.getSelectedValues !== 'function') return [];
      return el.getSelectedValues();
    }

    async function doFilter(){
      const carVals = _getSelectedValuesFromMulti('car_multisel');
      const trackVals = _getSelectedValuesFromMulti('track_multisel');
      const driverVals = _getSelectedValuesFromMulti('driver_multisel');
      const limit = document.getElementById('limit').value || 1;
      const params = new URLSearchParams();
      carVals.forEach(v => params.append('car', v));
      trackVals.forEach(v => params.append('track', v));
      driverVals.forEach(v => params.append('driver', v));
      if(limit) params.append('limit', limit);
      const res = await fetch('/api/top_times?' + params.toString());
      const data = await res.json();
      renderResults(data.results);
    }

    function renderResults(rows){
      const container = document.getElementById('results');
      if(!rows || rows.length === 0){ container.innerHTML = '<p>No results</p>'; return; }
      let html = '<table><tr><th>Car</th><th>Track</th><th>Driver</th><th>Time (s)</th><th>Uploaded</th></tr>';
      rows.forEach((r) => {
        const time = r.time !== null ? r.time.toFixed(2) : '';
        let uploaded_td = '<td></td>';
        if(r.uploaded_at){
          const iso = new Date(r.uploaded_at).toISOString();
          const hover = iso.replace('T',' ').replace(/Z$/,'');
          const display = iso.split('T')[0];
          uploaded_td = `<td title="${hover}">${display}</td>`;
        }
        html += `<tr><td>${r.car_name}</td><td>${r.track_name}</td><td>${r.driver_name}</td><td>${time}</td>${uploaded_td}</tr>`;
      });
      html += '</table>';
      container.innerHTML = html;
    }

    document.getElementById('filter').addEventListener('click', doFilter);
    window.addEventListener('load', async () => { await loadMeta(); await doFilter(); });

    // Close all multi-select dropdowns when clicking outside of any .multisel
    document.addEventListener('click', function(e){
      try{
        // If the click occurred inside a multisel, do nothing
        if (e.target && e.target.closest && e.target.closest('.multisel')) return;
        // Otherwise hide any open dropdowns
        document.querySelectorAll('.ms-dropdown').forEach(d => { d.style.display = 'none'; });
      }catch(err){ console && console.error && console.error(err); }
    });

    // Close dropdowns when Escape is pressed
    document.addEventListener('keydown', function(e){
      try{
        if(e.key === 'Escape' || e.key === 'Esc'){
          document.querySelectorAll('.ms-dropdown').forEach(d => { d.style.display = 'none'; });
        }
      }catch(err){ console && console.error && console.error(err); }
    });
    </script>
    </body>
    </html>
    """
    )
    return html

if __name__ == '__main__':
    # bind to localhost only for safety
    app.run(host='127.0.0.1', port=8000, debug=True)
