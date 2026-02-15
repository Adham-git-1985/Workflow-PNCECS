from pathlib import Path
import re

path = Path('/mnt/data/work_units_v2/templates/portal/admin/hr_org_structure.html')
text = path.read_text(encoding='utf-8')

# 1) Replace nav tabs: add Units tab after dirs, remove nodes tab
# Insert units tab after dirs tab
text = text.replace(
    "<li class=\"nav-item\" role=\"presentation\"><button class=\"nav-link {% if tab=='dirs' %}active{% endif %}\" data-bs-toggle=\"tab\" data-bs-target=\"#tab-dirs\" type=\"button\" onclick=\"location.href='{{ url_for('portal.portal_admin_hr_org_structure', tab='dirs') }}'\">الإدارات</button></li>\n",
    "<li class=\"nav-item\" role=\"presentation\"><button class=\"nav-link {% if tab=='dirs' %}active{% endif %}\" data-bs-toggle=\"tab\" data-bs-target=\"#tab-dirs\" type=\"button\" onclick=\"location.href='{{ url_for('portal.portal_admin_hr_org_structure', tab='dirs') }}'\">الإدارات</button></li>\n"
    "    <li class=\"nav-item\" role=\"presentation\"><button class=\"nav-link {% if tab=='units' %}active{% endif %}\" data-bs-toggle=\"tab\" data-bs-target=\"#tab-units\" type=\"button\" onclick=\"location.href='{{ url_for('portal.portal_admin_hr_org_structure', tab='units') }}'\">Units</button></li>\n"
)

# Remove nodes tab button
text = re.sub(r"\s*<li class=\"nav-item\" role=\"presentation\"><button class=\"nav-link \{% if tab=='nodes' %\}active\{% endif %\}\".*?التراكيب الجديدة</button></li>\n", "\n", text, flags=re.S)

# 2) Remove entire nodes tab content block
text = re.sub(r"\{# -------------------- ORG NODES \(NEW\) -------------------- #\}.*?\n\s*</div>\n\s*\n\s*\n\s*</div>\n\s*</div>\n\{% endblock %\}\n?", "\n\n  </div>\n</div>\n{% endblock %}\n", text, flags=re.S)

# 3) Insert Units tab content before Departments tab content marker
units_block = r'''

    {# -------------------- UNITS -------------------- #}
    <div class="tab-pane fade {% if tab=='units' %}show active{% endif %}" id="tab-units">
      <div class="d-flex justify-content-between align-items-center flex-wrap gap-2 mb-2">
        <div class="text-muted small">Units مرتبطة بالإدارة (Directorate)</div>
        <form class="d-flex gap-2" method="post" enctype="multipart/form-data">
          <input type="hidden" name="op" value="import"><input type="hidden" name="kind" value="units">
          <input class="form-control form-control-sm" type="file" name="file" accept=".xlsx" required>
          <button class="btn btn-sm btn-outline-primary" type="submit"><i class="bi bi-upload"></i> استيراد Excel</button>
        </form>
      </div>
      <div class="table-responsive">
        <table class="table table-sm align-middle">
          <thead><tr><th>الإدارة</th><th>Code</th><th>الاسم (AR)</th><th>الاسم (EN)</th><th>نشط</th><th></th></tr></thead>
          <tbody>
            {% for u in units %}
              <tr>
                <form method="post">
                  <input type="hidden" name="op" value="save"><input type="hidden" name="kind" value="units"><input type="hidden" name="id" value="{{ u.id }}">
                  <td style="min-width:240px">
                    <select class="form-select form-select-sm" name="parent_id" required>
                      <option value="">-- اختر --</option>
                      {% for di in directorates %}
                        <option value="{{ di.id }}" {% if u.directorate_id==di.id %}selected{% endif %}>{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>
                      {% endfor %}
                    </select>
                  </td>
                  <td style="width:120px"><input class="form-control form-control-sm" name="code" value="{{ u.code or '' }}"></td>
                  <td><input class="form-control form-control-sm" name="name_ar" value="{{ u.name_ar or '' }}"></td>
                  <td><input class="form-control form-control-sm" name="name_en" value="{{ u.name_en or '' }}"></td>
                  <td style="width:90px" class="text-center"><input class="form-check-input" type="checkbox" name="is_active" {% if u.is_active %}checked{% endif %}></td>
                  <td class="text-nowrap"><button class="btn btn-sm btn-primary" type="submit">حفظ</button></td>
                </form>
                <td>
                  <form method="post" onsubmit="return confirm('حذف الوحدة؟');">
                    <input type="hidden" name="op" value="delete"><input type="hidden" name="kind" value="units"><input type="hidden" name="id" value="{{ u.id }}">
                    <button class="btn btn-sm btn-outline-danger" type="submit">حذف</button>
                  </form>
                </td>
              </tr>
            {% else %}
              <tr><td colspan="7" class="text-muted">لا توجد Units.</td></tr>
            {% endfor %}
            <tr class="table-light">
              <form method="post">
                <input type="hidden" name="op" value="save"><input type="hidden" name="kind" value="units">
                <td>
                  <select class="form-select form-select-sm" name="parent_id" required>
                    <option value="">-- اختر الإدارة --</option>
                    {% for di in directorates %}<option value="{{ di.id }}">{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>{% endfor %}
                  </select>
                </td>
                <td><input class="form-control form-control-sm" name="code" placeholder="Code"></td>
                <td><input class="form-control form-control-sm" name="name_ar" placeholder="الاسم (AR)"></td>
                <td><input class="form-control form-control-sm" name="name_en" placeholder="الاسم (EN)"></td>
                <td class="text-center"><input class="form-check-input" type="checkbox" name="is_active" checked></td>
                <td colspan="2"><button class="btn btn-sm btn-success" type="submit"><i class="bi bi-plus"></i> إضافة</button></td>
              </form>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
'''

text = text.replace("    {# -------------------- DEPARTMENTS -------------------- #}", units_block + "\n    {# -------------------- DEPARTMENTS -------------------- #}")

# 4) Update departments tab to allow choosing parent_type (directorate OR unit)
# Replace 'مرتبطة بالإدارة' note
text = text.replace("<div class=\"text-muted small\">مرتبطة بالإدارة</div>", "<div class=\"text-muted small\">مرتبطة بإدارة أو Unit (يجب اختيار واحدة فقط)</div>")

# Modify departments table header: first column label
text = text.replace("<thead><tr><th>الإدارة</th><th>Code</th>", "<thead><tr><th>التبعية</th><th>Code</th>")

# Replace departments row parent select block with parent_type + two selects
text = text.replace(
    "<td style=\"min-width:240px\">\n                    <select class=\"form-select form-select-sm\" name=\"parent_id\" required>\n                      <option value=\"\">-- اختر --</option>\n                      {% for di in directorates %}\n                        <option value=\"{{ di.id }}\" {% if d.directorate_id==di.id %}selected{% endif %}>{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>\n                      {% endfor %}\n                    </select>\n                  </td>",
    "<td style=\"min-width:260px\">\n                    <div class=\"d-flex gap-2\">\n                      <select class=\"form-select form-select-sm parent-type-select\" name=\"parent_type\" data-target=\"dept\" required>\n                        <option value=\"directorate\" {% if d.directorate_id %}selected{% endif %}>إدارة</option>\n                        <option value=\"unit\" {% if d.unit_id %}selected{% endif %}>Units</option>\n                      </select>\n                      <select class=\"form-select form-select-sm parent-select parent-select-dir\" name=\"parent_id_dir\" data-target=\"dept\">\n                        <option value=\"\">-- اختر الإدارة --</option>\n                        {% for di in directorates %}\n                          <option value=\"{{ di.id }}\" {% if d.directorate_id==di.id %}selected{% endif %}>{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>\n                        {% endfor %}\n                      </select>\n                      <select class=\"form-select form-select-sm parent-select parent-select-unit\" name=\"parent_id_unit\" data-target=\"dept\">\n                        <option value=\"\">-- اختر Unit --</option>\n                        {% for u in units %}\n                          <option value=\"{{ u.id }}\" {% if d.unit_id==u.id %}selected{% endif %}>{{ u.name_ar }}{% if u.directorate %} ({{ u.directorate.name_ar }}){% endif %}</option>\n                        {% endfor %}\n                      </select>\n                    </div>\n                  </td>"
)

# Replace departments NEW row (table-light) parent select
text = text.replace(
    "<td>\n                  <select class=\"form-select form-select-sm\" name=\"parent_id\" required>\n                    <option value=\"\">-- اختر الإدارة --</option>\n                    {% for di in directorates %}<option value=\"{{ di.id }}\">{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>{% endfor %}\n                  </select>\n                </td>",
    "<td>\n                  <div class=\"d-flex gap-2\">\n                    <select class=\"form-select form-select-sm parent-type-select\" name=\"parent_type\" data-target=\"dept\" required>\n                      <option value=\"directorate\" selected>إدارة</option>\n                      <option value=\"unit\">Units</option>\n                    </select>\n                    <select class=\"form-select form-select-sm parent-select parent-select-dir\" name=\"parent_id_dir\" data-target=\"dept\">\n                      <option value=\"\">-- اختر الإدارة --</option>\n                      {% for di in directorates %}<option value=\"{{ di.id }}\">{{ di.name_ar }}{% if di.organization %} ({{ di.organization.name_ar }}){% endif %}</option>{% endfor %}\n                    </select>\n                    <select class=\"form-select form-select-sm parent-select parent-select-unit\" name=\"parent_id_unit\" data-target=\"dept\">\n                      <option value=\"\">-- اختر Unit --</option>\n                      {% for u in units %}<option value=\"{{ u.id }}\">{{ u.name_ar }}{% if u.directorate %} ({{ u.directorate.name_ar }}){% endif %}</option>{% endfor %}\n                    </select>\n                  </div>\n                </td>"
)

# 5) Update sections tab: allow parent_type among department/directorate/unit
text = text.replace("<div class=\"text-muted small\">مرتبطة بالدائرة</div>", "<div class=\"text-muted small\">مرتبطة بدائرة أو إدارة أو Unit (يجب اختيار واحدة فقط)</div>")
text = text.replace("<thead><tr><th>الدائرة</th><th>Code</th>", "<thead><tr><th>التبعية</th><th>Code</th>")

# Replace sections row parent select
text = text.replace(
    "<td style=\"min-width:240px\">\n                    <select class=\"form-select form-select-sm\" name=\"parent_id\" required>\n                      <option value=\"\">-- اختر --</option>\n                      {% for d in departments %}\n                        <option value=\"{{ d.id }}\" {% if s.department_id==d.id %}selected{% endif %}>{{ d.name_ar }}</option>\n                      {% endfor %}\n                    </select>\n                  </td>",
    "<td style=\"min-width:260px\">\n                    <div class=\"d-flex gap-2\">\n                      <select class=\"form-select form-select-sm parent-type-select\" name=\"parent_type\" data-target=\"sec\" required>\n                        <option value=\"department\" {% if s.department_id %}selected{% endif %}>دائرة</option>\n                        <option value=\"directorate\" {% if s.directorate_id %}selected{% endif %}>إدارة</option>\n                        <option value=\"unit\" {% if s.unit_id %}selected{% endif %}>Units</option>\n                      </select>\n                      <select class=\"form-select form-select-sm parent-select parent-select-dept\" name=\"parent_id_dept\" data-target=\"sec\">\n                        <option value=\"\">-- اختر الدائرة --</option>\n                        {% for d in departments %}\n                          <option value=\"{{ d.id }}\" {% if s.department_id==d.id %}selected{% endif %}>{{ d.name_ar }}</option>\n                        {% endfor %}\n                      </select>\n                      <select class=\"form-select form-select-sm parent-select parent-select-dir\" name=\"parent_id_dir\" data-target=\"sec\">\n                        <option value=\"\">-- اختر الإدارة --</option>\n                        {% for di in directorates %}\n                          <option value=\"{{ di.id }}\" {% if s.directorate_id==di.id %}selected{% endif %}>{{ di.name_ar }}</option>\n                        {% endfor %}\n                      </select>\n                      <select class=\"form-select form-select-sm parent-select parent-select-unit\" name=\"parent_id_unit\" data-target=\"sec\">\n                        <option value=\"\">-- اختر Unit --</option>\n                        {% for u in units %}\n                          <option value=\"{{ u.id }}\" {% if s.unit_id==u.id %}selected{% endif %}>{{ u.name_ar }}{% if u.directorate %} ({{ u.directorate.name_ar }}){% endif %}</option>\n                        {% endfor %}\n                      </select>\n                    </div>\n                  </td>"
)

# Replace sections NEW row parent select
text = text.replace(
    "<td>\n                  <select class=\"form-select form-select-sm\" name=\"parent_id\" required>\n                    <option value=\"\">-- اختر الدائرة --</option>\n                    {% for d in departments %}<option value=\"{{ d.id }}\">{{ d.name_ar }}</option>{% endfor %}\n                  </select>\n                </td>",
    "<td>\n                  <div class=\"d-flex gap-2\">\n                    <select class=\"form-select form-select-sm parent-type-select\" name=\"parent_type\" data-target=\"sec\" required>\n                      <option value=\"department\" selected>دائرة</option>\n                      <option value=\"directorate\">إدارة</option>\n                      <option value=\"unit\">Units</option>\n                    </select>\n                    <select class=\"form-select form-select-sm parent-select parent-select-dept\" name=\"parent_id_dept\" data-target=\"sec\">\n                      <option value=\"\">-- اختر الدائرة --</option>\n                      {% for d in departments %}<option value=\"{{ d.id }}\">{{ d.name_ar }}</option>{% endfor %}\n                    </select>\n                    <select class=\"form-select form-select-sm parent-select parent-select-dir\" name=\"parent_id_dir\" data-target=\"sec\">\n                      <option value=\"\">-- اختر الإدارة --</option>\n                      {% for di in directorates %}<option value=\"{{ di.id }}\">{{ di.name_ar }}</option>{% endfor %}\n                    </select>\n                    <select class=\"form-select form-select-sm parent-select parent-select-unit\" name=\"parent_id_unit\" data-target=\"sec\">\n                      <option value=\"\">-- اختر Unit --</option>\n                      {% for u in units %}<option value=\"{{ u.id }}\">{{ u.name_ar }}{% if u.directorate %} ({{ u.directorate.name_ar }}){% endif %}</option>{% endfor %}\n                    </select>\n                  </div>\n                </td>"
)

# 6) Add small JS to toggle parent selects
if 'parent-type-select' not in text:
    pass

# Insert JS before endblock
js = r"""
<script>
(function(){
  function toggleRow(container, target){
    var typeSel = container.querySelector('select.parent-type-select[data-target="'+target+'"]');
    if(!typeSel) return;
    function apply(){
      var t = (typeSel.value||'').toLowerCase();
      container.querySelectorAll('select.parent-select').forEach(function(s){
        if(s.getAttribute('data-target') !== target) return;
        s.style.display = 'none';
      });
      // dept target
      if(target==='dept'){
        var selDir = container.querySelector('select.parent-select-dir[name$="_dir"][data-target="dept"]');
        var selUnit = container.querySelector('select.parent-select-unit[name$="_unit"][data-target="dept"]');
        if(selDir) selDir.style.display = (t==='directorate' ? '' : 'none');
        if(selUnit) selUnit.style.display = (t==='unit' ? '' : 'none');
      }
      if(target==='sec'){
        var selDept = container.querySelector('select.parent-select-dept[name$="_dept"][data-target="sec"]');
        var selDir2 = container.querySelector('select.parent-select-dir[name$="_dir"][data-target="sec"]');
        var selUnit2 = container.querySelector('select.parent-select-unit[name$="_unit"][data-target="sec"]');
        if(selDept) selDept.style.display = (t==='department' ? '' : 'none');
        if(selDir2) selDir2.style.display = (t==='directorate' ? '' : 'none');
        if(selUnit2) selUnit2.style.display = (t==='unit' ? '' : 'none');
      }
    }
    typeSel.addEventListener('change', apply);
    apply();
  }

  document.querySelectorAll('#tab-depts tbody tr, #tab-secs tbody tr').forEach(function(tr){
    toggleRow(tr, 'dept');
    toggleRow(tr, 'sec');
  });
})();
</script>
"""

text = text.replace('{% endblock %}', js + '\n{% endblock %}')

path.write_text(text, encoding='utf-8')
print('portal/admin/hr_org_structure.html patched: removed nodes, added units, updated parent selection')
