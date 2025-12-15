import sys
from pathlib import Path
import struct
import os
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class LapRecord:
    car_type: int        # 0..5 index for car type
    track_idx: int       # 1..18 index for track
    time: Optional[float]
    driver_name: str


@dataclass
class FinishRecord:
    name: str
    races: Optional[int]
    difficulty: Optional[str]


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
            difficulty_map = {0: 'Speed makes me dizzy', 1: 'I live to ride', 2: 'Petrol in my veins'}
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
                    diff_byte = struct.unpack_from('<B', chunk, 16)[0]
                    difficulty = difficulty_map.get(diff_byte, f'unknown(0x{diff_byte:02X})')
                except Exception:
                    difficulty = None

                finish_records.append(FinishRecord(name, races, difficulty))

                rec_no += 1

    except FileNotFoundError:
        print(f'File not found: {file_path}', file=sys.stderr)
    except PermissionError:
        print(f'Permission denied: {file_path}', file=sys.stderr)

    return lap_records, finish_records


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
