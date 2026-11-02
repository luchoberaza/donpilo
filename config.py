import os

class Config:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    # Usamos la misma DB que la app real (app.py): pos.db
    DB_PATH = os.path.join(BASE_DIR, "pos.db")
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "donpilo-secret-key")
