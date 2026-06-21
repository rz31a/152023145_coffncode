"""
CuffnCode — Unified Entry Point
================================
IFB 206 Komputasi Paralel — Evaluasi 3

Jalankan dengan:
    python main.py

Otomatis:
  1. Menjalankan parallel pipeline (T1 Generator → T2 Filter → T3 Detector)
  2. Menyajikan dashboard.html via HTTP server (localhost:8765)
  3. Membuka browser ke dashboard secara otomatis
  4. Live state ditulis ke output/state.json setiap 250 ms (dibaca oleh dashboard)
  5. Benchmark Sequential vs Parallel + Speedup & Efficiency
  6. CPU/RAM monitoring via psutil
  7. Raw & filtered signal history untuk dashboard real-time
  8. CSV logging hasil BP ke output/results.csv

Author  : [REZA PUTRA PRATAMA]
NIM     : [152023145]
Kelas   : [AA]
"""

import argparse          # FIX C0415: pindah import ke top-level
import threading
import queue
import time
import math
import random
import json
import os
import csv
import webbrowser
import signal
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dataclasses import dataclass, asdict, field
from typing import Optional  # FIX W0611: hapus List yang tidak dipakai

try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False
    print("  [WARN] psutil tidak ditemukan. Install: pip install psutil")
    print("         CPU/RAM monitoring dinonaktifkan.\n")

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR  = os.path.join(BASE_DIR, "output")
LOG_PATH    = os.path.join(OUTPUT_DIR, "state.json")
CSV_PATH    = os.path.join(OUTPUT_DIR, "results.csv")
DASH_PATH   = os.path.join(BASE_DIR, "dashboard.html")
SERVER_PORT = 8765

# ── Shared state (thread-safe) ────────────────────────────────────────────────
raw_queue      = queue.Queue(maxsize=200)
filtered_queue = queue.Queue(maxsize=200)
stop_event     = threading.Event()
results_lock   = threading.Lock()

# ── Signal history untuk dashboard real-time ──────────────────────────────────
raw_history      = []
filtered_history = []
HISTORY_LEN      = 300


@dataclass
class PipelineState:
    """Menyimpan seluruh state pipeline yang dibagikan antar thread."""

    running: bool = False
    t1_count: int = 0
    t2_count: int = 0
    t3_count: int = 0
    raw_q_size: int = 0
    filtered_q_size: int = 0
    systolic: Optional[float] = None
    diastolic: Optional[float] = None
    map_val: Optional[float] = None
    elapsed: float = 0.0
    t1_rate: float = 0.0
    t2_rate: float = 0.0
    t3_rate: float = 0.0
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    speedup: float = 0.0
    efficiency: float = 0.0
    benchmark_seq: float = 0.0
    benchmark_par: float = 0.0
    raw_samples: list = field(default_factory=list)
    filtered_samples: list = field(default_factory=list)


state = PipelineState()


# ══════════════════════════════════════════════════════════════════════════════
# SIGNAL MODEL
# ══════════════════════════════════════════════════════════════════════════════

def oscillometric_signal(t: float, sbp: float = 120, dbp: float = 80,
                          noise_std: float = 0.04) -> float:
    """
    Simulasi sinyal tekanan cuff oscillometric.
    Model envelope Gaussian berpusat di MAP, di atas ramp deflasi cuff linear.
    """
    hr_hz     = 1.2
    map_p     = dbp + (sbp - dbp) / 3.0
    cuff_norm = max(0.0, 1.0 - t / 30.0)
    cuff_mmhg = cuff_norm * sbp
    sigma     = (sbp - dbp) / 4.0
    envelope  = math.exp(-((cuff_mmhg - map_p) ** 2) / (2 * sigma ** 2))
    osc       = envelope * 0.5 * math.sin(2 * math.pi * hr_hz * t)
    hum       = 0.08 * math.sin(2 * math.pi * 50 * t)
    noise     = random.gauss(0, noise_std)
    return osc + hum + noise


# ══════════════════════════════════════════════════════════════════════════════
# THREAD 1 — Signal Generator
# ══════════════════════════════════════════════════════════════════════════════

def thread_signal_generator(sbp=120, dbp=80, fs=200):
    """Hasilkan sampel tekanan mentah pada fs Hz dan kirim ke raw_queue."""
    interval = 1.0 / fs
    t        = 0.0
    t_start  = time.perf_counter()
    count    = 0

    while not stop_event.is_set():
        sample = oscillometric_signal(t, sbp, dbp)
        try:
            raw_queue.put(sample, timeout=0.1)
            count += 1
            t     += interval

            raw_history.append(sample)
            if len(raw_history) > HISTORY_LEN:
                raw_history.pop(0)

            with results_lock:
                state.t1_count   = count
                state.raw_q_size = raw_queue.qsize()
                state.t1_rate    = round(
                    count / max(1e-6, time.perf_counter() - t_start), 1
                )
        except queue.Full:
            pass

        time.sleep(max(0, interval - 0.0002))


# ══════════════════════════════════════════════════════════════════════════════
# THREAD 2 — Signal Filter
# ══════════════════════════════════════════════════════════════════════════════

class NotchFilter:
    """IIR notch filter orde 2 untuk meredam frekuensi 50 Hz pada fs=200 Hz."""

    def __init__(self, f0=50, fs=200, bandwidth=4):
        """Inisialisasi koefisien notch filter."""
        w0       = 2 * math.pi * f0 / fs
        bw       = 2 * math.pi * bandwidth / fs
        self.r   = 1 - bw / 2
        self.cos = math.cos(w0)
        self.b0  = 1.0
        self.b1  = -2 * self.cos
        self.b2  = 1.0
        self.a1  = -2 * self.r * self.cos
        self.a2  = self.r ** 2
        self.x1  = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x):
        """Proses satu sampel input dan kembalikan sampel terfilter."""
        y = (self.b0 * x + self.b1 * self.x1 + self.b2 * self.x2
             - self.a1 * self.y1 - self.a2 * self.y2)
        self.x2, self.x1 = self.x1, x
        self.y2, self.y1 = self.y1, y
        return y


def thread_signal_filter(window=15):
    """Konsumsi raw_queue → notch filter → moving-average → filtered_queue."""
    notch   = NotchFilter(f0=50, fs=200)
    buf     = []
    count   = 0
    t_start = time.perf_counter()

    while not stop_event.is_set():
        try:
            raw = raw_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        notched  = notch.process(raw)
        buf.append(notched)
        if len(buf) > window:
            buf.pop(0)
        smoothed = sum(buf) / len(buf)

        filtered_history.append(smoothed)
        if len(filtered_history) > HISTORY_LEN:
            filtered_history.pop(0)

        try:
            filtered_queue.put(smoothed, timeout=0.1)
            count += 1
            with results_lock:
                state.t2_count        = count
                state.filtered_q_size = filtered_queue.qsize()
                state.t2_rate         = round(
                    count / max(1e-6, time.perf_counter() - t_start), 1
                )
        except queue.Full:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# THREAD 3 — BP Detector
# ══════════════════════════════════════════════════════════════════════════════

def thread_bp_detector(analysis_window=400, sbp_target=120, dbp_target=80):
    # FIX W0613: hapus parameter 'fs' yang tidak digunakan
    """
    Konsumsi filtered_queue, deteksi SBP/DBP menggunakan algoritma oscillometric:
      - MAP  = posisi puncak amplitudo TERBESAR (envelope maksimum)
      - SBP  = puncak pertama SEBELUM MAP dengan amplitudo >= 45% max
      - DBP  = puncak terakhir SETELAH MAP dengan amplitudo >= 45% max
    Posisi puncak dikonversi ke mmHg via interpolasi tekanan cuff linear.
    """
    buf     = []
    count   = 0
    t_start = time.perf_counter()

    # FIX C0103: ganti ke snake_case
    sbp_tgt = sbp_target
    dbp_tgt = dbp_target
    # FIX W0612: MAP_TARGET tidak dipakai, hapus

    while not stop_event.is_set():
        try:
            sample = filtered_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        buf.append(sample)

        if len(buf) >= analysis_window:
            peaks = []
            for i in range(1, len(buf) - 1):
                if buf[i] > buf[i-1] and buf[i] > buf[i+1] and buf[i] > 0.01:
                    peaks.append((i, buf[i]))

            if len(peaks) >= 4:
                max_amp  = max(p[1] for p in peaks)
                map_peak = next((p for p in peaks if p[1] == max_amp), None)

                # FIX E0606: inisialisasi sbp_peak & dbp_peak di luar if
                sbp_peak = None
                dbp_peak = None

                if map_peak:
                    map_idx    = map_peak[0]
                    before_map = [p for p in peaks if p[0] < map_idx]
                    after_map  = [p for p in peaks if p[0] > map_idx]

                    sbp_peak = next(
                        (p for p in before_map if p[1] >= 0.45 * max_amp),
                        before_map[0] if before_map else None
                    )
                    dbp_peak = next(
                        (p for p in reversed(after_map) if p[1] >= 0.45 * max_amp),
                        after_map[-1] if after_map else None
                    )

                if sbp_peak and dbp_peak and map_peak:
                    n       = len(buf)
                    sbp_pos = sbp_peak[0] / n
                    dbp_pos = dbp_peak[0] / n
                    map_pos = map_peak[0] / n

                    sbp_mmhg = sbp_tgt * (1.0 - sbp_pos) + dbp_tgt * sbp_pos
                    dbp_mmhg = sbp_tgt * (1.0 - dbp_pos) + dbp_tgt * dbp_pos
                    map_mmhg = sbp_tgt * (1.0 - map_pos) + dbp_tgt * map_pos

                    sbp_mmhg = max(90,  min(180, sbp_mmhg))
                    dbp_mmhg = max(50,  min(100, dbp_mmhg))
                    map_mmhg = dbp_mmhg + (sbp_mmhg - dbp_mmhg) / 3.0

                    if sbp_mmhg > dbp_mmhg:
                        count += 1
                        with results_lock:
                            state.systolic  = round(sbp_mmhg, 1)
                            state.diastolic = round(dbp_mmhg, 1)
                            state.map_val   = round(map_mmhg, 1)
                            state.t3_count  = count
                            state.t3_rate   = round(
                                count / max(1e-6, time.perf_counter() - t_start),
                                3
                            )

                        # FIX W1514: tambah encoding='utf-8'
                        os.makedirs(OUTPUT_DIR, exist_ok=True)
                        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
                            writer = csv.writer(f)
                            writer.writerow([
                                round(time.time(), 3),
                                round(sbp_mmhg, 1),
                                round(dbp_mmhg, 1),
                                round(map_mmhg, 1)
                            ])

            buf = buf[analysis_window // 2:]


# ══════════════════════════════════════════════════════════════════════════════
# STATE EXPORTER
# ══════════════════════════════════════════════════════════════════════════════

def state_exporter():
    """Tulis state pipeline ke JSON setiap 250 ms agar dashboard bisa polling."""
    t_start = time.perf_counter()
    while not stop_event.is_set():
        with results_lock:
            state.elapsed         = round(time.perf_counter() - t_start, 1)
            state.raw_q_size      = raw_queue.qsize()
            state.filtered_q_size = filtered_queue.qsize()

            if PSUTIL_OK:
                state.cpu_usage    = psutil.cpu_percent(interval=None)
                state.memory_usage = psutil.virtual_memory().percent

            data = asdict(state)
            data["raw_samples"]      = list(raw_history[-HISTORY_LEN:])
            data["filtered_samples"] = list(filtered_history[-HISTORY_LEN:])

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        # FIX W1514: tambah encoding='utf-8'
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)

        time.sleep(0.25)


# ══════════════════════════════════════════════════════════════════════════════
# BENCHMARK
# ══════════════════════════════════════════════════════════════════════════════

def benchmark_sequential(samples=5000):
    """Jalankan pipeline secara sequential (single-thread) dan ukur waktunya."""
    notch = NotchFilter()
    start = time.perf_counter()
    buf   = []
    for i in range(samples):
        x = oscillometric_signal(i / 200)
        x = notch.process(x)
        buf.append(x)
    return time.perf_counter() - start


def benchmark_parallel(duration=5):
    """Jalankan pipeline parallel selama duration detik dan ukur waktunya."""
    # FIX W0702: ganti bare except → except queue.Empty
    while not raw_queue.empty():
        try:
            raw_queue.get_nowait()
        except queue.Empty:
            pass
    while not filtered_queue.empty():
        try:
            filtered_queue.get_nowait()
        except queue.Empty:
            pass
    stop_event.clear()

    # FIX C0321: pisahkan multi-statement ke baris terpisah
    threads = [
        threading.Thread(
            target=thread_signal_generator, args=(120, 80), daemon=True
        ),
        threading.Thread(
            target=thread_signal_filter, daemon=True
        ),
        threading.Thread(
            target=thread_bp_detector,
            kwargs={"sbp_target": 120, "dbp_target": 80},
            daemon=True
        ),
    ]
    start = time.perf_counter()
    for t in threads:
        t.start()
    time.sleep(duration)
    stop_event.set()
    for t in threads:
        t.join(timeout=2)
    return time.perf_counter() - start


def run_benchmark():
    """Jalankan benchmark dan cetak hasilnya."""
    # FIX C0103: ganti NUM_WORKERS ke snake_case
    num_workers = 3  # T1 Generator, T2 Filter, T3 Detector

    # FIX W1309: hapus f-prefix pada string tanpa interpolasi
    print("\n" + "─" * 60)
    print("  BENCHMARK — Sequential vs Parallel")
    print("─" * 60)
    print("  Menjalankan sequential benchmark (5000 sampel)...")
    seq_time = benchmark_sequential(samples=5000)

    print("  Menjalankan parallel benchmark (5 detik)...")
    par_time = benchmark_parallel(duration=5)

    speedup    = seq_time / max(par_time, 1e-6)
    efficiency = speedup / num_workers

    print("\n  ┌────────────────────────────────────────┐")
    print("  │  HASIL BENCHMARK                       │")
    print("  ├────────────────────────────────────────┤")
    print(f"  │  Sequential : {seq_time:.4f} s               │")
    print(f"  │  Parallel   : {par_time:.4f} s               │")
    print(f"  │  Speedup    : {speedup:.2f}x                    │")
    print(f"  │  Efficiency : {efficiency*100:.2f}%                  │")
    print(f"  │  Workers    : {num_workers} (T1 + T2 + T3)          │")
    print("  └────────────────────────────────────────┘\n")

    with results_lock:
        state.benchmark_seq = round(seq_time, 4)
        state.benchmark_par = round(par_time, 4)
        state.speedup       = round(speedup, 2)
        state.efficiency    = round(efficiency * 100, 2)

    return seq_time, par_time, speedup, efficiency


# ══════════════════════════════════════════════════════════════════════════════
# HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════

class DashboardHandler(SimpleHTTPRequestHandler):
    """
    Sajikan file dari BASE_DIR.
    Tambahkan header CORS agar fetch() dari browser tidak diblokir.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):  # pylint: disable=W0622,W0221
        pass  # Matikan log HTTP agar tidak mengganggu output konsol pipeline


def start_http_server():
    """Jalankan HTTP server di thread terpisah (daemon)."""
    server = HTTPServer(("localhost", SERVER_PORT), DashboardHandler)
    t = threading.Thread(target=server.serve_forever, name="T-HTTPServer", daemon=True)
    t.start()
    return server


# ══════════════════════════════════════════════════════════════════════════════
# PIPELINE RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def init_csv():
    """Buat header CSV jika file belum ada."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    if not os.path.exists(CSV_PATH):
        # FIX W1514: tambah encoding='utf-8'
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "systolic_mmhg", "diastolic_mmhg", "map_mmhg"])


def run_pipeline(duration=30, sbp=120, dbp=80):
    """Spin up semua thread dan jalankan selama `duration` detik."""
    raw_history.clear()
    filtered_history.clear()
    stop_event.clear()
    state.running = True

    threads = [
        threading.Thread(
            target=thread_signal_generator,
            args=(sbp, dbp), name="T1-Generator", daemon=True
        ),
        threading.Thread(
            target=thread_signal_filter,
            name="T2-Filter", daemon=True
        ),
        threading.Thread(
            target=thread_bp_detector,
            kwargs={"sbp_target": sbp, "dbp_target": dbp},
            name="T3-Detector", daemon=True
        ),
        threading.Thread(
            target=state_exporter,
            name="T0-Exporter", daemon=True
        ),
    ]

    cpu_info = f"CPU: {psutil.cpu_count()} core" if PSUTIL_OK else "CPU: N/A"
    sep = "=" * 60
    print(f"\n{sep}")
    print("  CuffnCode — Parallel BP Pipeline")
    print(f"  Target : SBP={sbp} mmHg  |  DBP={dbp} mmHg")
    print(f"  Durasi : {duration}s  |  {cpu_info}")
    print(f"  Threads: {len(threads)-1} worker + 1 exporter")
    print(f"  Dashboard : http://localhost:{SERVER_PORT}/dashboard.html")
    print(f"{sep}\n")

    for t in threads:
        t.start()
        print(f"  [+] {t.name} started")
    print()

    try:
        for _ in range(duration * 4):
            time.sleep(0.25)
            with results_lock:
                sbp_s = f"{state.systolic:.1f}"   if state.systolic  else "—"
                dbp_s = f"{state.diastolic:.1f}"  if state.diastolic else "—"
                cpu_s = f"{state.cpu_usage:.0f}%" if PSUTIL_OK       else "—"
                ram_s = f"{state.memory_usage:.0f}%" if PSUTIL_OK    else "—"
            print(
                f"\r  T={state.elapsed:5.1f}s | "
                f"Prod:{state.t1_count:6d} | "
                f"Filt:{state.t2_count:6d} | "
                f"Read:{state.t3_count:3d} | "
                f"BP:{sbp_s}/{dbp_s} | "
                f"CPU:{cpu_s} RAM:{ram_s}   ",
                end="", flush=True
            )
    except KeyboardInterrupt:
        print("\n\n  [!] Dihentikan oleh pengguna")
    finally:
        stop_event.set()
        state.running = False
        for t in threads:
            t.join(timeout=2)
        # FIX W1514: tambah encoding='utf-8'
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            data = asdict(state)
            data["raw_samples"]      = list(raw_history[-HISTORY_LEN:])
            data["filtered_samples"] = list(filtered_history[-HISTORY_LEN:])
            json.dump(data, f)

        print("\n\n  Pipeline selesai.")
        print(
            f"  Hasil Akhir — SBP:{state.systolic} | "
            f"DBP:{state.diastolic} | MAP:{state.map_val}"
        )
        if state.speedup > 0:
            print(
                f"  Benchmark   — Speedup:{state.speedup}x | "
                f"Efficiency:{state.efficiency}%"
            )
        print(f"  CSV tersimpan di: {CSV_PATH}")
        print(f"{'='*60}\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="CuffnCode Parallel BP Pipeline",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--sbp", type=int, default=120,
                        help="Target Systolic BP (mmHg)")
    parser.add_argument("--dbp", type=int, default=80,
                        help="Target Diastolic BP (mmHg)")
    parser.add_argument("--duration", type=int, default=30,
                        help="Durasi pipeline (detik)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Jangan buka browser otomatis")
    parser.add_argument("--no-benchmark", action="store_true",
                        help="Skip benchmark Sequential vs Parallel")
    return parser.parse_args()


def main():
    """Entry point utama program."""
    args = parse_args()

    # FIX C0325: hapus kurung tidak perlu setelah 'not'
    # FIX C0321: pisahkan multi-statement ke baris terpisah
    if not 90 <= args.sbp <= 180:
        print("ERROR: SBP harus antara 90-180 mmHg")
        sys.exit(1)
    if not 50 <= args.dbp <= 110:
        print("ERROR: DBP harus antara 50-110 mmHg")
        sys.exit(1)
    if args.sbp <= args.dbp:
        print("ERROR: SBP harus lebih besar dari DBP")
        sys.exit(1)
    if not os.path.exists(DASH_PATH):
        print(f"WARNING: dashboard.html tidak ditemukan di {DASH_PATH}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    init_csv()

    # FIX W1514: tambah encoding='utf-8'
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        data = asdict(state)
        data["raw_samples"]      = []
        data["filtered_samples"] = []
        json.dump(data, f)

    http_server = start_http_server()
    print(f"\n  [HTTP] Server aktif di http://localhost:{SERVER_PORT}")

    if not args.no_browser:
        time.sleep(0.5)
        url = f"http://localhost:{SERVER_PORT}/dashboard.html"
        webbrowser.open(url)
        print(f"  [Browser] Membuka {url}")

    # FIX W0613: ganti sig, frame → _sig, _frame (konvensi unused arg)
    def _sigint(_sig, _frame):
        stop_event.set()
        print("\n\n  [!] Program dihentikan oleh pengguna.")
        http_server.shutdown()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    if not args.no_benchmark:
        run_benchmark()
        stop_event.clear()
    else:
        print("  [!] Benchmark dilewati (--no-benchmark)")

    run_pipeline(duration=args.duration, sbp=args.sbp, dbp=args.dbp)

    # FIX W1309: hapus f-prefix pada string tanpa interpolasi
    print("  [HTTP] Server masih aktif.")
    print(f"  URL   : http://localhost:{SERVER_PORT}/dashboard.html")
    print("  Tekan Ctrl+C untuk keluar sepenuhnya.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  [!] Server dihentikan.")
        http_server.shutdown()


if __name__ == "__main__":
    main()