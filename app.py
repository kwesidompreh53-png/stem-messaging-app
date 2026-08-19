import os
import re
import uuid
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from gtts import gTTS
from celery import Celery
import requests
import openpyxl

app = Flask(__name__)

app.config['SECRET_KEY'] = 'your_secret_key_here'
database_url = os.environ.get('DATABASE_URL', 'sqlite:///stem.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['CELERY_BROKER_URL'] = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
app.config['CELERY_RESULT_BACKEND'] = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# Paystack Secret Key
PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY')

db = SQLAlchemy(app)
migrate = Migrate(app, db)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

def make_celery(app):
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY_RESULT_BACKEND'],
        broker=app.config['CELERY_BROKER_URL']
    )
    celery.conf.update(app.config)
    
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return super().__call__(*args, **kwargs)
                
    celery.Task = ContextTask
    return celery

celery = make_celery(app)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    balance = db.Column(db.Float, default=0.0)  # Main Wallet balance holding deposited money

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class Contact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    group_name = db.Column(db.String(50), default='General')

class WalletTransaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    reference = db.Column(db.String(100), unique=True, nullable=False)
    status = db.Column(db.String(30), default='Success')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class BundleHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    bundle_type = db.Column(db.String(20), nullable=False)  # 'Voice' or 'SMS'
    plan_type = db.Column(db.String(100), nullable=False)   # e.g., 'Custom SMS', 'Voice Pro GHS 50'
    cost = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class MessageLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    recipient = db.Column(db.String(20), nullable=False)
    message_body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default='Pending')
    channel = db.Column(db.String(20), default='SMS')
    provider_sid = db.Column(db.String(100), nullable=True)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            return "Username already exists! <a href='/register'>Try again</a>"
            
        new_user = User(username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        return "Invalid username or password! <a href='/login'>Try again</a>"
        
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        new_username = request.form.get('username')
        new_password = request.form.get('password')
        
        existing_user = User.query.filter_by(username=new_username).first()
        if existing_user and existing_user.id != current_user.id:
            return "Username already taken! <a href='/profile'>Go back</a>", 400
            
        current_user.username = new_username
        if new_password:
            current_user.set_password(new_password)
            
        db.session.commit()
        return redirect(url_for('profile'))
        
    return render_template('profile.html')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        default_sender = request.form.get('default_sender', 'STE-M')
        max_retry = request.form.get('max_retry', '3')
        return redirect(url_for('settings'))
    return render_template('settings.html')

# --- Wallet & Bundle Menu Routes ---

@app.route('/purchase-bundle', methods=['GET', 'POST'])
@login_required
def buy_points():
    if request.method == 'POST':
        bundle_category = request.form.get('bundle_category', 'Voice') # 'Voice' or 'SMS'
        plan_type = request.form.get('plan_type', 'Standard Bundle')
        
        try:
            amount = float(request.form.get('amount', 0))
        except (TypeError, ValueError):
            return "Invalid amount entered. <a href='/purchase-bundle'>Go back</a>", 400
            
        if amount <= 0:
            return "Amount must be greater than zero. <a href='/purchase-bundle'>Go back</a>", 400

        # Automatic deduction check from main wallet balance
        if current_user.balance < amount:
            return "Insufficient funds in your main wallet! Please top up your wallet first. <a href='/purchase-bundle'>Go back</a>", 400
            
        # Deduct automatically from main wallet
        current_user.balance -= amount
        
        # Save bundle purchase history record
        new_bundle = BundleHistory(
            user_id=current_user.id,
            bundle_type=bundle_category,
            plan_type=plan_type,
            cost=amount
        )
        db.session.add(new_bundle)
        db.session.commit()
        
        return redirect(url_for('bundle_history'))

    return render_template('buy_points.html', balance=current_user.balance)

@app.route('/wallet-history', methods=['GET'])
@login_required
def wallet_history():
    transactions = WalletTransaction.query.filter_by(user_id=current_user.id).order_by(WalletTransaction.timestamp.desc()).all()
    return render_template('wallet_history.html', transactions=transactions, balance=current_user.balance)

@app.route('/bundle-history', methods=['GET'])
@login_required
def bundle_history():
    bundles = BundleHistory.query.filter_by(user_id=current_user.id).order_by(BundleHistory.timestamp.desc()).all()
    return render_template('bundle_history.html', bundles=bundles, balance=current_user.balance)

# -----------------------------------

@app.route('/initialize-payment', methods=['POST'])
@login_required
def initialize_payment():
    amount = float(request.form.get('amount', 0))
    phone_number = request.form.get('phone_number')
    network = request.form.get('network') # MTN, VOD, ATL
    
    if amount <= 0:
        return "Invalid amount <a href='/purchase-bundle'>Go back</a>", 400
        
    amount_in_pesewas = int(amount * 100)
    
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "email": f"user_{current_user.id}@stem-platform.com",
        "amount": amount_in_pesewas,
        "currency": "GHS",
        "callback_url": url_for('payment_callback', _external=True),
        "metadata": {
            "user_id": current_user.id,
            "deposit_amount": amount,
            "phone": phone_number,
            "network": network
        }
    }
    
    response = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers)
    data = response.json()
    
    if data.get('status'):
        authorization_url = data['data']['authorization_url']
        return redirect(authorization_url)
    else:
        error_msg = data.get('message', 'Unknown error')
        return f"Payment initialization failed: {error_msg} <a href='/purchase-bundle'>Go back</a>", 400

@app.route('/payment-callback', methods=['GET'])
@login_required
def payment_callback():
    reference = request.args.get('reference')
    if not reference:
        return redirect(url_for('dashboard'))
        
    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
    }
    
    response = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers)
    data = response.json()
    
    if data.get('status') and data['data']['status'] == 'success':
        metadata = data['data'].get('metadata', {})
        deposit_amount = float(metadata.get('deposit_amount', 0))
        
        # Check if transaction already recorded to prevent double-crediting on refresh
        existing_tx = WalletTransaction.query.filter_by(reference=reference).first()
        if not existing_tx:
            current_user.balance = (current_user.balance or 0.0) + deposit_amount
            
            # Save transaction history record
            new_tx = WalletTransaction(
                user_id=current_user.id,
                amount=deposit_amount,
                reference=reference,
                status='Success'
            )
            db.session.add(new_tx)
            db.session.commit()
        
        return redirect(url_for('wallet_history'))
    
    return "Payment verification failed or was cancelled. <a href='/dashboard'>Go to Dashboard</a>", 400

@app.route('/')
@login_required
def dashboard():
    contacts = Contact.query.all()
    logs = MessageLog.query.all()
    groups = db.session.query(Contact.group_name.distinct()).all()
    group_list = [g[0] for g in groups if g[0]]
    return render_template('index.html', contacts=contacts, logs=logs, groups=group_list, balance=current_user.balance)

@app.route('/add-contact-web', methods=['POST'])
@login_required
def add_contact_web():
    name = request.form.get('name', '').strip()
    phone_number = request.form.get('phone_number', '').strip()
    group_name = request.form.get('group_name', 'General')
    
    if not re.match(r"^[A-Za-z\s]+$", name):
        return "Error: Name must contain alphabets only! <a href='/'>Go back</a>", 400
        
    if not re.match(r"^\+?[0-9]+$", phone_number):
        return "Error: Phone number must contain numbers only! <a href='/'>Go back</a>", 400
    
    new_contact = Contact(name=name, phone_number=phone_number, group_name=group_name)
    db.session.add(new_contact)
    db.session.commit()
    return redirect(url_for('dashboard'))

# --- EXCEL CONTACT UPLOAD ROUTE USING OPENPYXL ---
@app.route('/upload-contacts-excel', methods=['POST'])
@login_required
def upload_contacts_excel():
    if 'excel_file' not in request.files:
        return "No file part uploaded! <a href='/'>Go back</a>", 400
        
    file = request.files['excel_file']
    group_name = request.form.get('group_name', 'General').strip()
    
    if file.filename == '':
        return "No selected file! <a href='/'>Go back</a>", 400
        
    if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
        filename = secure_filename(file.filename)
        filepath = os.path.join('static', filename)
        file.save(filepath)
        
        try:
            wb = openpyxl.load_workbook(filepath, data_only=True)
            sheet = wb.active
            
            header_row = [str(cell.value).strip().lower() for cell in sheet[1]]
            
            name_idx = next((i for i, h in enumerate(header_row) if h and ('name' in h or 'student' in h)), None)
            phone_idx = next((i for i, h in enumerate(header_row) if h and ('phone' in h or 'contact' in h or 'mobile' in h)), None)
            
            if name_idx is None or phone_idx is None:
                if os.path.exists(filepath):
                    os.remove(filepath)
                return "Error: Excel file must contain header columns for 'Name' and 'Phone'! <a href='/'>Go back</a>", 400
                
            count = 0
            for row in sheet.iter_rows(min_row=2, values_only=True):
                if len(row) > max(name_idx, phone_idx):
                    name = str(row[name_idx]).strip() if row[name_idx] is not None else ''
                    phone = str(row[phone_idx]).strip() if row[phone_idx] is not None else ''
                    
                    if phone.endswith('.0'):
                        phone = phone[:-2]
                        
                    if name and phone and name.lower() != 'none' and phone.lower() != 'none':
                        new_contact = Contact(name=name, phone_number=phone, group_name=group_name)
                        db.session.add(new_contact)
                        count += 1
                        
            db.session.commit()
            
            if os.path.exists(filepath):
                os.remove(filepath)
                
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            if os.path.exists(filepath):
                os.remove(filepath)
            return f"Error processing Excel sheet: {str(e)} <a href='/'>Go back</a>", 400
            
    return "Invalid file format. Please upload an Excel (.xlsx or .xls) file. <a href='/'>Go back</a>", 400

@app.route('/edit-contact/<int:contact_id>', methods=['GET', 'POST'])
@login_required
def edit_contact(contact_id):
    contact = Contact.query.get_or_404(contact_id)
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        phone_number = request.form.get('phone_number', '').strip()
        
        if not re.match(r"^[A-Za-z\s]+$", name):
            return "Error: Name must contain alphabets only! <a href='/edit-contact/{}'>Go back</a>".format(contact_id), 400
            
        if not re.match(r"^\+?[0-9]+$", phone_number):
            return "Error: Phone number must contain numbers only! <a href='/edit-contact/{}'>Go back</a>".format(contact_id), 400
            
        contact.name = name
        contact.phone_number = phone_number
        contact.group_name = request.form.get('group_name', 'General')
        db.session.commit()
        return redirect(url_for('dashboard'))
    return render_template('edit_contact.html', contact=contact)

@app.route('/delete-contact/<int:contact_id>', methods=['POST'])
@login_required
def delete_contact(contact_id):
    contact = Contact.query.get_or_404(contact_id)
    db.session.delete(contact)
    db.session.commit()
    return redirect(url_for('dashboard'))

@app.route('/send-bulk-web', methods=['POST'])
@login_required
def send_bulk_web():
    group_name = request.form.get('group_name')
    message_text = request.form.get('message')
    channel = request.form.get('channel')
    schedule_str = request.form.get('schedule_time')
    
    if schedule_str:
        scheduled_dt = datetime.strptime(schedule_str, '%Y-%m-%dT%H:%M')
        send_bulk_campaign.apply_async(args=[group_name, message_text, channel], eta=scheduled_dt)
    else:
        send_bulk_campaign.delay(group_name, message_text, channel)
        
    return redirect(url_for('dashboard'))

@celery.task(name='app.send_bulk_campaign')
def send_bulk_campaign(group_name, message_text, channel):
    contacts = Contact.query.filter_by(group_name=group_name).all()
    for contact in contacts:
        tracking_sid = f"SID_{uuid.uuid4().hex[:12]}"
        if channel.upper() == 'SMS':
            log = MessageLog(recipient=contact.phone_number, message_body=message_text, status='Sent (Queued)', channel='SMS', provider_sid=tracking_sid)
            db.session.add(log)
        elif channel.upper() == 'VOICE':
            tts = gTTS(text=message_text, lang='en', slow=False)
            os.makedirs('static', exist_ok=True)
            audio_filename = f"voice_{contact.phone_number.replace('+', '')}.mp3"
            tts.save(os.path.join('static', audio_filename))
            log = MessageLog(recipient=contact.phone_number, message_body=message_text, status='Call Initiated', channel='Voice', provider_sid=tracking_sid)
            db.session.add(log)
    db.session.commit()
    return f"Bulk campaign completed for group: {group_name}"

@celery.task(name='app.send_mp3_campaign')
def send_mp3_campaign(recipient_group, filename):
    contacts = Contact.query.filter_by(group_name=recipient_group).all()
    if not contacts:
        tracking_sid = f"SID_{uuid.uuid4().hex[:12]}"
        db.session.add(MessageLog(recipient=recipient_group, message_body=f"[Uploaded MP3: {filename}]", status='MP3 Ready', channel='Voice (MP3)', provider_sid=tracking_sid))
    else:
        for contact in contacts:
            tracking_sid = f"SID_{uuid.uuid4().hex[:12]}"
            db.session.add(MessageLog(recipient=contact.phone_number, message_body=f"[Uploaded MP3: {filename}]", status='MP3 Broadcast Queued', channel='Voice (MP3)', provider_sid=tracking_sid))
    db.session.commit()
    return f"MP3 campaign completed for group: {recipient_group}"

@app.route('/upload-mp3-campaign', methods=['POST'])
@login_required
def upload_mp3_campaign():
    if 'mp3_file' not in request.files:
        return "No audio file uploaded! <a href='/'>Go back</a>", 400
    mp3_file = request.files['mp3_file']
    recipient_group = request.form.get('recipient_group', 'General')
    schedule_str = request.form.get('schedule_time')
    
    if mp3_file.filename == '':
        return "No selected file! <a href='/'>Go back</a>", 400
    
    if mp3_file and mp3_file.filename.endswith('.mp3'):
        os.makedirs('static', exist_ok=True)
        filename = f"campaign_{recipient_group}_{mp3_file.filename}"
        mp3_file.save(os.path.join('static', filename))
        
        if schedule_str:
            scheduled_dt = datetime.strptime(schedule_str, '%Y-%m-%dT%H:%M')
            send_mp3_campaign.apply_async(args=[recipient_group, mp3_file.filename], eta=scheduled_dt)
        else:
            send_mp3_campaign.delay(recipient_group, mp3_file.filename)
            return redirect(url_for('dashboard'))
            
    return "Invalid file format. Please upload an MP3 file. <a href='/'>Go back</a>", 400

@app.route('/sms-webhook', methods=['POST'])
def sms_webhook():
    provider_sid = request.form.get('MessageSid') or request.form.get('id')
    new_status = request.form.get('MessageStatus') or request.form.get('status')
    if provider_sid and new_status:
        log_entry = MessageLog.query.filter_by(provider_sid=provider_sid).first()
        if log_entry:
            log_entry.status = new_status.capitalize()
            db.session.commit()
    return '', 200

@app.route('/voice-webhook', methods=['POST'])
def voice_webhook():
    provider_sid = request.form.get('CallSid') or request.form.get('id')
    call_status = request.form.get('CallStatus') or request.form.get('status')
    if provider_sid and call_status:
        log_entry = MessageLog.query.filter_by(provider_sid=provider_sid).first()
        if log_entry:
            log_entry.status = 'Call Answered / Completed' if call_status.lower() in ['completed', 'answered'] else f"Call {call_status.capitalize()}"
            db.session.commit()
    return '', 200

# Global table creation for Gunicorn/Render compatibility
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)