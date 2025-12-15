from flask import Flask, request, jsonify, render_template_string
import tempfile
import os
from dataclasses import asdict
from typing import Any
import sqlite3
from datetime import datetime

# Import the existing parser
from records import read_records

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

    <h2>Upload dr.cfg</h2>
    <form action="/upload" method=post enctype=multipart/form-data>
      <input type=file name=file>
      <input type=submit value=Upload>
    </form>
  </div>
</body>
</html>
""")


@app.route('/', methods=['GET'])
def index():
    return render_template_string(UPLOAD_FORM)


# human-readable mappings for car types and tracks (used to convert indexes from records.LapRecord)
CAR_NAMES = ["Vagabond", "Dervish", "Sentinel", "Shrieker", "Wraith", "Deliverator"]
TRACK_NAMES = [
    "Suburbia", "Downtown", "Utopia", "Rock Zone", "Snake Alley", "Oasis",
    "Velodrome", "Holocaust", "Bogota", "West End", "Newark", "Complex",
    "Hell Mountain", "Desert Run", "Palm Side", "Eidolon", "Toxic Dump", "Borneo"
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
        return TRACK_NAMES.index(name) + 1
    except ValueError:
        return None


def dataclass_list_to_jsonable(lst: Any):
    # convert list of dataclasses to list of dicts
    out = []
    for x in lst:
        d = asdict(x)
        # If parser uses numeric indexes, convert to human-readable fields expected by the API
        if 'car_type' in d and 'track_idx' in d:
            d['car_name'] = car_name_from_index(d.get('car_type'))
            d['track_name'] = track_name_from_index(d.get('track_idx'))
        out.append(d)
    return out


DB_FILENAME = os.path.join(os.path.dirname(__file__), 'records.db')


def init_db(db_path: str = DB_FILENAME) -> None:
    """Create database and tables if they don't exist."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY,
        filename TEXT,
        uploaded_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lap_records (
        id INTEGER PRIMARY KEY,
        upload_id INTEGER,
        car_type INTEGER,
        track_idx INTEGER,
        time REAL,
        driver_name TEXT,
        FOREIGN KEY(upload_id) REFERENCES uploads(id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS finish_records (
        id INTEGER PRIMARY KEY,
        upload_id INTEGER,
        name TEXT,
        races INTEGER,
        difficulty TEXT,
        FOREIGN KEY(upload_id) REFERENCES uploads(id)
    )
    """)

    # Indexes to speed up common queries (leaderboards, top_times, meta)
    # Composite index for car_type+track_idx lookups
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_track_idx ON lap_records(car_type, track_idx)')
    # Indexes for time-based ordering and filtering
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_time ON lap_records(car_type, time)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_track_time ON lap_records(track_idx, time)')
    # Driver name lookup (used with LIKE)
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_driver_name ON lap_records(driver_name)')
    # Upload related lookups
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_upload_id ON lap_records(upload_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_finish_upload_id ON finish_records(upload_id)')

    conn.commit()
    conn.close()


def save_records(db_path: str, filename: str, lap_records: list, finish_records: list) -> int:
    """Save parsed records into the database. Returns upload_id."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    uploaded_at = datetime.utcnow().isoformat() + 'Z'
    cur.execute('INSERT INTO uploads (filename, uploaded_at) VALUES (?, ?)', (filename, uploaded_at))
    upload_id = cur.lastrowid

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

    # Insert finish records, but avoid duplicates defined as same name+races+difficulty
    for fr in finish_records:
        # consider a duplicate to be same name + races + difficulty (across any upload)
        cur.execute(
            'SELECT 1 FROM finish_records WHERE name IS ? AND races IS ? AND difficulty IS ?',
            (fr.name, fr.races, fr.difficulty)
        )
        if cur.fetchone():
            # duplicate found, skip
            continue
        cur.execute(
            'INSERT INTO finish_records (upload_id, name, races, difficulty) VALUES (?, ?, ?, ?)',
            (upload_id, fr.name, fr.races, fr.difficulty)
        )

    conn.commit()
    conn.close()
    return upload_id


@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'no file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'empty filename'}), 400

    # save to a temporary file and pass path to read_records
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.cfg')
    try:
        file.save(tmp.name)
        tmp.close()
        lap_records, finish_records = read_records(tmp.name)
        upload_id = save_records(DB_FILENAME, file.filename, lap_records, finish_records)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    return jsonify({
        'lap_records': dataclass_list_to_jsonable(lap_records),
        'finish_records': dataclass_list_to_jsonable(finish_records),
        'upload_id': upload_id
    })


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

    # --- Top 10 finishers (lowest races) per difficulty in a specific order ---
    ordered_levels = [
        'Speed makes me dizzy',
        'I live to ride',
        'Petrol in my veins'
    ]

    finish_by_difficulty = {}
    finish_difficulty_order = []

    # Fetch for the known levels in the requested order
    for lvl in ordered_levels:
        cur.execute('''
            SELECT f.name, f.races, f.difficulty, u.uploaded_at
            FROM finish_records f
            JOIN uploads u ON f.upload_id = u.id
            WHERE f.races IS NOT NULL AND f.difficulty = ?
            ORDER BY f.races ASC, f.name ASC
            LIMIT 10
        ''', (lvl,))
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            finish_by_difficulty[lvl] = rows
            finish_difficulty_order.append(lvl)

    conn.close()
    return {
        'lap_leaders': lap_leaders,
        'finish_by_difficulty': finish_by_difficulty,
        'finish_difficulty_order': finish_difficulty_order
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
    html.append('<h1>Finish Leaders (top 10 per difficulty)</h1>')
    for diff in data.get('finish_difficulty_order', []):
        rows = data['finish_by_difficulty'].get(diff, [])
        label = diff if diff and diff != 'Unknown' else 'Unknown'
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
    """Return top lap times filtered by car, track, driver. Query params: car, track, driver, limit.

    If neither car nor track is specified, return the best time for each car/track combination
    (respecting an optional driver filter). If car or track is specified, return the top N rows
    ordered by time (limit controlled by the `limit` parameter, default 10).
    """
    car = request.args.get('car')
    track = request.args.get('track')
    driver = request.args.get('driver')

    # convert car/track names to numeric indexes if provided
    car_idx_val = car_index_from_name(car) if car else None
    track_idx_val = track_index_from_name(track) if track else None

    # treat limit param: if omitted, only apply default when doing top-N queries (car or track present)
    limit_str = request.args.get('limit')
    limit = None
    if limit_str is not None:
        try:
            limit = int(limit_str)
        except ValueError:
            limit = 10

    conn = sqlite3.connect(DB_FILENAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if not car and not track:
        # Return best time per car+track combination. Respect optional driver filter.
        params = []
        inner_where = 'WHERE time IS NOT NULL'
        if driver:
            inner_where += ' AND driver_name LIKE ?'
            params.append(f"%{driver}%")

        # subquery: best time per car_type and track_idx
        subq = f"SELECT car_type, track_idx, MIN(time) as min_time FROM lap_records {inner_where} GROUP BY car_type, track_idx"

        sql = f"""
        SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at
        FROM lap_records l
        JOIN uploads u ON l.upload_id = u.id
        JOIN ({subq}) m ON l.car_type = m.car_type AND l.track_idx = m.track_idx AND l.time = m.min_time
        ORDER BY l.car_type, l.track_idx
        """

        # Do not apply a LIMIT here — return the best entry for every car/track combination
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    else:
        # If both car and track specified: return top-N for that pair
        if car and track:
            # if conversion failed, no results
            if car_idx_val is None or track_idx_val is None:
                rows = []
            else:
                sql = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                params = [car_idx_val, track_idx_val]
                if driver:
                    sql += ' AND l.driver_name LIKE ?'
                    params.append(f"%{driver}%")
                sql += ' ORDER BY l.time ASC LIMIT ?'
                if limit is None:
                    limit = 10
                params.append(limit)
                cur.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()]
        else:
            # If only car specified: for each track return top-N rows for that car
            # If only track specified: for each car return top-N rows for that track
            if limit is None:
                limit = 10
            rows = []
            if car and not track:
                if car_idx_val is None:
                    rows = []
                else:
                    q = 'SELECT DISTINCT track_idx FROM lap_records WHERE car_type = ?'
                    params = [car_idx_val]
                    if driver:
                        q += ' AND driver_name LIKE ?'
                        params.append(f"%{driver}%")
                    cur.execute(q, params)
                    tracks = [r[0] for r in cur.fetchall()]
                    for t in tracks:
                        q2 = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                        p2 = [car_idx_val, t]
                        if driver:
                            q2 += ' AND l.driver_name LIKE ?'
                            p2.append(f"%{driver}%")
                        q2 += ' ORDER BY l.time ASC LIMIT ?'
                        p2.append(limit)
                        cur.execute(q2, p2)
                        rows.extend([dict(r) for r in cur.fetchall()])
            elif track and not car:
                if track_idx_val is None:
                    rows = []
                else:
                    q = 'SELECT DISTINCT car_type FROM lap_records WHERE track_idx = ?'
                    params = [track_idx_val]
                    if driver:
                        q += ' AND driver_name LIKE ?'
                        params.append(f"%{driver}%")
                    cur.execute(q, params)
                    cars = [r[0] for r in cur.fetchall()]
                    for c in cars:
                        q2 = 'SELECT l.car_type, l.track_idx, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_type = ? AND l.track_idx = ?'
                        p2 = [c, track_idx_val]
                        if driver:
                            q2 += ' AND l.driver_name LIKE ?'
                            p2.append(f"%{driver}%")
                        q2 += ' ORDER BY l.time ASC LIMIT ?'
                        p2.append(limit)
                        cur.execute(q2, p2)
                        rows.extend([dict(r) for r in cur.fetchall()])
            else:
                rows = []

    # map numeric columns back to names for API output
    mapped = []
    for r in rows:
        d = dict(r)
        # preserve numeric car_type/track_idx when present; try to derive car_type from name when missing
        if 'car_type' not in d or d.get('car_type') is None:
            # try to derive from car_name for backward compatibility
            if d.get('car_name'):
                d['car_type'] = car_index_from_name(d.get('car_name'))
        if 'track_idx' not in d or d.get('track_idx') is None:
            # no reliable fallback for track_idx in all cases; leave as-is
            d['track_idx'] = d.get('track_idx')
        # ensure human-readable names are present
        d['car_name'] = car_name_from_index(d.get('car_type')) if d.get('car_type') is not None else d.get('car_name')
        d['track_name'] = track_name_from_index(d.get('track_idx')) if d.get('track_idx') is not None else d.get('track_name')
        mapped.append(d)

    # ensure results are ordered by car index (numeric) and then track name
    try:
        if isinstance(mapped, list) and len(mapped) > 0:
            def sort_key(item):
                car_idx = item.get('car_type')
                # fallback: try to derive numeric index from car_name, else large number to push unknowns to end
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
    # limit drivers list to distinct non-empty names
    cur.execute("SELECT DISTINCT driver_name FROM lap_records WHERE driver_name IS NOT NULL AND driver_name <> '' ORDER BY driver_name")
    drivers = [r[0] for r in cur.fetchall()]
    conn.close()
    return jsonify({'cars': cars, 'tracks': tracks, 'drivers': drivers})


@app.route('/browse', methods=['GET'])
def browse_view():
    """HTML UI to browse top times with filters."""
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
      <div>
        <label>Car: <select id="car"><option value="">(any)</option></select></label>
        <label>Track: <select id="track"><option value="">(any)</option></select></label>
        <label>Driver: <input list="drivers" id="driver" placeholder="partial name"><datalist id="drivers"></datalist></label>
        <label>Limit: <input id="limit" type="number" min="1" value="10" style="width:60px"></label>
        <button id="filter">Filter</button>
      </div>
      <div id="results"></div>

    <script>
    async function loadMeta(){
      const res = await fetch('/api/meta');
      const meta = await res.json();
      const carSel = document.getElementById('car');
      meta.cars.forEach(c => { const opt = document.createElement('option'); opt.value = c; opt.text = c; carSel.add(opt); });
      const trackSel = document.getElementById('track');
      meta.tracks.forEach(t => { const opt = document.createElement('option'); opt.value = t; opt.text = t; trackSel.add(opt); });
      const datalist = document.getElementById('drivers');
      // clear existing options
      while (datalist.firstChild) datalist.removeChild(datalist.firstChild);
      // populate datalist with driver names for autocompletion
      meta.drivers.forEach(d => { if(d && d.trim() !== ''){ const opt = document.createElement('option'); opt.value = d; datalist.appendChild(opt); }});
    }

    async function doFilter(){
      const car = document.getElementById('car').value;
      const track = document.getElementById('track').value;
      const driver = document.getElementById('driver').value;
      const limit = document.getElementById('limit').value || 10;
      const params = new URLSearchParams();
      if(car) params.append('car', car);
      if(track) params.append('track', track);
      if(driver) params.append('driver', driver);
      params.append('limit', limit);
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
    </script>
    </body>
    </html>
    """
    )
    return html


if __name__ == '__main__':
    # bind to localhost only for safety
    app.run(host='127.0.0.1', port=8000, debug=True)
