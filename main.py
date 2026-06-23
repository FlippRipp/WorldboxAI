import uvicorn
import sys
import os

if __name__ == "__main__":
    reload = "--reload" in sys.argv

    pid_file = ".backend_pid.tmp"
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    print(f"Starting WorldBox API Server (reload={reload})...")
    uvicorn.run("backend.api.server:app", host="127.0.0.1", port=8000, reload=reload)
