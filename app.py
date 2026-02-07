import os
import re
import base64
import sys
import json
import warnings
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, flash, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
from apscheduler.schedulers.background import BackgroundScheduler
from imap_tools import MailBox, A
import apprise
import google.generativeai as genai

# Warnung von Google unterdrücken, damit die Logs lesbar bleiben
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

# --- Config ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
UPLOAD_FOLDER = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'bills.db')
ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-fallback')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB Limit

db = SQLAlchemy(app)
apobj = apprise.Apprise()

# --- Auth Setup ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

oauth = OAuth(app)
google = oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

# Environment Vars
IMAP_SERVER = os.environ.get('IMAP_SERVER')
IMAP_USER = os.environ.get('IMAP_USER')
IMAP_PASSWORD = os.environ.get('IMAP_PW')
IMAP_OWNER_EMAIL = os.environ.get('IMAP_OWNER_EMAIL')
NOTIFY_URL = os.environ.get('NOTIFY_URL')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

if NOTIFY_URL:
    apobj.add(NOTIFY_URL)

# --- DB Models ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=True)
    name = db.Column(db.String(100))
    bills = db.relationship('Bill', backref='owner', lazy=True, cascade="all, delete-orphan")
    notify_url = db.Column(db.String(255), nullable=True)
    imap_server = db.Column(db.String(150), nullable=True)
    imap_user = db.Column(db.String(150), nullable=True)
    imap_password = db.Column(db.String(150), nullable=True)

class Bill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    filename = db.Column(db.String(150), nullable=False, unique=True)
    amount = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(20), default='offen') 
    due_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    paid_at = db.Column(db.DateTime, nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    files = db.relationship('BillFile', backref='bill', lazy=True, cascade="all, delete-orphan")

    @property
    def days_left(self):
        if not self.due_date: return None
        return (self.due_date - datetime.now().date()).days
    
    @property
    def file_type(self):
        if '.' not in self.filename: return 'pdf'
        ext = self.filename.rsplit('.', 1)[1].lower()
        return 'image' if ext in ['jpg', 'jpeg', 'png'] else 'pdf'

class BillFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('bill.id'), nullable=False)
    filename = db.Column(db.String(150), nullable=False)

    @property
    def file_type(self):
        if '.' not in self.filename: return 'pdf'
        ext = self.filename.rsplit('.', 1)[1].lower()
        return 'image' if ext in ['jpg', 'jpeg', 'png'] else 'pdf'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Helper ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def send_notification(title, body, user=None):
    urls = []
    # 1. User Config
    if user and user.notify_url:
        urls.append(user.notify_url)
    
    # 2. Global Fallback (nur wenn User nichts hat oder als Admin-Kanal)
    if not urls and NOTIFY_URL:
        urls.append(NOTIFY_URL)

    if not urls:
        return

    for url in urls:
        try:
            ap_inst = apprise.Apprise()
            ap_inst.add(url)
            ap_inst.notify(body=body, title=title)
        except Exception as e:
            print(f"Notification Error ({url}): {e}")

def analyze_bill_ai(filepath):
    """Versucht mittels AI Titel, Datum und Betrag aus der Datei zu lesen."""
    if not GEMINI_API_KEY:
        print("AI Info: GEMINI_API_KEY not set.", flush=True)
        return None
    
    print(f"AI Info: Starting analysis for {os.path.basename(filepath)}", flush=True)
    
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('models/gemini-2.0-flash')
        
        ext = filepath.rsplit('.', 1)[1].lower()
        prompt_text = """Analysiere diese Rechnung und extrahiere folgende Daten:
1. Titel (Name des Unternehmens/Rechnungsstellers)
2. Fälligkeitsdatum (Format YYYY-MM-DD). WICHTIG: Unterscheide strikt zwischen Rechnungsdatum (Invoice Date) und Fälligkeitsdatum (Due Date). Suche nach "Zahlbar bis", "Fällig am", "Due date". Nimm NICHT das Rechnungsdatum, es sei denn, es ist identisch mit dem Fälligkeitsdatum.
3. Rechnungsbetrag (als Fliesskommazahl, z.B. 120.50)

Antworte ausschliesslich mit einem validen JSON-Objekt in diesem Format:
{"title": "string oder null", "date": "YYYY-MM-DD oder null", "amount": 0.00 oder null}"""
        content = []
        
        # MIME-Type bestimmen
        mime_type = None
        if ext in ['jpg', 'jpeg']:
            mime_type = "image/jpeg"
        elif ext == 'png':
            mime_type = "image/png"
        elif ext == 'pdf':
            mime_type = "application/pdf"

        if mime_type:
            print(f"AI Info: Analyzing as {mime_type} (Direct).", flush=True)
            with open(filepath, "rb") as f:
                file_data = f.read()
            content = [
                prompt_text,
                {"mime_type": mime_type, "data": file_data}
            ]
        else:
            print(f"AI Info: Unsupported file type: {ext}", flush=True)
            return None

        print("AI Info: Sending request to Gemini...", flush=True)
        response = model.generate_content(content)
        ans = response.text.strip()
        
        # Markdown Code-Blöcke entfernen falls vorhanden
        if ans.startswith('```json'):
            ans = ans[7:]
        if ans.endswith('```'):
            ans = ans[:-3]
            
        try:
            data = json.loads(ans.strip())
            result = {}
            
            if data.get('date'):
                try:
                    result['date'] = datetime.strptime(data['date'], '%Y-%m-%d').date()
                except ValueError: pass
            
            result['title'] = data.get('title')
            result['amount'] = data.get('amount')
            
            print(f"AI Info: Extracted -> {result}", flush=True)
            return result
        except json.JSONDecodeError:
            print(f"AI Info: Failed to parse JSON response: {ans}", flush=True)
            
    except Exception as e:
        print(f"AI Analysis Error: {e}", flush=True)
        try:
            print("--- DEBUG: Verfügbare Modelle für diesen Key ---", flush=True)
            for m in genai.list_models():
                if 'generateContent' in m.supported_generation_methods:
                    print(f" - {m.name}", flush=True)
        except Exception as e2:
            print(f"Debug Error: {e2}", flush=True)
    return None

# --- Background Logic ---
def process_mailbox(server, user, password, owner):
    """Hilfsfunktion zum Abrufen eines Postfachs"""
    try:
        with MailBox(server).login(user, password, initial_folder='INBOX') as mailbox:
            for msg in mailbox.fetch(A(seen=False)):
                if msg.attachments:
                    found_file = False
                    for att in msg.attachments:
                        if att.filename and att.filename.lower().endswith('.pdf'):
                            secure_name = f"{int(datetime.now().timestamp())}_{att.filename}"
                            save_path = os.path.join(UPLOAD_FOLDER, secure_name)
                            with open(save_path, 'wb') as f:
                                f.write(att.payload)
                            
                            default_due = datetime.now().date() + timedelta(days=30)
                            
                            # AI Analyse versuchen
                            ai_data = analyze_bill_ai(save_path)
                            
                            due_date = default_due
                            title = msg.subject[:100]
                            amount = None

                            if ai_data:
                                if ai_data.get('date'): due_date = ai_data['date']
                                if ai_data.get('title'): title = ai_data['title']
                                if ai_data.get('amount'): amount = ai_data['amount']
                            
                            new_bill = Bill(
                                title=title,
                                filename=secure_name,
                                due_date=due_date,
                                amount=amount,
                                user_id=owner.id
                            )
                            db.session.add(new_bill)
                            try:
                                db.session.commit()
                                found_file = True
                            except Exception:
                                db.session.rollback()
                    
                    if found_file:
                        send_notification("Neue Rechnung", f"Importiert: {msg.subject}", user=owner)
    except Exception as e:
        print(f"IMAP Error for {user}: {e}")

def fetch_emails():
    """Holt Mails für alle konfigurierten User + Global"""
    with app.app_context():
        # 1. Globale Konfiguration (Legacy / Docker Env)
        if all([IMAP_SERVER, IMAP_PASSWORD, IMAP_USER, IMAP_OWNER_EMAIL]):
            owner = User.query.filter_by(email=IMAP_OWNER_EMAIL).first()
            if owner:
                process_mailbox(IMAP_SERVER, IMAP_USER, IMAP_PASSWORD, owner)
        
        # 2. Benutzer-spezifische Konfiguration
        users = User.query.filter(User.imap_server != None).all()
        for u in users:
            if u.imap_server and u.imap_user and u.imap_password:
                process_mailbox(u.imap_server, u.imap_user, u.imap_password, u)

def check_due_dates():
    """Täglicher Reminder"""
    with app.app_context():
        bills = Bill.query.filter_by(status='offen').all()
        today = datetime.now().date()
        for bill in bills:
            if not bill.due_date: continue
            days = (bill.due_date - today).days
            
            if days <= 0:
                send_notification("⚠️ ÜBERFÄLLIG", f"{bill.title} war fällig am {bill.due_date}!", user=bill.owner)
            elif days == 3:
                send_notification("Erinnerung", f"{bill.title} ist in 3 Tagen fällig.", user=bill.owner)

# Scheduler setup
scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(fetch_emails, 'interval', minutes=2)
scheduler.add_job(check_due_dates, 'cron', hour=8, minute=0)
scheduler.start()

# --- Routes ---

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').lower()
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and user.password_hash and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            return redirect(url_for('index'))
        else:
            flash('Login fehlgeschlagen. Daten prüfen.', 'error')
            
    return render_template('login.html', register_mode=False)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        email = request.form.get('email', '').lower()
        password = request.form.get('password')
        
        if User.query.filter_by(email=email).first():
            flash('Email existiert bereits.', 'error')
        else:
            new_user = User(email=email, password_hash=generate_password_hash(password, method='pbkdf2:sha256'))
            db.session.add(new_user)
            db.session.commit()
            login_user(new_user)
            return redirect(url_for('index'))
    return render_template('login.html', register_mode=True)

@app.route('/google-login')
def google_login():
    if not os.environ.get('GOOGLE_CLIENT_ID'):
        flash("Google Login nicht konfiguriert.", "error")
        return redirect(url_for('login'))
    redirect_uri = url_for('google_auth', _external=True)
    return google.authorize_redirect(redirect_uri)

@app.route('/auth/callback')
def google_auth():
    try:
        token = google.authorize_access_token()
        user_info = token.get('userinfo')
        if not user_info:
            raise Exception("No User Info")
            
        email = user_info['email'].lower()
        name = user_info.get('name', 'Google User')
        
        user = User.query.filter_by(email=email).first()
        if not user:
            user = User(email=email, name=name)
            db.session.add(user)
            db.session.commit()
        
        login_user(user, remember=True)
        return redirect(url_for('index'))
        
    except Exception as e:
        print(f"OAuth Error: {e}")
        flash(f"Fehler: {e}", "error")
        return redirect(url_for('login'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    q = request.args.get('q')
    
    query_open = Bill.query.filter_by(status='offen', user_id=current_user.id)
    query_paid = Bill.query.filter_by(status='bezahlt', user_id=current_user.id)
    
    if q:
        # Einfache Suche im Titel
        query_open = query_open.filter(Bill.title.contains(q))
        query_paid = query_paid.filter(Bill.title.contains(q))
        
    bills_open = query_open.order_by(Bill.due_date.asc().nulls_last()).all()
    
    # Bei Suche zeigen wir alle Ergebnisse, sonst nur die letzten 20
    if q:
        bills_paid = query_paid.order_by(Bill.paid_at.desc()).all()
    else:
        bills_paid = query_paid.order_by(Bill.paid_at.desc()).limit(20).all()
        
    total_open = sum(b.amount for b in bills_open if b.amount)
    return render_template('index.html', bills_open=bills_open, bills_paid=bills_paid, user=current_user, total_open=total_open, search_query=q)

@app.route('/stats')
@login_required
def stats():
    # Nur bezahlte Rechnungen mit Betrag holen
    bills = Bill.query.filter(Bill.status == 'bezahlt', Bill.user_id == current_user.id, Bill.amount != None).order_by(Bill.paid_at.asc()).all()
    
    data_map = {}
    for b in bills:
        if b.paid_at:
            month = b.paid_at.strftime('%Y-%m')
            data_map[month] = data_map.get(month, 0) + b.amount
            
    sorted_keys = sorted(data_map.keys())
    labels = sorted_keys
    values = [data_map[k] for k in sorted_keys]
    
    return render_template('stats.html', labels=labels, values=values)

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        current_user.notify_url = request.form.get('notify_url')
        current_user.imap_server = request.form.get('imap_server')
        current_user.imap_user = request.form.get('imap_user')
        
        pw = request.form.get('imap_password')
        if pw and pw.strip(): # Nur updaten wenn nicht leer
            current_user.imap_password = pw
            
        db.session.commit()
        flash('Einstellungen gespeichert.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html', user=current_user)

@app.route('/test_notification')
@login_required
def test_notification():
    if current_user.notify_url:
        send_notification(
            "Test-Benachrichtigung", 
            "Wenn du das siehst, funktioniert die Benachrichtigung von BillTracker! 🎉", 
            user=current_user
        )
        flash('Test-Benachrichtigung wurde an deine URL gesendet.', 'success')
    else:
        flash('Keine Benachrichtigungs-URL in den Einstellungen hinterlegt.', 'error')
    return redirect(url_for('settings'))

@app.route('/test_imap')
@login_required
def test_imap():
    server = current_user.imap_server
    user = current_user.imap_user
    password = current_user.imap_password
    
    if not all([server, user, password]):
        flash('Bitte zuerst IMAP-Daten speichern.', 'error')
        return redirect(url_for('settings'))
        
    try:
        with MailBox(server).login(user, password, initial_folder='INBOX'):
            flash(f'Verbindung zu {server} erfolgreich! ✅', 'success')
    except Exception as e:
        flash(f'Verbindung fehlgeschlagen: {e}', 'error')
        
    return redirect(url_for('settings'))

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    files = request.files.getlist('file')
    title = request.form.get('title')
    date_str = request.form.get('due_date')
    due_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else None

    valid_files = [f for f in files if f and allowed_file(f.filename)]

    if valid_files:
        # Titel Fallback, falls leer gelassen (wird ggf. durch AI ersetzt)
        if not title:
            title = "Wird verarbeitet..."

        # Haupt-Dateiname für die Bill-Tabelle (Legacy-Support & Thumbnail)
        first_file = valid_files[0]
        ext = first_file.filename.rsplit('.', 1)[1].lower()
        ts = int(datetime.now().timestamp())
        main_filename = f"manual_{ts}_0.{ext}"

        new_bill = Bill(title=title, filename=main_filename, due_date=due_date, user_id=current_user.id)
        db.session.add(new_bill)
        db.session.flush() # ID generieren

        # Zuerst speichern, damit die Datei für die AI-Analyse existiert
        for i, file in enumerate(valid_files):
            # Dateiname generieren (der erste bekommt den main_filename)
            if i == 0: 
                save_name = main_filename
            else: 
                f_ext = file.filename.rsplit('.', 1)[1].lower()
                save_name = f"manual_{ts}_{i}.{f_ext}"
            
            file.save(os.path.join(UPLOAD_FOLDER, save_name))
            db.session.add(BillFile(bill_id=new_bill.id, filename=save_name))

        # AI Analyse starten
        ai_data = analyze_bill_ai(os.path.join(UPLOAD_FOLDER, main_filename))
        if ai_data:
            # Nur überschreiben, wenn vom User nicht manuell gesetzt
            if not due_date and ai_data.get('date'):
                new_bill.due_date = ai_data['date']
            if (title == "Wird verarbeitet..." or not request.form.get('title')) and ai_data.get('title'):
                new_bill.title = ai_data['title']
            if ai_data.get('amount'):
                new_bill.amount = ai_data['amount']

        db.session.commit()
    return redirect(url_for('index'))

@app.route('/update_date/<int:id>', methods=['POST'])
@login_required
def update_date(id):
    bill = Bill.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    date_str = request.form.get('due_date')
    if date_str:
        bill.due_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        db.session.commit()
    return redirect(url_for('index'))

@app.route('/pay/<int:id>')
@login_required
def pay(id):
    bill = Bill.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    bill.status = 'bezahlt'
    bill.paid_at = datetime.now()
    db.session.commit()
    
    if request.referrer and 'bill' in request.referrer:
        return redirect(request.referrer)
    return redirect(url_for('index'))

@app.route('/bill/<int:id>')
@login_required
def bill_details(id):
    bill = Bill.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    return render_template('bill_details.html', bill=bill)

@app.route('/edit/<int:id>', methods=['POST'])
@login_required
def edit_bill(id):
    bill = Bill.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    title = request.form.get('title')
    date_str = request.form.get('due_date')
    amount_str = request.form.get('amount')
    
    if title:
        bill.title = title
    if date_str:
        try:
            bill.due_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            pass
    if amount_str:
        try:
            bill.amount = float(amount_str)
        except ValueError:
            pass
            
    db.session.commit()
    flash('Änderungen gespeichert.', 'success')
    return redirect(url_for('bill_details', id=id))

@app.route('/delete/<int:id>')
@login_required
def delete_bill(id):
    bill = Bill.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    
    # Datei vom Dateisystem löschen
    files_to_delete = set()
    files_to_delete.add(bill.filename)
    for f in bill.files:
        files_to_delete.add(f.filename)

    for fname in files_to_delete:
        fp = os.path.join(UPLOAD_FOLDER, fname)
        if os.path.exists(fp):
            try: os.remove(fp)
            except: pass
            
    db.session.delete(bill)
    db.session.commit()
    flash('Rechnung gelöscht.', 'success')
    return redirect(url_for('index'))

@app.route('/file/<filename>')
@login_required
def serve_file(filename):
    bill = Bill.query.filter_by(filename=filename).first()
    
    if not bill:
        # Falls Datei nicht in der Haupt-Tabelle, in der Unter-Tabelle suchen
        bill_file = BillFile.query.filter_by(filename=filename).first()
        if bill_file:
            bill = bill_file.bill
            
    if not bill:
        abort(404)
    if bill.user_id != current_user.id:
        abort(403)
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(host='0.0.0.0', port=5000)