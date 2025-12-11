from flask import Flask, request, jsonify, render_template_string
import tempfile
import os
from dataclasses import asdict
from typing import Any
import sqlite3
from datetime import datetime

# Import the existing parser
from records import read_records

app = Flask(__name__)

UPLOAD_FORM = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Death Rally Records</title>
  <style>
    :root{
      --bg:#0b1220; --panel:#0f1624; --muted:#9fb0c8; --text:#e6eef8; --accent:#79b8ff; --border:#22262d;
    }
    html,body{height:100%;}
    body { background: var(--bg); color: var(--text); font-family: Arial, Helvetica, sans-serif; margin:0; padding:24px; }
    a { color: var(--accent); }
    a:hover { color: #a7d2ff; }
    .container { max-width:900px; margin:0 auto; }
    h1, h2 { color: var(--text); }
    ul { color: var(--muted); }
    form { background: var(--panel); padding:16px; border:1px solid var(--border); border-radius:6px; }
    input[type=file] { background: transparent; color: var(--text); }
    input, select, button { background: #111621; color: var(--text); border:1px solid var(--border); padding:8px 10px; margin-right:8px; border-radius:4px; }
    button { background: linear-gradient(#202734,#151826); cursor:pointer; }
    table { border-collapse: collapse; margin-top:12px; width:100%; }
    th, td { border:1px solid var(--border); padding:8px; }
    th { background:#0e1624; color: var(--muted); }
  </style>
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
"""


@app.route('/', methods=['GET'])
def index():
    return render_template_string(UPLOAD_FORM)


def dataclass_list_to_jsonable(lst: Any):
    # convert list of dataclasses to list of dicts
    return [asdict(x) for x in lst]


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
        rec_no INTEGER,
        car_name TEXT,
        track_name TEXT,
        idx INTEGER,
        time REAL,
        driver_name TEXT,
        FOREIGN KEY(upload_id) REFERENCES uploads(id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS finish_records (
        id INTEGER PRIMARY KEY,
        upload_id INTEGER,
        rec_no INTEGER,
        name TEXT,
        races INTEGER,
        difficulty TEXT,
        FOREIGN KEY(upload_id) REFERENCES uploads(id)
    )
    """)

    # Indexes to speed up common queries (leaderboards, top_times, meta)
    # Composite index for car+track+idx lookups
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_track_idx ON lap_records(car_name, track_name, idx)')
    # Indexes for time-based ordering and filtering
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_car_time ON lap_records(car_name, time)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_lap_track_time ON lap_records(track_name, time)')
    # Driver name lookup (used with LIKE) — helps with prefix searches; full substring LIKE may still be slow
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

    # Insert lap records, but avoid duplicates defined as same car_name+track_name+driver_name+time
    for r in lap_records:
        # Use IS for driver_name/time comparison to correctly handle NULLs
        cur.execute(
            'SELECT 1 FROM lap_records WHERE car_name = ? AND track_name = ? AND driver_name IS ? AND time IS ?',
            (r.car_name, r.track_name, r.driver_name, r.time)
        )
        if cur.fetchone():
            # duplicate found, skip insertion
            continue
        cur.execute(
            'INSERT INTO lap_records (upload_id, rec_no, car_name, track_name, idx, time, driver_name) VALUES (?, ?, ?, ?, ?, ?, ?)', 
            (upload_id, r.rec_no, r.car_name, r.track_name, r.idx, r.time, r.driver_name)
        )

    # Insert finish records as before
    finish_rows = []
    for fr in finish_records:
        finish_rows.append((upload_id, fr.rec_no, fr.name, fr.races, fr.difficulty))
    cur.executemany('INSERT INTO finish_records (upload_id, rec_no, name, races, difficulty) VALUES (?, ?, ?, ?, ?)', finish_rows)

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

    # best lap time per car and track (use idx to identify track order)
    cur.execute('''
    SELECT l.car_name, l.idx, l.track_name, l.driver_name, l.time, u.uploaded_at
    FROM lap_records l
    JOIN uploads u ON l.upload_id = u.id
    WHERE l.time IS NOT NULL AND l.time = (
        SELECT MIN(time) FROM lap_records WHERE car_name = l.car_name AND idx = l.idx AND time IS NOT NULL
    )
    ORDER BY l.car_name, l.track_name
    ''')
    lap_leaders = [dict(r) for r in cur.fetchall()]

    # best finish (fewest races) per rec_no, include upload time for the record that has the best_races
    cur.execute('''
    SELECT l.rec_no, l.name, l.races as best_races, u.uploaded_at
    FROM finish_records l
    JOIN uploads u ON l.upload_id = u.id
    WHERE l.races IS NOT NULL AND l.races = (
        SELECT MIN(races) FROM finish_records WHERE rec_no = l.rec_no AND races IS NOT NULL
    )
    ORDER BY l.rec_no
    ''')
    finish_leaders = [dict(r) for r in cur.fetchall()]

    conn.close()
    return {'lap_leaders': lap_leaders, 'finish_leaders': finish_leaders}


@app.route('/leaderboards', methods=['GET'])
def leaderboards_json():
    """Return leaderboards as JSON."""
    data = get_leaderboards()
    return jsonify(data)


@app.route('/leaderboards/view', methods=['GET'])
def leaderboards_view():
    """Simple HTML view of the leaderboards."""
    data = get_leaderboards()

    html = [
        '<html><head><title>Death Rally Leaderboards</title>',
        '<meta charset="utf-8">',
        '<style>',
        ':root{ --bg:#0b1220; --panel:#0f1624; --muted:#9fb0c8; --text:#e6eef8; --accent:#79b8ff; --border:#22262d; }',
        'html,body{height:100%;}',
        'body { background:var(--bg); color:var(--text); font-family:Arial,Helvetica,sans-serif; margin:0; padding:20px; }',
        'h1{color:var(--text);}',
        'table{border-collapse:collapse; width:100%; margin-top:12px}',
        'th,td{border:1px solid var(--border); padding:8px;}',
        'th{background:#0e1624; color:var(--muted);}',
        'tr:nth-child(even){background:#0f1624}',
        'a{color:var(--accent)}',
        '</style>',
        '</head><body>'
    ]
    html.append('<h1>Lap Leaders</h1>')
    html.append('<table><tr><th>Car</th><th>Track</th><th>Driver</th><th>Time (s)</th><th>Uploaded</th></tr>')
    for r in data['lap_leaders']:
        time_display = f"{r['time']:.2f}" if r.get('time') is not None else ''
        uploaded_display = r.get('uploaded_at') or ''
        html.append(f"<tr><td>{r['car_name']}</td><td>{r['track_name']}</td><td>{r['driver_name']}</td><td>{time_display}</td><td>{uploaded_display}</td></tr>")
    html.append('</table>')

    html.append('<h1>Finish Leaders</h1>')
    html.append('<table><tr><th>Rec No</th><th>Name</th><th>Best Races</th><th>Uploaded</th></tr>')
    for f in data['finish_leaders']:
        uploaded_display = f.get('uploaded_at') or ''
        html.append(f"<tr><td>{f['rec_no']}</td><td>{f['name']}</td><td>{f['best_races']}</td><td>{uploaded_display}</td></tr>")
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

        # subquery: best time per car and idx
        subq = f"SELECT car_name, idx, MIN(time) as min_time FROM lap_records {inner_where} GROUP BY car_name, idx"

        sql = f"""
        SELECT l.car_name, l.track_name, l.driver_name, l.time, u.uploaded_at
        FROM lap_records l
        JOIN uploads u ON l.upload_id = u.id
        JOIN ({subq}) m ON l.car_name = m.car_name AND l.idx = m.idx AND l.time = m.min_time
        ORDER BY l.car_name, l.track_name
        """

        # Do not apply a LIMIT here — return the best entry for every car/track combination
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    else:
        # If both car and track specified: return top-N for that pair
        if car and track:
            sql = 'SELECT l.car_name, l.track_name, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_name = ? AND l.track_name = ?'
            params = [car, track]
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
                # get distinct tracks for this car (respect driver filter)
                q = 'SELECT DISTINCT track_name FROM lap_records WHERE car_name = ?'
                params = [car]
                if driver:
                    q += ' AND driver_name LIKE ?'
                    params.append(f"%{driver}%")
                cur.execute(q, params)
                tracks = [r[0] for r in cur.fetchall()]
                for t in tracks:
                    q2 = 'SELECT l.car_name, l.track_name, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_name = ? AND l.track_name = ?'
                    p2 = [car, t]
                    if driver:
                        q2 += ' AND l.driver_name LIKE ?'
                        p2.append(f"%{driver}%")
                    q2 += ' ORDER BY l.time ASC LIMIT ?'
                    p2.append(limit)
                    cur.execute(q2, p2)
                    rows.extend([dict(r) for r in cur.fetchall()])
            elif track and not car:
                q = 'SELECT DISTINCT car_name FROM lap_records WHERE track_name = ?'
                params = [track]
                if driver:
                    q += ' AND driver_name LIKE ?'
                    params.append(f"%{driver}%")
                cur.execute(q, params)
                cars = [r[0] for r in cur.fetchall()]
                for c in cars:
                    q2 = 'SELECT l.car_name, l.track_name, l.driver_name, l.time, u.uploaded_at FROM lap_records l JOIN uploads u ON l.upload_id = u.id WHERE l.time IS NOT NULL AND l.car_name = ? AND l.track_name = ?'
                    p2 = [c, track]
                    if driver:
                        q2 += ' AND l.driver_name LIKE ?'
                        p2.append(f"%{driver}%")
                    q2 += ' ORDER BY l.time ASC LIMIT ?'
                    p2.append(limit)
                    cur.execute(q2, p2)
                    rows.extend([dict(r) for r in cur.fetchall()])
            else:
                rows = []

    # ensure results are ordered by car and track
    try:
        if isinstance(rows, list) and len(rows) > 0:
            rows.sort(key=lambda r: (r.get('car_name') or '', r.get('track_name') or ''))
    except Exception:
        pass

    conn.close()
    return jsonify({'results': rows})


@app.route('/api/meta', methods=['GET'])
def api_meta():
    """Return distinct car names, track names and driver names for UI selectors."""
    conn = sqlite3.connect(DB_FILENAME)
    cur = conn.cursor()
    cur.execute('SELECT DISTINCT car_name FROM lap_records ORDER BY car_name')
    cars = [r[0] for r in cur.fetchall()]
    cur.execute('SELECT DISTINCT track_name FROM lap_records ORDER BY track_name')
    tracks = [r[0] for r in cur.fetchall()]
    # limit drivers list to distinct non-empty names
    cur.execute("SELECT DISTINCT driver_name FROM lap_records WHERE driver_name IS NOT NULL AND driver_name <> '' ORDER BY driver_name")
    drivers = [r[0] for r in cur.fetchall()]
    conn.close()
    return jsonify({'cars': cars, 'tracks': tracks, 'drivers': drivers})


@app.route('/browse', methods=['GET'])
def browse_view():
    """HTML UI to browse top times with filters."""
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Browse Top Times</title>
      <style>
        :root{ --bg:#0b1220; --panel:#0f1624; --muted:#9fb0c8; --text:#e6eef8; --accent:#79b8ff; --border:#22262d }
        body { background:var(--bg); color:var(--text); font-family: Arial, Helvetica, sans-serif; padding: 20px; }
        select, input { margin-right: 8px; background:#111621; color:var(--text); border:1px solid var(--border); padding:6px 8px; }
        button { background: linear-gradient(#202734,#151826); color:var(--text); border:1px solid var(--border); padding:8px 10px; border-radius:4px; cursor:pointer; }
        table { border-collapse: collapse; margin-top: 12px; width:100%; }
        th, td { border: 1px solid var(--border); padding: 6px 8px; }
        th { background:#0e1624; color:var(--muted); }
        tr:nth-child(even){ background:#0f1624 }
      </style>
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
      let html = '<table><tr><th>#</th><th>Car</th><th>Track</th><th>Driver</th><th>Time (s)</th><th>Uploaded</th></tr>';
      rows.forEach((r,i) => {
        const time = r.time !== null ? r.time.toFixed(2) : '';
        const uploaded = r.uploaded_at ? new Date(r.uploaded_at).toLocaleString() : '';
        html += `<tr><td>${i+1}</td><td>${r.car_name}</td><td>${r.track_name}</td><td>${r.driver_name}</td><td>${time}</td><td>${uploaded}</td></tr>`;
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
    return html


if __name__ == '__main__':
    # bind to localhost only for safety
    app.run(host='127.0.0.1', port=8000, debug=True)
