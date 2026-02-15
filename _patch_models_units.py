import re
from pathlib import Path

path = Path('/mnt/data/work_units_v2/models.py')
text = path.read_text(encoding='utf-8')

# Remove OrgNode section entirely
pattern = r"\n# ======================\n# Unified Org Tree \(OrgNode\)\n# ======================\nclass OrgNode\(db\.Model\):.*?\n\s*class Directorate\(db\.Model\):"

m = re.search(pattern, text, flags=re.S)
if not m:
    raise SystemExit('OrgNode block not found; file structure changed')

# Keep the 'class Directorate' header (already matched at end)
start, end = m.span()
replacement = "\nclass Directorate(db.Model):"
text = text[:start] + replacement + text[end:]

# Insert Unit model after Directorate model definition
# Find end of Directorate class (blank line before next class Department)
# We will match 'organization = db.relationship...' then a blank line then 'class Department'
pat_dir_end = r"(class Directorate\(db\.Model\):.*?\n\s*organization\s*=\s*db\.relationship\(\"Organization\".*?\n)\nclass Department\(db\.Model\):"
md = re.search(pat_dir_end, text, flags=re.S)
if not md:
    raise SystemExit('Directorate block not found for insertion')

dir_block = md.group(1)

unit_class = """

class Unit(db.Model):
    __tablename__ = \"units\"

    id = db.Column(db.Integer, primary_key=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey(\"directorates.id\"), nullable=False, index=True)

    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    directorate = db.relationship(\"Directorate\", backref=db.backref(\"units\", lazy=\"selectin\"))

    def __repr__(self) -> str:
        return f\"<Unit {self.name_ar}>\"

    @property
    def label(self) -> str:
        return (self.name_ar or self.name_en or self.code or '').strip() or str(self.id)
"""

# Replace the matched area inserting unit_class between Directorate and Department
text = text[:md.start(1)] + dir_block + unit_class + "\n\nclass Department(db.Model):" + text[md.end():]

# Modify Department model: add unit_id, make directorate_id nullable, add XOR constraint, add relationships
# We'll replace the Department class definition block up to the line right before '# ======================' of RequestType.
pat_dept = r"class Department\(db\.Model\):\n    __tablename__ = \"departments\"\n    id = db\.Column\(db\.Integer, primary_key=True\)\n    directorate_id = db\.Column\(db\.Integer, db\.ForeignKey\(\"directorates\.id\"\), nullable=False\)\n    name_ar = db\.Column\(db\.String\(200\), nullable=False\)\n    name_en = db\.Column\(db\.String\(200\), nullable=True\)\n    code = db\.Column\(db\.String\(50\), nullable=True\)\n    is_active = db\.Column\(db\.Boolean, default=True, nullable=False\)\n    created_at = db\.Column\(db\.DateTime, default=datetime\.utcnow, nullable=False\)\n\n    directorate = db\.relationship\(\"Directorate\", backref=db\.backref\(\"departments\", lazy=\"selectin\"\)\)\n"

mdept = re.search(pat_dept, text)
if not mdept:
    raise SystemExit('Department block not found for rewrite')

new_dept = """class Department(db.Model):
    __tablename__ = \"departments\"
    __table_args__ = (
        db.CheckConstraint(
            "(directorate_id IS NOT NULL AND unit_id IS NULL) OR (directorate_id IS NULL AND unit_id IS NOT NULL)",
            name="ck_departments_parent_xor",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Department can belong either to a Directorate OR to a Unit (must choose one)
    directorate_id = db.Column(db.Integer, db.ForeignKey(\"directorates.id\"), nullable=True, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey(\"units.id\"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), nullable=True)
    code = db.Column(db.String(50), nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    directorate = db.relationship(\"Directorate\", backref=db.backref(\"departments\", lazy=\"selectin\"))
    unit = db.relationship(\"Unit\", backref=db.backref(\"departments\", lazy=\"selectin\"))

    @property
    def effective_directorate_id(self):
        # Return directorate_id even if department is attached to a Unit.
        if self.directorate_id:
            return self.directorate_id
        try:
            return self.unit.directorate_id if self.unit else None
        except Exception:
            return None

    def __repr__(self) -> str:
        return f\"<Department {self.name_ar}>\"\n"""

text = text[:mdept.start()] + new_dept + text[mdept.end():]

# Modify Section model to support unit_id and XOR among department/directorate/unit
pat_sec = r"class Section\(db\.Model\):\n    __tablename__ = \"sections\"\n    __table_args__ = \(\n        db\.CheckConstraint\(\n            \"department_id IS NOT NULL OR directorate_id IS NOT NULL\",\n            name=\"ck_sections_parent\",\n        \),\n    \)\n\n    id = db\.Column\(db\.Integer, primary_key=True\)\n\n    # Section can belong either to a Department \(دائرة\) OR directly to a Directorate \(إدارة\)\n    department_id = db\.Column\(db\.Integer, db\.ForeignKey\(\"departments\.id\"\), nullable=True, index=True\)\n    directorate_id = db\.Column\(db\.Integer, db\.ForeignKey\(\"directorates\.id\"\), nullable=True, index=True\)\n\n    name_ar = db\.Column\(db\.String\(200\), nullable=False, index=True\)\n    name_en = db\.Column\(db\.String\(200\), nullable=True, index=True\)\n    code = db\.Column\(db\.String\(50\), nullable=True, index=True\)\n    is_active = db\.Column\(db\.Boolean, default=True\)\n    created_at = db\.Column\(db\.DateTime, default=datetime\.utcnow\)\n\n    department = db\.relationship\(\"Department\", backref=db\.backref\(\"sections\", lazy=\"dynamic\"\)\)\n    directorate = db\.relationship\(\"Directorate\", backref=db\.backref\(\"sections_direct\", lazy=\"dynamic\"\)\)\n"

msec = re.search(pat_sec, text)
if not msec:
    raise SystemExit('Section header block not found for rewrite')

new_sec_header = """class Section(db.Model):
    __tablename__ = \"sections\"
    __table_args__ = (
        db.CheckConstraint(
            "(department_id IS NOT NULL AND directorate_id IS NULL AND unit_id IS NULL) OR "
            "(department_id IS NULL AND directorate_id IS NOT NULL AND unit_id IS NULL) OR "
            "(department_id IS NULL AND directorate_id IS NULL AND unit_id IS NOT NULL)",
            name="ck_sections_parent_xor",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)

    # Section can belong either to a Department OR directly to a Directorate OR directly to a Unit (must choose one)
    department_id = db.Column(db.Integer, db.ForeignKey(\"departments.id\"), nullable=True, index=True)
    directorate_id = db.Column(db.Integer, db.ForeignKey(\"directorates.id\"), nullable=True, index=True)
    unit_id = db.Column(db.Integer, db.ForeignKey(\"units.id\"), nullable=True, index=True)

    name_ar = db.Column(db.String(200), nullable=False, index=True)
    name_en = db.Column(db.String(200), nullable=True, index=True)
    code = db.Column(db.String(50), nullable=True, index=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    department = db.relationship(\"Department\", backref=db.backref(\"sections\", lazy=\"dynamic\"))
    directorate = db.relationship(\"Directorate\", backref=db.backref(\"sections_direct\", lazy=\"dynamic\"))
    unit = db.relationship(\"Unit\", backref=db.backref(\"sections_direct\", lazy=\"dynamic\"))
"""

text = text[:msec.start()] + new_sec_header + text[msec.end():]

# Patch Section.to_dict parent resolution: include unit and reflect XOR
# We'll replace the to_dict method body by regex.
pat_to_dict = r"def to_dict\(self\):\n        parent_name = None\n        parent_type = None\n        if self\.department_id and self\.department:\n            parent_type = \"DEPARTMENT\"\n            parent_name = self\.department\.name_ar\n        elif self\.directorate_id and self\.directorate:\n            parent_type = \"DIRECTORATE\"\n            parent_name = self\.directorate\.name_ar\n\n        return \{\n            \"id\": self\.id,\n            \"department_id\": self\.department_id,\n            \"directorate_id\": self\.directorate_id,"

mt = re.search(pat_to_dict, text)
if not mt:
    raise SystemExit('Section.to_dict signature not found; adjust manually')

# We will do a smaller targeted replace: the parent resolution block only.
old_parent_block = """        parent_name = None
        parent_type = None
        if self.department_id and self.department:
            parent_type = \"DEPARTMENT\"
            parent_name = self.department.name_ar
        elif self.directorate_id and self.directorate:
            parent_type = \"DIRECTORATE\"
            parent_name = self.directorate.name_ar
"""

new_parent_block = """        parent_name = None
        parent_type = None
        if self.department_id and self.department:
            parent_type = \"DEPARTMENT\"
            parent_name = self.department.name_ar
        elif self.unit_id and self.unit:
            parent_type = \"UNIT\"
            parent_name = self.unit.name_ar
        elif self.directorate_id and self.directorate:
            parent_type = \"DIRECTORATE\"
            parent_name = self.directorate.name_ar
"""

if old_parent_block not in text:
    raise SystemExit('Old Section parent block not found; abort')
text = text.replace(old_parent_block, new_parent_block)

# Add unit_id in Section.to_dict returned dict: insert after directorate_id
text = text.replace('            "directorate_id": self.directorate_id,\n', '            "directorate_id": self.directorate_id,\n            "unit_id": self.unit_id,\n')

path.write_text(text, encoding='utf-8')
print('models.py patched: removed OrgNode, added Unit, updated Department/Section')
