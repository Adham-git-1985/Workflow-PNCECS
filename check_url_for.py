import os
import re
from flask import Flask
from werkzeug.routing import BuildError

# =========================
# إعدادات
# =========================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")

URL_FOR_PATTERN = re.compile(
    r"url_for\(\s*['\"]([^'\"]+)['\"]"
)

# =========================
# تحميل Flask app
# =========================
from app import app  # ⚠️ يجب أن يكون app = Flask(__name__)

# =========================
# استخراج كل endpoints المسجلة
# =========================
def get_registered_endpoints(flask_app: Flask):
    endpoints = set()
    for rule in flask_app.url_map.iter_rules():
        endpoints.add(rule.endpoint)
    return endpoints


# =========================
# فحص ملفات HTML
# =========================
def scan_templates():
    results = []

    registered_endpoints = get_registered_endpoints(app)

    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if not file.endswith(".html"):
                continue

            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, TEMPLATES_DIR)

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            matches = URL_FOR_PATTERN.findall(content)

            for endpoint in matches:
                if endpoint not in registered_endpoints:
                    results.append({
                        "file": rel_path,
                        "endpoint": endpoint
                    })

    return results


# =========================
# تشغيل الفحص
# =========================
if __name__ == "__main__":
    print("\n🔍 Checking url_for() usage...\n")

    errors = scan_templates()

    if not errors:
        print("✅ All url_for() calls are valid.")
    else:
        print("❌ Invalid url_for() calls found:\n")
        for e in errors:
            print(f"  📄 {e['file']}")
            print(f"     ➜ url_for('{e['endpoint']}') ❌ NOT FOUND\n")

        print("🔧 Tip:")
        print(" - Check if the endpoint belongs to a Blueprint")
        print(" - Use: blueprint_name.endpoint_name\n")
