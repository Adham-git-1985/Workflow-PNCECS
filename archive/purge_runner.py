from app import app
from archive.purge import purge_archived_files

with app.app_context():
    purge_archived_files(30)
