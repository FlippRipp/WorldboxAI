import uvicorn
import sys
import os

# On Windows, stdout/stderr default to the legacy cp1252 ("charmap") codec,
# which raises UnicodeEncodeError when LLM output (em-dashes, curly quotes,
# accented characters, etc.) is printed or redirected to a log file.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

if __name__ == "__main__":
    reload = "--reload" in sys.argv

    pid_file = ".backend_pid.tmp"
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    print(f"Starting WorldBox API Server (reload={reload})...")
    uvicorn.run("backend.api.server:app", host="127.0.0.1", port=8000, reload=reload)
