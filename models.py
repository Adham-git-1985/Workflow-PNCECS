from datetime import datetime
from extensions import db
from werkzeug.security import generate_password_hash, check_password_hash

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150))
    email = db.Column(db.String(150), unique=True)
    password_hash = db.Column(db.String(255))
    role = db.Column(db.String(50))  # employee, dept_head, finance, secretary_general

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class WorkflowRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    description = db.Column(db.Text)
    status = db.Column(db.String(50))

    requester_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    requester = db.relationship("User", backref="requests")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Approval(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(20))  # approve / reject
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)



class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.Integer, db.ForeignKey("workflow_request.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    action = db.Column(db.String(50))
    old_status = db.Column(db.String(50))
    new_status = db.Column(db.String(50))
    note = db.Column(db.Text)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")
    request = db.relationship("WorkflowRequest")
