"""db_test.py - quick database test for Mask POS

Usage:
  python db_test.py

This script uses backend.py, so it works in standalone, host, or connect mode.
"""

from backend import backend_init, backend_mode, BASE_URL
import backend as B

APP_TITLE = "Mask POS"

def main():
    backend_init(APP_TITLE)
    print("Backend mode:", backend_mode())
    if backend_mode() != "standalone":
        print("Server:", BASE_URL)

    if backend_mode() in ("host", "connect"):
        try:
            http = B._http()
            j = http.get(BASE_URL + "/debug/db_test", timeout=30)
            print("DB self-test:", j)
            return
        except Exception as e:
            print("Could not call /debug/db_test, falling back to local test:", e)

    import pos_logic as L
    print("DB self-test:", L.db_self_test())

if __name__ == "__main__":
    main()
