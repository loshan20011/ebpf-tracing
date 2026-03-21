from aggregator import app, fetch_from_agents_loop
import threading


if __name__ == "__main__":
    threading.Thread(target=fetch_from_agents_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=8000)
