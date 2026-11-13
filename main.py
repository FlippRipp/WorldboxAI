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
    # 8321 by default: 8000 collides with SillyTavern and friends.
    # Bound to 0.0.0.0 so the paired-only, subnet-locked `/api/debug-bridge/lan/*`
    # surface is reachable on the local network; LocalOnlyExceptBridgeMiddleware
    # rejects all other non-loopback HTTP/WS requests with 403.
    port = int(os.environ.get("WB_PORT", "8321"))
    host = os.environ.get("WB_HOST", "0.0.0.0")
    uvicorn.run("backend.api.server:app", host=host, port=port, reload=reload)
