from flask_sqlalchemy import SQLAlchemy

# Custom query class that adds server-side sorting for /portal tables.
from utils.portal_query import PortalSortableQuery
from flask_login import LoginManager

db = SQLAlchemy(query_class=PortalSortableQuery)
login_manager = LoginManager()
