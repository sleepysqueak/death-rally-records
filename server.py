from flask import Flask, request, jsonify, render_template_string
import tempfile
import os
from dataclasses import asdict
from typing import Any, List, Tuple, Union
import sqlite3
from datetime import datetime, UTC
import json

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

    uploaded_at = datetime.now(UTC).isoformat()
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
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1] or '.cfg')
        try:
            file.save(tmp.name)
            tmp.close()

            # Detect JSON uploads by filename extension or content type
            is_json_file = False
            try:
                fname_lower = (file.filename or '').lower()
                if fname_lower.endswith('.json') or (hasattr(file, 'mimetype') and getattr(file, 'mimetype') and 'json' in getattr(file, 'mimetype')):
                    is_json_file = True
            except Exception:
                is_json_file = False

            if is_json_file:
                # parse JSON payload from uploaded file and normalize into lists
                try:
                    with open(tmp.name, 'r', encoding='utf-8') as jf:
                        payload = json.load(jf)
                except Exception as e:
                    # skip invalid JSON files
                    summary_rows.append((file.filename, 0, 0, None, f'invalid JSON: {e}'))
                    continue

                # Normalize payload into lists of lap and finish record dicts
                lap_json_list = []
                finish_json_list = []

                def _consume_item(it):
                    if not isinstance(it, dict):
                        return
                    if 'lap_records' in it or 'finish_records' in it:
                        lap_json_list.extend(it.get('lap_records') or [])
                        finish_json_list.extend(it.get('finish_records') or [])
                        return
                    # Heuristics: objects with time/driver_name/car are lap records
                    if any(k in it for k in ('time', 'driver_name', 'driver', 'car_name', 'car_type', 'track_name', 'track_idx')):
                        lap_json_list.append(it)
                        return
                    # Objects with races/difficulty/name are finish records
                    if any(k in it for k in ('races', 'difficulty', 'difficulty_idx', 'name')):
                        finish_json_list.append(it)
                        return
                    # Fallback to lap
                    lap_json_list.append(it)

                if isinstance(payload, list):
                    for element in payload:
                        _consume_item(element)
                elif isinstance(payload, dict):
                    _consume_item(payload)
                else:
                    # unexpected root type
                    summary_rows.append((file.filename, 0, 0, None, 'unsupported JSON root'))
                    continue

                # Convert JSON dicts to dataclasses
                lap_records = []
                finish_records = []

                for item in lap_json_list:
                    # resolve car_type
                    car_type = None
                    if isinstance(item.get('car_type'), (int, float)):
                        car_type = int(item.get('car_type'))
                    else:
                        for key in ('car_name', 'car', 'vehicle'):
                            if item.get(key):
                                car_type = car_index_from_name(item.get(key))
                                break
                    # resolve track_idx
                    track_idx = None
                    if isinstance(item.get('track_idx'), (int, float)):
                        track_idx = int(item.get('track_idx'))
                    else:
                        for key in ('track_name', 'track'):
                            if item.get(key):
                                track_idx = track_index_from_name(item.get(key))
                                break
                    # time and driver_name
                    time_val = None
                    if 'time' in item and item.get('time') is not None:
                        try:
                            time_val = float(item.get('time'))
                        except Exception:
                            time_val = None
                    driver = item.get('driver_name') or item.get('driver') or item.get('name') or ''
                    lap_records.append(LapRecord(car_type, track_idx, time_val, driver))

                for item in finish_json_list:
                    name = item.get('name') or item.get('driver_name') or item.get('driver') or ''
                    races = None
                    if 'races' in item and item.get('races') is not None:
                        try:
                            races = int(item.get('races'))
                        except Exception:
                            races = None
                    # difficulty may be numeric index or textual name
                    diff_idx = None
                    if isinstance(item.get('difficulty_idx'), (int, float)):
                        diff_idx = int(item.get('difficulty_idx'))
                    elif item.get('difficulty') is not None:
                        # string name -> index
                        diff_idx = difficulty_index_from_name(str(item.get('difficulty')))
                    elif item.get('level') is not None:
                        diff_idx = difficulty_index_from_name(str(item.get('level')))

                    finish_records.append(FinishRecord(name, races, diff_idx))

                # If both lists empty, skip
                if not lap_records and not finish_records:
                    summary_rows.append((file.filename, 0, 0, None, 'no records found in JSON'))
                    continue

                # Filter out records with empty driver/name (do not import empty-name records)
                lap_before = len(lap_records)
                fin_before = len(finish_records)
                lap_records = [r for r in lap_records if (r.driver_name is not None and str(r.driver_name).strip() != '')]
                finish_records = [fr for fr in finish_records if (fr.name is not None and str(fr.name).strip() != '')]

                # If filtering removed all records, skip
                if not lap_records and not finish_records:
                    summary_rows.append((file.filename, 0, 0, None, 'all records had empty names, skipped'))
                    continue

                upload_id, laps_inserted, finishes_inserted = save_records(DB_FILENAME, file.filename, lap_records, finish_records)
                total_laps += laps_inserted
                total_finishes += finishes_inserted
                summary_rows.append((file.filename, laps_inserted, finishes_inserted, upload_id, None))

            else:
                # treat as a binary dr.cfg file and parse using existing parser
                try:
                    lap_records, finish_records = read_records(tmp.name)
                    # Filter out empty driver/name records from parsed cfg
                    lap_records = [r for r in lap_records if (r.driver_name is not None and str(r.driver_name).strip() != '')]
                    finish_records = [fr for fr in finish_records if (fr.name is not None and str(fr.name).strip() != '')]
                    upload_id, laps_inserted, finishes_inserted = save_records(DB_FILENAME, file.filename, lap_records, finish_records)
                    total_laps += laps_inserted
                    total_finishes += finishes_inserted
                    summary_rows.append((file.filename, laps_inserted, finishes_inserted, upload_id, None))
                except Exception as e:
                    summary_rows.append((file.filename, 0, 0, None, f'parse error: {e}'))
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
    for fn, lins, fins, uid, *rest in summary_rows:
        note = (rest[0] if rest and rest[0] else '')
        parts.append(f"<li>{fn}: {lins} lap(s), {fins} finish(es)" + (f" (upload id {uid})" if uid else '') + (f" - {note}" if note else '') + '</li>')
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

# Register API routes implemented in separate module to keep this file small
from top_times import register_routes as register_top_times
register_top_times(app, DB_FILENAME, car_index_from_name, track_index_from_name, car_name_from_index, track_name_from_index, difficulty_name_from_index)

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
        <label>Records per Car/Track: <input id="limit" type="number" min="1" value="1" style="width:60px"></label>
        <label style="margin-left:12px"><input id="allow_dups" type="checkbox"> Allow multiple times per driver</label>
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
        <button id="export_csv" style="margin-left:8px">Export CSV</button>
        <button id="export_tsv" style="margin-left:6px">Export TSV</button>
        <button id="export_json" style="margin-left:6px">Export JSON</button>
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

    // last fetched results cached on the client for exporting
    window._lastTopTimes = [];

    async function doFilter(){
      try{
        const carVals = _getSelectedValuesFromMulti('car_multisel');
        const trackVals = _getSelectedValuesFromMulti('track_multisel');
        const driverVals = _getSelectedValuesFromMulti('driver_multisel');
        const limit = document.getElementById('limit').value || 1;
        const allowDups = document.getElementById('allow_dups') ? document.getElementById('allow_dups').checked : true;
        const params = new URLSearchParams();
        carVals.forEach(v => params.append('car', v));
        trackVals.forEach(v => params.append('track', v));
        driverVals.forEach(v => params.append('driver', v));
        if(limit) params.append('limit', limit);
        params.append('allow_dups', allowDups ? '1' : '0');
        const res = await fetch('/api/top_times?' + params.toString());
        const data = await res.json();
        const rows = (data && data.results) ? data.results : [];
        // cache results locally for client-side export
        window._lastTopTimes = rows.slice();
        renderResults(rows);
      }catch(e){
        console && console.error && console.error('doFilter error', e);
        const container = document.getElementById('results');
        if(container) container.innerHTML = '<p style="color:red">Error fetching results. See console for details.</p>';
      }
    }

    function renderResults(rows){
      const container = document.getElementById('results');
      if(!container){ return; }
      try{
        if(!rows || rows.length === 0){ container.innerHTML = '<p>No results</p>'; return; }
        // New left-most column shows racer_rank (position among drivers based on each driver's best time)
        let html = '<table><tr><th>Racer Rank</th><th>#</th><th>Car</th><th>Track</th><th>Driver</th><th>Time (s)</th><th>Uploaded</th></tr>';
        rows.forEach((r) => {
          const racerRank = (r.racer_rank !== undefined && r.racer_rank !== null) ? r.racer_rank : '';
          const rank = (r.rank !== undefined && r.rank !== null) ? r.rank : '';
          const time = (r.time !== null && r.time !== undefined && !isNaN(Number(r.time))) ? Number(r.time).toFixed(2) : '';
          let uploaded_td = '<td></td>';
          if(r.uploaded_at){
            try{
              const iso = new Date(r.uploaded_at).toISOString();
              const hover = iso.replace('T',' ').replace(/Z$/,'');
              const display = iso.split('T')[0];
              uploaded_td = `<td title="${hover}">${display}</td>`;
            }catch(e){ uploaded_td = `<td>${r.uploaded_at}</td>`; }
          }
          const car = r.car_name || '';
          const track = r.track_name || '';
          const driver = r.driver_name || '';
          html += `<tr><td>${racerRank}</td><td>${rank}</td><td>${car}</td><td>${track}</td><td>${driver}</td><td>${time}</td>${uploaded_td}</td>`;
        });
        html += '</table>';
        container.innerHTML = html;
      }catch(err){ console && console.error && console.error('renderResults error', err); container.innerHTML = '<p style="color:red">Error rendering results</p>'; }
    }

    // Client-side export helpers (use cached last results; no server call)
    function _escapeCsv(val){
      const s = val === null || val === undefined ? '' : String(val);
      return '"' + s.replace(/"/g, '""') + '"';
    }
    // Avoid regex literal parsing issues by using split/join to replace control characters
    function _cleanTsv(val){
      if(val === null || val === undefined) return '';
      let s = String(val);
      s = s.split('\\t').join(' ');
      s = s.split('\\n').join(' ');
      s = s.split('\\r').join(' ');
      return s;
    }

    function exportClient(format){
      try{
        const rows = (window._lastTopTimes && Array.isArray(window._lastTopTimes)) ? window._lastTopTimes : [];
        if(!rows || rows.length === 0){ alert('No results to export. Run Filter first.'); return; }
        const now = new Date().toISOString().replace(/[:\\-]/g,'').split('.')[0];
        const filenameBase = 'top_times_' + now;
        if(format === 'json'){
          const out = JSON.stringify(rows, null, 2);
          const blob = new Blob([out], {type:'application/json;charset=utf-8'});
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a'); a.href = url; a.download = filenameBase + '.json'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); return;
        }
        const delim = format === 'tsv' ? '\\t' : ',';
        // include Racer Rank as the first column in exports
        const headers = ['Racer Rank','Rank','Car','Track','Driver','Time','Uploaded'];
        const lines = [headers.join(delim)];
        rows.forEach(r=>{
          const racerRank = r.racer_rank !== undefined && r.racer_rank !== null ? String(r.racer_rank) : '';
          const rank = r.rank !== undefined && r.rank !== null ? String(r.rank) : '';
          const car = r.car_name || '';
          const track = r.track_name || '';
          const driver = r.driver_name || '';
          const time = (r.time !== null && r.time !== undefined && !isNaN(Number(r.time))) ? Number(r.time).toFixed(2) : '';
          const uploaded = r.uploaded_at || '';
          if(format === 'csv'){
            // Emit numeric fields without quotes so spreadsheets import them as numbers
            const rrField = (racerRank !== '' && !isNaN(Number(racerRank))) ? String(Number(racerRank)) : '';
            const rankField = (rank !== '' && !isNaN(Number(rank))) ? String(Number(rank)) : '';
            // sanitize: remove any stray double-quotes just in case
            const sanitize = s => s.replace(/"/g, '');
            const rrSan = sanitize(rrField);
            const rankSan = sanitize(rankField);
            lines.push([rrSan, rankSan, _escapeCsv(car), _escapeCsv(track), _escapeCsv(driver), _escapeCsv(time), _escapeCsv(uploaded)].join(delim));
          }else{
            lines.push([_cleanTsv(racerRank), _cleanTsv(rank), _cleanTsv(car), _cleanTsv(track), _cleanTsv(driver), _cleanTsv(time), _cleanTsv(uploaded)].join(delim));
          }
        });
        const outText = lines.join('\\n');
        const blob = new Blob([outText], {type:'text/plain;charset=utf-8'});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a'); a.href = url; a.download = filenameBase + (format === 'tsv' ? '.tsv' : '.csv'); document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      }catch(e){ console && console.error && console.error('export error', e); alert('Export failed - see console'); }
    }

    // Wire client export buttons (guard element presence)
    try{
      const bCsv = document.getElementById('export_csv'); if(bCsv) bCsv.addEventListener('click', ()=> exportClient('csv'));
      const bTsv = document.getElementById('export_tsv'); if(bTsv) bTsv.addEventListener('click', ()=> exportClient('tsv'));
      const bJson = document.getElementById('export_json'); if(bJson) bJson.addEventListener('click', ()=> exportClient('json'));
    }catch(e){ console && console.error && console.error('export wiring error', e); }

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
