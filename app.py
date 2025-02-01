from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from datetime import datetime, timedelta, timezone
from config import Config
import os
import base64
import time

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(Config)

# Initialize SQLAlchemy for database
db = SQLAlchemy(app)

# Initialize Flask-Login
login_manager = LoginManager(app)
login_manager.login_view = 'login'



# Gmail daily sending limits
GMAIL_DAILY_LIMIT = 500  # Standard Gmail limit (2000 for Google Workspace)

# Define User model
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=True)
    name = db.Column(db.String(100))
    profile_pic = db.Column(db.String(200))
    google_auth = db.Column(db.Boolean, default=False)
    campaigns = db.relationship('Campaign', backref='author', lazy=True)

# Define Campaign model
class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200))
    recipients = db.Column(db.Integer)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Load user for Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Allow insecure transport for OAuth (for local testing only)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# Function to get Google OAuth flow
def get_google_flow():
    return Flow.from_client_secrets_file(
        'client_secret.json',
        scopes=app.config['SCOPES'],
        redirect_uri=app.config['GOOGLE_REDIRECT_URI']
    )

# Function to get email sending limits
def get_email_limits():
    now = datetime.now(timezone.utc)
    if 'last_reset' not in session or now - session['last_reset'] > timedelta(hours=24):
        session['emails_sent_today'] = 0
        session['last_reset'] = now  # Reset the timer

    remaining_emails = GMAIL_DAILY_LIMIT - session.get('emails_sent_today', 0)
    return {
        'limit': GMAIL_DAILY_LIMIT,
        'emails_sent_today': session.get('emails_sent_today', 0),
        'remaining': remaining_emails,
        'next_reset': session.get('last_reset', now) + timedelta(hours=24)
    }

# Home route
@app.route('/')
def home():
    return redirect(url_for('login'))

# Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid email or password', 'error')
    return render_template('login.html')

# Register route
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        if User.query.filter_by(email=email).first():
            flash('Email already registered!', 'error')
            return redirect(url_for('register'))
        new_user = User(
            name=name,
            email=email,
            password=generate_password_hash(password),
            google_auth=False
        )
        db.session.add(new_user)
        db.session.commit()
        flash('Registration successful! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

# Google login route
@app.route('/google-login')
def google_login():
    try:
        flow = get_google_flow()
        authorization_url, state = flow.authorization_url(
            access_type='offline',
            prompt='consent'
        )
        session['state'] = state
        return redirect(authorization_url)
    except Exception as e:
        flash(f'Google login error: {str(e)}', 'error')
        return redirect(url_for('login'))

# Google callback route
@app.route('/google-callback')
def google_callback():
    try:
        flow = get_google_flow()
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        if credentials.expired:
            credentials.refresh(Request())
        session['google_credentials'] = {
            'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes
        }
        service = build('oauth2', 'v2', credentials=credentials)
        user_info = service.userinfo().get().execute()
        user = User.query.filter_by(email=user_info['email']).first()
        if not user:
            user = User(
                email=user_info['email'],
                password=None,
                name=user_info.get('name'),
                profile_pic=user_info.get('picture'),
                google_auth=True
            )
            db.session.add(user)
        db.session.commit()
        login_user(user)
        return redirect(url_for('dashboard'))
    except Exception as e:
        flash(f'Google callback error: {str(e)}', 'error')
        return redirect(url_for('login'))

# Dashboard route
@app.route('/dashboard')
@login_required
def dashboard():
    limits = get_email_limits()
    campaigns = Campaign.query.filter_by(user_id=current_user.id).order_by(Campaign.sent_at.desc()).limit(10).all()
    return render_template('dashboard.html', 
                         user=current_user,
                         limits=limits,
                         campaigns=campaigns)

# Send emails route
@app.route('/send-emails', methods=['POST'])
@login_required
def send_emails():
    try:
        limits = get_email_limits()
        if limits['emails_sent_today'] >= limits['limit']:
            flash('Daily email limit reached! Try again tomorrow.', 'error')
            return redirect(url_for('dashboard'))

        emails = [e.strip() for e in request.form.get('emails').split('\n') if e.strip()]
        subject = request.form.get('subject')
        body = request.form.get('body')
        delay = int(request.form.get('delay', 1))  # Get delay from form input (default: 1 second)

        if len(emails) > limits['remaining']:
            flash(f'Only {limits["remaining"]} emails remaining today!', 'error')
            return redirect(url_for('dashboard'))

        creds_data = session.get('google_credentials')
        credentials = Credentials(
            token=creds_data['token'],
            refresh_token=creds_data['refresh_token'],
            token_uri=creds_data['token_uri'],
            client_id=creds_data['client_id'],
            client_secret=creds_data['client_secret'],
            scopes=creds_data['scopes']
        )

        service = build('gmail', 'v1', credentials=credentials)
        sent_count = 0
        
        for email in emails:
            message = {
                'raw': base64.urlsafe_b64encode(
                    f"To: {email}\nSubject: {subject}\n\n{body}".encode('utf-8')
                ).decode('utf-8')
            }
            try:
                service.users().messages().send(userId='me', body=message).execute()
                sent_count += 1
                session['emails_sent_today'] = session.get('emails_sent_today', 0) + 1  # Update session
                time.sleep(delay)  # Use the user-specified delay
            except Exception as e:
                print(f"Failed to send to {email}: {str(e)}")

        new_campaign = Campaign(
            subject=subject,
            recipients=sent_count,
            author=current_user
        )
        db.session.add(new_campaign)
        db.session.commit()

        flash(f'Sent {sent_count}/{len(emails)} emails successfully!', 'success')
        return redirect(url_for('dashboard'))
    except Exception as e:
        flash(f'Error sending emails: {str(e)}', 'error')
        return redirect(url_for('dashboard'))
    
# Profile route
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        current_user.name = request.form.get('name')
        new_password = request.form.get('password')
        if new_password:
            current_user.password = generate_password_hash(new_password)
        db.session.commit()
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=current_user)

# Logout route
@app.route('/logout')
def logout():
    logout_user()
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))

# Main entry point
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Create database tables

    # app.run(host='0.0.0.0', port=5000, debug=True)  # Comment this out for static deployment
