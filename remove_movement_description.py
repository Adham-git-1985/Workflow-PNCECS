from pathlib import Path

TARGETS = [
    "",
    "",
    "",
    "",
]

EXTS = {".html", ".jinja", ".jinja2", ".py", ".js"}
ROOT = Path.cwd()
changed = []

for path in ROOT.rglob("*"):
    if not path.is_file() or path.suffix.lower() not in EXTS:
        continue
    if any(part in {".git", ".venv", "venv", "__pycache__", "node_modules"} for part in path.parts):
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue

    new_text = text
    for target in TARGETS:
        new_text = new_text.replace(target, "")

    # Clean empty muted paragraph/card description lines that may remain after removal.
    new_text = new_text.replace("<p class=\"text-muted\"></p>", "")
    new_text = new_text.replace("<p class=\"text-muted small\"></p>", "")
    new_text = new_text.replace("<small class=\"text-muted\"></small>", "")
    new_text = new_text.replace("<div class=\"text-muted\"></div>", "")

    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        changed.append(str(path.relative_to(ROOT)))

if changed:
    print("تم حذف الوصف من الملفات التالية:")
    for item in changed:
        print("-", item)
else:
    print("لم أجد العبارة المطلوبة. تأكد أنك تشغل السكربت من جذر المشروع الصحيح.")
