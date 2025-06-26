# wsgi.py
# This is the new production entry point for our Gunicorn server.

# It is CRITICAL that this is the first thing that runs
from gevent import monkey
monkey.patch_all()

# Now that everything is patched, we can import our main Flask app
from main import app

# This allows Gunicorn to find the app object
if __name__ == "__main__":
    app.run()