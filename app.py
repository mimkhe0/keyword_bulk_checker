# -*- coding: utf-8 -*-
import os
import re
import uuid
import time
import logging
import shutil
import pandas as pd
from flask import Flask, request, render_template, send_file, abort
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc

# --- Config ---
UPLOAD_FOLDER = 'instance/uploads'
RESULTS_FOLDER = 'instance/results'
DEBUG_FOLDER = RESULTS_FOLDER
ALLOWED_EXTENSIONS = {'.xlsx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
TIMEOUT = 30

# --- App setup ---
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RESULTS_FOLDER'] = RESULTS_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RESULTS_FOLDER, exist_ok=True)

logging.basicConfig(
    filename='instance/app.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- Helpers ---
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
        # Debug: save raw content
        filename = os.path.join(DEBUG_FOLDER, f'debug_{uuid.uuid4().hex}.txt')
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(text)
        return text
    except Exception as e:
        logging.error(f"[Selenium] Error fetching {url}: {e}")
        return ""
    finally:
        try:
            driver.quit()
        except:
            pass

def check_keywords(text, keywords):
    results = []
    for kw in keywords:
        base = {'keyword': kw, 'found': False, 'score': 0, 'preview': '', 'url': '-'}
        if not text:
            results.append(base)
            continue
        kw_parts = [w for w in re.split(r'\s+', kw.lower()) if len(w) > 2]
        found_word = next((w for w in kw_parts if w in text), None)
        if found_word:
            idx = text.find(found_word)
            preview = text[max(0, idx - 60): idx + len(found_word) + 60]
            base.update({'found': True, 'score': text.count(found_word), 'preview': f"...{preview}..."})
        results.append(base)
    return results

# --- Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    results = []
    download_filename = None
    error = None
    if request.method == 'POST':
        file = request.files.get('file')
        url = request.form.get('website', '').strip()
        if not url.startswith("http"):
            url = "https://" + url
        if not file or not allowed_file(file.filename):
            error = "فایل نامعتبر است."
        else:
            try:
                filename = secure_filename(file.filename)
                path = os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4().hex}_{filename}")
                file.save(path)
                df = pd.read_excel(path)
                os.remove(path)
                keywords = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
                page_text = fetch_page_text(url)
                results = check_keywords(page_text, keywords)
                df_out = pd.DataFrame(results)
                output_file = f"results_{uuid.uuid4()}.xlsx"
                output_path = os.path.join(RESULTS_FOLDER, output_file)
                df_out.to_excel(output_path, index=False)
                download_filename = output_file
            except Exception as e:
                logging.error(f"Processing error: {e}")
                error = "خطا در پردازش فایل یا محتوا."
    return render_template("index.html", results=results, download_filename=download_filename, error=error)

@app.route('/download/<filename>')
def download(filename):
    filename = secure_filename(filename)
    file_path = os.path.join(RESULTS_FOLDER, filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, as_attachment=True)

# --- Run ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
