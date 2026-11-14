import os
import subprocess
import csv
import io

def get_process_rss_mb() -> float:
    """Returns process RSS in MB across Linux (/proc/self/status) and Windows (tasklist)."""
    # Linux (Render container)
    if os.path.exists('/proc/self/status'):
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    if line.startswith('VmRSS:'):
                        parts = line.split()
                        return round(float(parts[1]) / 1024.0, 1)
        except Exception:
            pass
    # Windows
    try:
        pid = os.getpid()
        out = subprocess.check_output(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV'], timeout=3).decode('utf-8', errors='ignore')
        reader = list(csv.reader(io.StringIO(out.strip())))
        if len(reader) >= 2:
            mem_raw = reader[1][4]  # e.g. "12,680 K"
            mem_k = float(mem_raw.split()[0].replace(',', ''))
            return round(mem_k / 1024.0, 1)
    except Exception:
        pass
    return 0.0

if __name__ == '__main__':
    print(f"Current Process RSS: {get_process_rss_mb():.1f} MB")
