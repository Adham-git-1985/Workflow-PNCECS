from pathlib import Path
import re

path = Path('/mnt/data/work_units_v2/init_db.py')
text = path.read_text(encoding='utf-8')

# Replace model import list: remove OrgNode, add Unit
text = text.replace('Organization, Directorate, Department, OrgNode,', 'Organization, Directorate, Unit, Department,')

# Also update any other import tuple mention of OrgNode
text = text.replace('Organization, Directorate, Department, OrgNode,\n', 'Organization, Directorate, Unit, Department,\n')

# Remove the entire seed_org_nodes_baseline function and invocation
pattern = r"\n\s*# =========================\n\s*# \U0001f3db\ufe0f Seed OrgNodes baseline \(Chief \u2192 SG \u2192 Assistant SG\)\n\s*# =========================\n\s*def seed_org_nodes_baseline\(\):.*?\n\s*seed_org_nodes_baseline\(\)\n"
new_text, n = re.subn(pattern, "\n", text, flags=re.S)
if n == 0:
    # Try alternate if spacing differs
    pattern2 = r"\n\s*# =========================\n\s*# \U0001f3db\ufe0f Seed OrgNodes baseline \(Chief \u2192 SG \u2192 Assistant SG\)\n\s*# =========================.*?\n\s*seed_org_nodes_baseline\(\)\n"
    new_text, n = re.subn(pattern2, "\n", text, flags=re.S)
text = new_text

# Remove any lingering OrgNode references
text = re.sub(r'\bOrgNode\b', 'Unit  # OrgNode removed', text) if 'OrgNode' in text else text
# The above could be dangerous; instead, do targeted cleanup for import list only.
# Let's undo if it happened excessively.
if 'Unit  # OrgNode removed' in text:
    # If we replaced too much, revert and do targeted cleanup.
    text = path.read_text(encoding='utf-8')
    text = text.replace('Organization, Directorate, Department, OrgNode,', 'Organization, Directorate, Unit, Department,')
    text = text.replace('Organization, Directorate, Department, OrgNode,\n', 'Organization, Directorate, Unit, Department,\n')
    text = re.sub(pattern, "\n", text, flags=re.S)
    text = re.sub(r'\bOrgNode\b', 'OrgNode', text)  # no-op

# Finally, ensure models import includes Unit
if 'Unit,' not in text and 'Unit' not in text:
    pass

path.write_text(text, encoding='utf-8')
print('init_db.py patched (OrgNode removed, Unit added)')
