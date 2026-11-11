"""
Zaram Viral Klipa as a desktop app.

    python desktop.py

Runs the same server the browser version runs, in this process, and puts a
native window in front of it instead of a browser tab. Nothing about the
app changes: it is the same FastAPI app, the same worker, the same database,
so a clip exported here and a clip exported from the browser are the same
clip. Closing the window stops the server with it.

If a server is already running on the port, this attaches to that one rather
than starting a second copy fighting over the same database.
"""

import socket
import sys
import threading
import time

import settings

WINDOW = "Zaram Viral Klipa"
WIDTH, HEIGHT = 1280, 860
MIN_SIZE = (900, 640)
START_TIMEOUT = 25


def already_running(host, port, timeout=0.4):
    """Something is listening, so leave it alone and use it."""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def serve(host, port):
    """Run uvicorn on a worker thread. Returns once it is answering."""
    import uvicorn
    import server

    config = uvicorn.Config(server.app, host=host, port=port,
                            log_level="warning")
    engine = uvicorn.Server(config)
    # Signal handlers only install on the main thread, and this is not it.
    engine.install_signal_handlers = lambda: None
    threading.Thread(target=engine.run, daemon=True).start()

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if engine.started and already_running(host, port):
            return engine
        time.sleep(0.2)
    raise RuntimeError(
        f"The server did not come up on {host}:{port} within "
        f"{START_TIMEOUT} seconds.")


def main():
    host = "127.0.0.1" if settings.HOST == "0.0.0.0" else settings.HOST
    port = settings.PORT
    url = f"http://{host}:{port}"

    engine = None
    if already_running(host, port):
        print(f"Attaching to the Zaram Viral Klipa already running at {url}")
    else:
        print(f"Starting Zaram Viral Klipa at {url}")
        engine = serve(host, port)

    try:
        import webview
    except ImportError:
        # No window toolkit here, so fall back to the browser rather than
        # leaving someone with a server and nothing to look at.
        import webbrowser
        print("pywebview is not installed, opening your browser instead.\n"
              "Install it with: pip install pywebview")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    webview.create_window(WINDOW, url, width=WIDTH, height=HEIGHT,
                          min_size=MIN_SIZE, confirm_close=False)
    webview.start()             # returns when the window is closed

    if engine is not None:
        engine.should_exit = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
