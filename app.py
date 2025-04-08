
# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from collections import Counter
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver.v2 as uc
from selenium.webdriver.chrome.options import Options

# --- Config ---
UPLOAD_FOLDER = 'instance/uploads'
RESULTS_FOLDER = 'instance/results'
DEBUG_FOLDER = RESULTS_FOLDER
ALLOWED_EXTENSIONS = {'.xlsx'}
TIMEOUT = 20

# Ensure folders exist
for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- Flask App ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RESULTS_FOLDER'] = RESULTS_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB

logging.basicConfig(
    filename='instance/app.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def fetch_page_text(url):
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument("user-agent=Mozilla/5.0")
        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(TIMEOUT)
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        html = driver.page_source
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).lower()

        debug_file = os.path.join(DEBUG_FOLDER, f'debug_{uuid.uuid4().hex}.txt')
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write(text)
        logging.info(f"[DEBUG] Saved debug content to {debug_file}")
        return text
    except Exception as e:
        logging.error(f"[Selenium] Error fetching {url}: {e}")
        return ""
    finally:
        try:
            driver.quit()
        except:
            pass

def get_keywords_from_file(filepath):
    df = pd.read_excel(filepath)
    keywords = df.iloc[:, 0].dropna().astype(str).str.strip().str.lower().tolist()
    return list(set(keywords))

def filter_stop_words(keywords, text):
    words = re.findall(r'\b\w{3,}\b', text)
    common = {w for w, _ in Counter(words).most_common(50)}
    return [kw for kw in keywords if not any(w in common for w in kw.split())]

def check_keywords(keywords, text, url):
    results = []
    for kw in keywords:
        score = text.count(kw)
        found = score > 0
        preview = ""
        if found:
            idx = text.find(kw)
            preview = text[max(0, idx - 50):idx + 50]
        results.append({
            'keyword': kw,
            'found': found,
            'url': url if found else "-",
            'score': score,
            'preview': preview
        })
    return results

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    results, error, download_filename = [], None, None
    email = request.form.get('email', '').strip()
    website = request.form.get('website', '').strip()

    if request.method == 'POST':
        file = request.files.get('file')
        if not email or not website or not file or not allowed_file(file.filename):
            error = "اطلاعات ناقص یا فایل نامعتبر است."
        else:
            try:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                keywords = get_keywords_from_file(filepath)

                text = fetch_page_text(website)
                if not text:
                    raise ValueError("محتوایی برای سایت دریافت نشد.")

                filtered_keywords = filter_stop_words(keywords, text)
                results = check_keywords(filtered_keywords, text, website)

                df = pd.DataFrame(results)
                output_name = f"results_{uuid.uuid4()}.xlsx"
                output_path = os.path.join(RESULTS_FOLDER, output_name)
                df.to_excel(output_path, index=False)
                download_filename = output_name
            except Exception as e:
                logging.error(f"Processing failed: {e}")
                error = "خطا در پردازش اطلاعات."

    return render_template("index.html", results=results, error=error, download_filename=download_filename, email=email, website=website)

@app.route('/download/<filename>')
def download(filename):
    safe_name = secure_filename(filename)
    path = os.path.join(RESULTS_FOLDER, safe_name)
    if not os.path.exists(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)

# --- Main ---
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
