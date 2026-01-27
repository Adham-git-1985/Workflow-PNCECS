from app import app
from extensions import db
from models import User

with app.app_context():
    user = User.query.filter_by(email="adham.pncecs@gmail.com").first()
    user.set_password("123456")   # اختر كلمة مرور جديدة
    db.session.commit()