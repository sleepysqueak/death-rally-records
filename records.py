import sys
from pathlib import Path
import struct
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple, Callable
import json


@dataclass
class LapRecord:
    car_type: int        # 0..5 index for car type
    track_idx: int       # 0..17 index for track
    time: Optional[float]
    driver_name: str


@dataclass
class FinishRecord:
    name: str
    races: Optional[int]
    difficulty: Optional[int]


def read_records(file_path, lap_start=0x56, races_start=0xA76) -> Tuple[List[LapRecord], List[FinishRecord]]:
    """Read and return parsed records from the cfg file.

    Returns (lap_records, finish_records) where:
      - lap_records: list of LapRecord (108 entries expected)
      - finish_records: list of FinishRecord (10 entries expected)

    This function no longer prints anything; it only parses the file into dataclasses.
    """
    lap_records: List[LapRecord] = []
    finish_records: List[FinishRecord] = []

    try:
        with open(file_path, 'rb') as f:
            # --- Lap records ---
            f.seek(lap_start)
            rec_no = 0
            total_lap = 6 * 18  # 6 car types, 18 records each
            while rec_no < total_lap:
                chunk = f.read(24)
                if not chunk or len(chunk) < 24:
                    break

                # parse time
                try:
                    sec = struct.unpack_from('<I', chunk, 16)[0]
                    centis = struct.unpack_from('<I', chunk, 20)[0]
                    centis_display = centis % 100
                    time_val = sec + centis_display / 100.0
                except Exception:
                    time_val = None

                name_bytes = chunk[0:10]
                name = name_bytes.split(b'\x00', 1)[0].decode('ascii', errors='replace').strip()

                # compute car type and index within that type
                car_type = rec_no // 18  # 0..5
                car_idx = (rec_no % 18)  # 0..17

                # Store numeric indexes instead of textual names
                lap_records.append(LapRecord(car_type, car_idx, time_val, name))

                rec_no += 1

            # --- Finish records ---
            f.seek(races_start)
            rec_no = 0
            total_finish = 10
            while rec_no < total_finish:
                chunk = f.read(20)
                if not chunk or len(chunk) < 20:
                    break

                name_bytes = chunk[0:10]
                name = name_bytes.split(b'\x00', 1)[0].decode('ascii', errors='replace').strip()
                try:
                    races = struct.unpack_from('<B', chunk, 12)[0]
                except Exception:
                    races = None

                try:
                    difficulty = struct.unpack_from('<B', chunk, 16)[0]
                except Exception:
                    difficulty = None

                finish_records.append(FinishRecord(name, races, difficulty))

                rec_no += 1

    except FileNotFoundError:
        print(f'File not found: {file_path}', file=sys.stderr)
    except PermissionError:
        print(f'Permission denied: {file_path}', file=sys.stderr)

    return lap_records, finish_records


def read_records_from_json(json_input, car_index_fn: Optional[Callable[[str], Optional[int]]] = None, track_index_fn: Optional[Callable[[str], Optional[int]]] = None, difficulty_index_fn: Optional[Callable[[str], Optional[int]]] = None) -> Tuple[List[LapRecord], List[FinishRecord]]:
    """Parse a JSON payload (string or already-parsed object) and return (lap_records, finish_records).

    The function applies heuristics similar to the server-side logic: it accepts either a dict with
    keys "lap_records"/"finish_records", a list of record-like objects, or individual objects.
    Optional mapping functions can be provided to convert human-readable car/track/difficulty names
    into numeric indexes required by the dataclasses.
    """
    # Accept either a JSON string or already parsed object
    if isinstance(json_input, str):
        try:
            payload = json.loads(json_input)
        except Exception:
            return ([], [])
    else:
        payload = json_input

    lap_json_list = []
    finish_json_list = []

    def _consume_item(it):
        if not isinstance(it, dict):
            return
        # explicit containers
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
        return ([], [])

    lap_records: List[LapRecord] = []
    finish_records: List[FinishRecord] = []

    for item in lap_json_list:
        car_type = None
        if isinstance(item.get('car_type'), (int, float)):
            car_type = int(item.get('car_type'))
        else:
            for key in ('car_name', 'car', 'vehicle'):
                if item.get(key):
                    if callable(car_index_fn):
                        car_type = car_index_fn(item.get(key))
                    else:
                        try:
                            car_type = int(item.get(key))
                        except Exception:
                            car_type = None
                    break

        track_idx = None
        if isinstance(item.get('track_idx'), (int, float)):
            track_idx = int(item.get('track_idx'))
        else:
            for key in ('track_name', 'track'):
                if item.get(key):
                    if callable(track_index_fn):
                        track_idx = track_index_fn(item.get(key))
                    else:
                        try:
                            track_idx = int(item.get(key))
                        except Exception:
                            track_idx = None
                    break

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

        diff_idx = None
        if isinstance(item.get('difficulty_idx'), (int, float)):
            diff_idx = int(item.get('difficulty_idx'))
        elif item.get('difficulty') is not None:
            if callable(difficulty_index_fn):
                diff_idx = difficulty_index_fn(str(item.get('difficulty')))
            else:
                try:
                    diff_idx = int(item.get('difficulty'))
                except Exception:
                    diff_idx = None
        elif item.get('level') is not None:
            if callable(difficulty_index_fn):
                diff_idx = difficulty_index_fn(str(item.get('level')))
            else:
                try:
                    diff_idx = int(item.get('level'))
                except Exception:
                    diff_idx = None

        finish_records.append(FinishRecord(name, races, diff_idx))

    return (lap_records, finish_records)


def print_records(lap_records: List[LapRecord], finish_records: List[FinishRecord]) -> None:
    """Pretty-print the parsed dataclasses."""
    print('Lap records:')
    for r in lap_records:
        if r.time is not None:
            print(f'car_type={r.car_type} track_idx={r.track_idx} time={r.time:.2f}s name="{r.driver_name}"')
        else:
            print(f'car_type={r.car_type} track_idx={r.track_idx} name="{r.driver_name}"')

    print('\nFinish records (names + races + difficulty):')
    for fr in finish_records:
        line = f'name="{fr.name}"'
        if fr.races is not None:
            line += f' races={fr.races}'
        if fr.difficulty is not None:
            line += f' difficulty={fr.difficulty}'
        print(line)


def main():
    cfg_path = os.path.join(str(Path(__file__).parent), 'dr.cfg')
    lap_records, finish_records = read_records(cfg_path)
    print_records(lap_records, finish_records)


if __name__ == '__main__':
    main()
